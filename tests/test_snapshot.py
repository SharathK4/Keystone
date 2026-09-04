"""The frontend contract: what a snapshot holds, and what the API will not do.

Three things are being defended here.

**The contract is analytical, not a simulator UI.** A test walks every response
model looking for simulator vocabulary - tick, timeline, cursor, play state - and
fails if any of it has leaked into a field name. That is a real risk: the backend
runs a discrete-event simulation, and it would be easy for its concepts to seep
into the payload one field at a time.

**Requests are bounded.** On-demand analysis has a merchant ceiling, a candidate
cap and an action cap. A test asserts the ceiling is enforced by refusal rather
than by silently returning a worse answer.

**Numbers are computed.** The dashboard is asserted against values derived
independently from the store, so a hardcoded figure would fail rather than look
plausible.
"""

from __future__ import annotations

import json

import pytest

from lce.errors import NotFoundError, ValidationError
from lce.snapshot.build import build_snapshot
from lce.snapshot.dashboard import build_dashboard, execution_status
from lce.snapshot.models import OFFER_DISCLAIMER, SNAPSHOT_FORMAT_VERSION
from lce.snapshot.store import (
    MAX_ON_DEMAND_ACTIONS,
    MAX_ON_DEMAND_CANDIDATES,
    SnapshotStore,
    list_snapshots,
    reset_store,
    resolve_snapshot,
    save_snapshot,
)

TINY = {
    "n_merchants": 26,
    "n_layers": 3,
    "history_hours": 16 * 24.0,
    "horizon_hours": 168.0,
}

#: Vocabulary that would mean simulator mechanics had leaked into the contract.
FORBIDDEN_FIELD_TOKENS = (
    "tick",
    "timeline",
    "cursor",
    "playing",
    "play_state",
    "is_paused",
    "current_time",
    "step_index",
    "frame",
)


@pytest.fixture(scope="session")
def snapshot_dir(tmp_path_factory):
    """One small snapshot, built once and shared - building it is the slow part."""
    from lce.benchmark.scenarios import ScenarioFamily

    payload, manifest = build_snapshot(
        seed=4242,
        scale="small",
        families=(
            ScenarioFamily.SINGLE_MISSED_INFLOW,
            ScenarioFamily.CONCENTRATED_SHOCK,
        ),
        systemic_sample=12,
        generator_overrides=TINY,
    )
    root = tmp_path_factory.mktemp("snapshots")
    return save_snapshot(payload, manifest, root / manifest.snapshot_id)


@pytest.fixture(scope="session")
def store(snapshot_dir) -> SnapshotStore:
    return SnapshotStore(snapshot_dir)


@pytest.fixture
def client(snapshot_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from lce.api.app import create_app
    from lce.snapshot import store as store_module

    loaded = SnapshotStore(snapshot_dir)
    reset_store()
    monkeypatch.setattr(store_module, "get_store", lambda *a, **k: loaded)
    with TestClient(create_app()) as test_client:
        yield test_client
    reset_store()


# ------------------------------------------------------------------- artifact


class TestSnapshotArtifact:
    def test_round_trip(self, snapshot_dir, store):
        assert store.manifest.format_version == SNAPSHOT_FORMAT_VERSION
        assert store.manifest.content_hash
        assert store.manifest.n_scenarios == len(store.scenarios())

    def test_listing_and_resolution(self, snapshot_dir):
        root = snapshot_dir.parent
        entries = list_snapshots(root)
        assert entries and entries[0]["snapshot_id"] == snapshot_dir.name
        assert resolve_snapshot(root, snapshot_dir.name) == snapshot_dir
        with pytest.raises(NotFoundError, match="no snapshot with id"):
            resolve_snapshot(root, "snap-nope")

    def test_an_unsupported_format_is_refused(self, snapshot_dir, tmp_path):
        payload = json.loads((snapshot_dir / "snapshot.json").read_text(encoding="utf-8"))
        payload["format_version"] = SNAPSHOT_FORMAT_VERSION + 5
        target = tmp_path / "bad"
        target.mkdir()
        (target / "snapshot.json").write_text(json.dumps(payload), encoding="utf-8")
        (target / "manifest.json").write_text(
            (snapshot_dir / "manifest.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        with pytest.raises(ValidationError, match="format"):
            SnapshotStore(target)

    def test_a_missing_snapshot_is_reported(self, tmp_path):
        with pytest.raises(NotFoundError):
            SnapshotStore(tmp_path / "nothing")

    def test_the_snapshot_carries_no_payment_history(self, store):
        """The serving graph is merchants, obligations and the estimated overlay."""
        assert store.graph.stats().n_payment_events == 0
        assert store.graph.stats().n_merchants > 0
        assert store.graph.dependency_edges

    def test_the_overlay_is_estimated_not_ground_truth(self, store):
        """Shipping the generator's true edges would leak the answer to a client."""
        assert all(not edge.is_ground_truth for edge in store.graph.dependency_edges)
        assert all(d.estimated for d in store.dependencies())


# ------------------------------------------------------------------- the reads


class TestReads:
    def test_network_overview_is_consistent(self, store):
        network = store.network()
        assert network.n_merchants == len(store.merchants())
        assert network.total_obligation_value > 0
        assert network.horizon_hours > 0

    def test_history_derived_counts_survive_the_event_strip(self, store):
        """The serving graph carries no events; the overview must still report them.

        These three fields are computed from the payment history, which is
        deliberately dropped before the snapshot is written. They have to be
        carried over from the build-time graph, and one of them once was not -
        the overview advertised a hundred merchants with zero relationships
        between them while /network/dependencies returned 216 edges.
        """
        network = store.network()
        assert network.n_payment_events > 0
        assert network.total_payment_value > 0
        assert network.n_relationships > 0
        assert store.graph.stats().n_payment_events == 0

    def test_merchant_fields_are_derived(self, store):
        for merchant in store.merchants():
            assert merchant.liquidity_buffer == pytest.approx(
                merchant.opening_balance + merchant.credit_limit - merchant.operating_floor
            )
            assert merchant.net_position == pytest.approx(
                merchant.receivables_in_horizon - merchant.payables_in_horizon
            )
            if merchant.payables_in_horizon > 0:
                assert merchant.cover_ratio is not None
                assert merchant.vulnerable == (merchant.cover_ratio < 1.0)
            else:
                assert merchant.cover_ratio is None
                assert not merchant.vulnerable

    def test_unknown_merchant_is_reported(self, store):
        with pytest.raises(NotFoundError, match="unknown merchant"):
            store.merchant("not_a_merchant")

    def test_relationships_split_by_direction(self, store):
        merchant_id = store.dependencies()[0].source_id
        relationships = store.dependencies_for(merchant_id)
        assert all(e.source_id == merchant_id for e in relationships["downstream"])
        assert all(e.target_id == merchant_id for e in relationships["upstream"])

    def test_systemic_ranking_reports_its_baselines(self, store):
        ranking = store.systemic()
        assert ranking.entries
        assert [e.rank for e in ranking.entries] == list(
            range(1, len(ranking.entries) + 1)
        )
        # The claim that this is not a size ranking has to be checkable.
        assert set(ranking.baseline_rank_correlation) >= {
            "throughput", "degree", "cash_deficit"
        }

    def test_every_scenario_is_a_complete_result(self, store):
        for scenario in store.scenarios():
            assert scenario.shock.description
            assert scenario.projected_impact.disruption_index >= 0
            assert scenario.counterfactual.baseline_disruption > 0
            assert scenario.provenance.scenario_id == scenario.scenario_id
            assert scenario.provenance.dataset_version
            # Affected merchants are ranked and ordered by damage.
            values = [m.disrupted_value for m in scenario.affected_merchants]
            assert values == sorted(values, reverse=True)

    def test_time_to_constraint_is_a_cumulative_distribution(self, store):
        for scenario in store.scenarios():
            shares = [b.cumulative_share for b in scenario.time_to_constraint]
            assert shares == sorted(shares)
            assert all(0.0 <= s <= 1.0 for s in shares)

    def test_unknown_scenario_is_reported(self, store):
        with pytest.raises(NotFoundError, match="unknown scenario"):
            store.scenario("scn-nope")


# --------------------------------------------------------------------- offers


class TestOffers:
    def test_an_offer_is_a_recommendation_not_an_approval(self, store):
        for offer in store.offers():
            assert offer.status == "recommended"
            assert offer.disclaimer == OFFER_DISCLAIMER
            assert offer.proposed_amount > 0
            assert offer.duration_hours > 0
            assert offer.repayment.n_instalments >= 1

    def test_capital_and_cost_are_separate(self, store):
        """A facility's cost is a fee, not the principal."""
        for offer in store.offers():
            assert offer.indicative_cost < offer.proposed_amount
            assert offer.indicative_rate_annual_pct is not None

    def test_the_offer_states_its_eligibility(self, store):
        for offer in store.offers():
            assert set(offer.eligibility.criteria)
            assert offer.eligibility.eligible == all(offer.eligibility.criteria.values())
            assert "max_amount" in offer.eligibility.constraints

    def test_an_offer_carries_its_network_benefit(self, store):
        for offer in store.offers():
            benefit = offer.expected_network_benefit
            assert set(benefit) >= {"disruption_prevented", "commerce_preserved"}

    def test_a_merchant_with_no_recommendation_has_no_offer(self, store):
        offered = {o.merchant_id for o in store.offers()}
        others = [m.merchant_id for m in store.merchants() if m.merchant_id not in offered]
        if not others:
            pytest.skip("every merchant received a recommendation")
        with pytest.raises(NotFoundError, match="no intervention was recommended"):
            store.offer(others[0])


# ----------------------------------------------------------- bounded analysis


class TestBoundedAnalysis:
    def test_on_demand_analysis_returns_a_complete_scenario(self, store):
        target = store.merchants()[0].merchant_id
        result = store.analyze(merchant_ids=[target], magnitude_multiple=2.0)
        assert result.family == "on_demand"
        assert result.shock.origin_merchants == [target]
        assert result.counterfactual.baseline_disruption >= 0
        # No exhaustive optimum runs on this path, so no gap may be claimed.
        assert result.counterfactual.optimality_gap is None
        assert result.provenance.scenario_id == result.scenario_id

    def test_the_result_is_retrievable_afterwards(self, store):
        target = store.merchants()[0].merchant_id
        result = store.analyze(merchant_ids=[target])
        assert store.scenario(result.scenario_id).scenario_id == result.scenario_id

    def test_analysis_is_deterministic(self, store):
        target = store.merchants()[1].merchant_id
        first = store.analyze(merchant_ids=[target], magnitude_multiple=1.5)
        second = store.analyze(merchant_ids=[target], magnitude_multiple=1.5)
        assert first.scenario_id == second.scenario_id
        assert first.counterfactual.model_dump() == second.counterfactual.model_dump()

    def test_limits_are_published(self, store):
        limits = store.analysis_limits()
        assert limits["max_candidates"] == MAX_ON_DEMAND_CANDIDATES
        assert limits["max_actions"] == MAX_ON_DEMAND_ACTIONS
        assert limits["n_merchants"] == len(store.merchants())

    def test_an_unknown_origin_is_rejected(self, store):
        with pytest.raises(NotFoundError):
            store.analyze(merchant_ids=["not_a_merchant"])

    def test_an_empty_shock_is_rejected(self, store):
        with pytest.raises(ValidationError, match="at least one origin"):
            store.analyze(merchant_ids=[])

    def test_an_onset_past_the_horizon_is_rejected(self, store):
        target = store.merchants()[0].merchant_id
        with pytest.raises(ValidationError, match="onset_hours"):
            store.analyze(merchant_ids=[target], onset_hours=10_000.0)

    def test_a_network_over_the_ceiling_is_refused_not_degraded(self, store, monkeypatch):
        """Refusal is the contract; quietly returning a worse answer is not."""
        from lce.snapshot import store as store_module

        monkeypatch.setattr(store_module, "MAX_MERCHANTS_FOR_ON_DEMAND", 1)
        with pytest.raises(ValidationError, match="limited to"):
            store.analyze(merchant_ids=[store.merchants()[0].merchant_id])


# ------------------------------------------------------------------ dashboard


class TestDashboard:
    def test_values_are_computed_from_the_store(self, store):
        dashboard = build_dashboard(store, probe_execution=False)
        merchants = store.merchants()
        expected_vulnerable = [m for m in merchants if m.vulnerable]

        assert dashboard.merchants_vulnerable == len(expected_vulnerable)
        assert dashboard.total_value_exposed == pytest.approx(
            sum(m.payables_in_horizon for m in expected_vulnerable)
        )
        assert dashboard.vulnerable_share == pytest.approx(
            len(expected_vulnerable) / dashboard.network.n_merchants
        )
        assert dashboard.projected_disrupted_value == pytest.approx(
            sum(s.projected_impact.disrupted_value for s in store.scenarios())
        )

    def test_the_dashboard_carries_provenance(self, store):
        dashboard = build_dashboard(store, probe_execution=False)
        assert dashboard.provenance.dataset_version
        assert dashboard.provenance.config_hash
        assert dashboard.provenance.code_version

    def test_execution_status_never_raises(self):
        status = execution_status(probe=False)
        assert status.fallback_provider == "simulation"
        assert "no funds move" in status.note.lower() or "unavailable" in status.note


# ------------------------------------------------------------ the API surface


class TestAnalyticsAPI:
    def test_network(self, client):
        response = client.get("/api/v1/network")
        assert response.status_code == 200
        assert response.json()["n_merchants"] > 0

    def test_merchants_are_paged_and_sorted(self, client):
        first = client.get("/api/v1/network/merchants?limit=5&sort=exposure").json()
        assert len(first) <= 5
        exposures = [m["payables_in_horizon"] for m in first]
        assert exposures == sorted(exposures, reverse=True)

    def test_vulnerable_filter(self, client):
        rows = client.get("/api/v1/network/merchants?vulnerable_only=true").json()
        assert all(m["vulnerable"] for m in rows)

    def test_merchant_detail(self, client):
        merchant_id = client.get("/api/v1/network/merchants?limit=1").json()[0]["merchant_id"]
        body = client.get(f"/api/v1/network/merchants/{merchant_id}").json()
        assert body["merchant"]["merchant_id"] == merchant_id
        assert "upstream" in body and "downstream" in body

    def test_unknown_merchant_is_404(self, client):
        assert client.get("/api/v1/network/merchants/nope").status_code == 404

    def test_dependencies_and_systemic(self, client):
        assert client.get("/api/v1/network/dependencies?limit=5").status_code == 200
        systemic = client.get("/api/v1/network/systemic-importance?limit=5").json()
        assert len(systemic["entries"]) <= 5
        assert systemic["baseline_rank_correlation"]

    def test_scenario_endpoints(self, client):
        scenarios = client.get("/api/v1/scenarios").json()
        assert scenarios
        scenario_id = scenarios[0]["scenario_id"]
        for suffix in ("", "/impact", "/interventions", "/counterfactual"):
            response = client.get(f"/api/v1/scenarios/{scenario_id}{suffix}")
            assert response.status_code == 200, response.text
            assert response.json()["scenario_id"] == scenario_id

    def test_unknown_scenario_is_404(self, client):
        assert client.get("/api/v1/scenarios/scn-nope").status_code == 404

    def test_analyze_is_bounded_and_documented(self, client):
        limits = client.get("/api/v1/scenarios/analyze/limits").json()
        assert limits["bounded"] is True
        assert "exhaustive optimum and optimality gap" in limits["does_not_compute"]

        # The published origin cap must be the one the API actually rejects on,
        # or the endpoint's only purpose - pre-validation - does not work.
        cap = limits["limits"]["max_shocked_merchants"]
        merchants = client.get("/api/v1/network/merchants?limit=1").json()
        over = client.post(
            "/api/v1/scenarios/analyze",
            json={"merchant_ids": [merchants[0]["merchant_id"]] * (cap + 1)},
        )
        assert over.status_code == 422

        merchant_id = client.get("/api/v1/network/merchants?limit=1").json()[0]["merchant_id"]
        response = client.post(
            "/api/v1/scenarios/analyze",
            json={"merchant_ids": [merchant_id], "magnitude_multiple": 2.0},
        )
        assert response.status_code == 200, response.text
        assert response.json()["family"] == "on_demand"

    def test_analyze_caps_the_fan_out(self, client):
        rows = client.get("/api/v1/network/merchants?limit=10").json()
        response = client.post(
            "/api/v1/scenarios/analyze",
            json={"merchant_ids": [m["merchant_id"] for m in rows]},
        )
        assert response.status_code == 422

    def test_analyze_rejects_a_malformed_request(self, client):
        assert client.post("/api/v1/scenarios/analyze", json={}).status_code == 422
        assert (
            client.post(
                "/api/v1/scenarios/analyze",
                json={"merchant_ids": ["m0"], "max_actions": 99},
            ).status_code
            == 422
        )

    def test_offers(self, client):
        offers = client.get("/api/v1/offers").json()
        if not offers:
            pytest.skip("no offer was recommended in this snapshot")
        assert offers[0]["status"] == "recommended"
        merchant_id = offers[0]["merchant_id"]
        assert client.get(f"/api/v1/offers/{merchant_id}").status_code == 200

    def test_dashboard(self, client):
        body = client.get("/api/v1/dashboard?probe_execution=false").json()
        assert body["network"]["n_merchants"] > 0
        assert "execution" in body
        assert body["provenance"]["dataset_version"]

    def test_execution_status(self, client):
        body = client.get("/api/v1/execution/status?probe=false").json()
        assert body["fallback_provider"] == "simulation"

    def test_snapshot_info(self, client):
        body = client.get("/api/v1/snapshot").json()
        assert body["snapshot_id"]
        assert body["limits"]["max_candidates"] == MAX_ON_DEMAND_CANDIDATES


# ------------------------------------------------- the contract stays analytical


class TestContractIsAnalytical:
    def _walk(self, value, path=""):
        if isinstance(value, dict):
            for key, item in value.items():
                yield f"{path}.{key}", key
                yield from self._walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for item in value:
                yield from self._walk(item, path)

    def test_no_simulator_mechanics_in_any_field_name(self, store):
        """A scenario is a result. Timeline concepts must not appear anywhere."""
        payload = {
            "network": store.network().model_dump(),
            "merchants": [m.model_dump() for m in store.merchants()[:3]],
            "dependencies": [d.model_dump() for d in store.dependencies()[:3]],
            "systemic": store.systemic().model_dump(),
            "scenarios": [s.model_dump() for s in store.scenarios()],
            "dashboard": build_dashboard(store, probe_execution=False).model_dump(),
        }
        offenders = [
            path
            for path, key in self._walk(payload)
            if any(token in key.lower() for token in FORBIDDEN_FIELD_TOKENS)
        ]
        assert offenders == [], f"simulator vocabulary leaked into: {offenders}"

    def test_the_api_openapi_schema_is_stable_and_typed(self, client):
        schema = client.get("/openapi.json").json()
        paths = schema["paths"]
        for route in (
            "/api/v1/network",
            "/api/v1/network/merchants",
            "/api/v1/network/systemic-importance",
            "/api/v1/scenarios",
            "/api/v1/scenarios/analyze",
            "/api/v1/dashboard",
            "/api/v1/execution/status",
        ):
            assert route in paths, f"{route} missing from the published schema"

    def test_every_analytical_result_carries_provenance(self, store):
        for scenario in store.scenarios():
            assert scenario.provenance.run_id
            assert scenario.provenance.dataset_version
            assert scenario.provenance.seed is not None
            assert scenario.provenance.config_hash
            assert scenario.provenance.simulator_config_hash
            assert scenario.provenance.code_version
            if scenario.offer is not None:
                assert scenario.offer.provenance.scenario_id == scenario.scenario_id
