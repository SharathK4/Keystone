"""Benchmark layer: scales, scenarios, ground truth, validation, export."""

from __future__ import annotations

import pytest

from lce.benchmark.export import (
    export_dataset,
    export_scenario,
    list_scenarios,
    load_dataset,
    replay_scenario,
)
from lce.benchmark.ground_truth import compute_ground_truth
from lce.benchmark.manifest import DatasetManifest, make_scenario_id
from lce.benchmark.scales import (
    SCALE_PROFILES,
    BenchmarkScale,
    events_per_edge,
    profile_for,
    scale_config,
    sufficient_power,
)
from lce.benchmark.scenarios import (
    DEFAULT_STRATEGY_FOR_FAMILY,
    ScenarioFamily,
    ScenarioSpec,
    TargetStrategy,
    baseline_affected_set,
    build_scenario,
    fragility,
    horizon_payables,
    liquidity_slack,
    resolve_shock_time,
    scenario_suite,
    select_targets,
)
from lce.benchmark.validation import compute_diagnostics, validate_dataset
from lce.data.generator import GENERATOR_VERSION, GeneratorConfig, generate_network
from lce.errors import NotFoundError, ValidationError
from lce.simulation.engine import LiquiditySimulator, SimulationConfig


@pytest.fixture(scope="module")
def bench_network():
    """A small benchmark-shaped network, shared read-only across the module."""
    return generate_network(
        scale_config(
            BenchmarkScale.SMALL,
            seed=4242,
            overrides={"n_merchants": 60, "history_hours": 30 * 24.0},
        )
    )


@pytest.fixture(scope="module")
def bench_sim(bench_network):
    return SimulationConfig(
        horizon_hours=bench_network.config.horizon_hours, seed=bench_network.config.seed
    )


@pytest.fixture(scope="module")
def bench_baseline(bench_network, bench_sim):
    return baseline_affected_set(bench_network.graph, bench_sim)


class TestScales:
    def test_required_scales_exist_at_the_documented_sizes(self):
        assert profile_for(BenchmarkScale.SMALL).n_merchants == 100
        assert profile_for(BenchmarkScale.MEDIUM).n_merchants == 1_000
        assert profile_for(BenchmarkScale.LARGE).n_merchants == 10_000

    def test_event_budget_scales_with_the_network(self):
        """A fixed cap would starve the learner as the network grows."""
        budgets = [SCALE_PROFILES[s].event_budget for s in BenchmarkScale]
        assert budgets == sorted(budgets)
        for scale in BenchmarkScale:
            profile = SCALE_PROFILES[scale]
            assert sufficient_power(profile.event_budget, profile.approx_edges)

    def test_events_per_edge_is_held_roughly_constant(self):
        ratios = [
            events_per_edge(p.event_budget, p.approx_edges) for p in SCALE_PROFILES.values()
        ]
        assert max(ratios) - min(ratios) < 1e-6

    def test_only_small_claims_an_exact_optimum(self):
        """Exhaustive search is what makes the optimality gap a measurement."""
        assert profile_for(BenchmarkScale.SMALL).exhaustive_optimum
        assert not profile_for(BenchmarkScale.MEDIUM).exhaustive_optimum

    def test_large_is_flagged_for_streaming(self):
        assert profile_for(BenchmarkScale.LARGE).streaming

    def test_overrides_are_validated(self):
        with pytest.raises(ValueError, match="unknown generator parameters"):
            scale_config(BenchmarkScale.SMALL, overrides={"nonsense": 1})


class TestManifest:
    def test_manifest_round_trips_and_verifies(self, tmp_path, bench_network):
        manifest = DatasetManifest.for_config(
            bench_network.config, seeds=bench_network.seeds, scale="small"
        )
        manifest.save(tmp_path)
        loaded = DatasetManifest.load(tmp_path)

        assert loaded.dataset_id == bench_network.dataset_version
        assert loaded.generator_version == GENERATOR_VERSION
        assert loaded.seed == bench_network.config.seed
        assert loaded.created_at
        loaded.verify()

    def test_rebuilt_config_reproduces_the_dataset(self, bench_network):
        manifest = DatasetManifest.for_config(bench_network.config)
        assert manifest.rebuild_config().dataset_version == bench_network.dataset_version

    def test_manifest_from_another_generator_version_is_rejected(self, bench_network):
        """Same parameters under different semantics is a different dataset."""
        manifest = DatasetManifest.for_config(bench_network.config)
        manifest.generator_version = "0.0.1-ancient"
        with pytest.raises(ValueError, match="generator"):
            manifest.rebuild_config()

    def test_tampered_parameters_fail_verification(self, bench_network):
        manifest = DatasetManifest.for_config(bench_network.config)
        manifest.parameters = {**manifest.parameters, "n_merchants": 999}
        with pytest.raises(ValueError, match="inconsistent"):
            manifest.verify()

    def test_scenario_id_is_content_addressed(self):
        spec = {"family": "liquidity_drain", "magnitude": 2.0}
        assert make_scenario_id("ds", spec) == make_scenario_id("ds", spec)
        assert make_scenario_id("ds", spec) != make_scenario_id("other", spec)


class TestTargeting:
    def test_liquidity_slack_exceeds_buffer_for_a_replenished_node(self, bench_network):
        """Slack, not the buffer, is what a shock must overcome."""
        graph = bench_network.graph
        replenished = [
            m
            for m in graph.merchant_ids
            if sum(o.outstanding for o in graph.receivables_of(m) if o.is_open)
            > horizon_payables(graph, m) > 0
        ]
        assert replenished, "expected at least one net-inflow merchant"
        for merchant_id in replenished[:5]:
            assert liquidity_slack(graph, merchant_id) > graph.merchant(
                merchant_id
            ).initial_buffer

    def test_fragility_is_payables_over_buffer(self, bench_network):
        graph = bench_network.graph
        merchant_id = next(m for m in graph.merchant_ids if horizon_payables(graph, m) > 0)
        expected = horizon_payables(graph, merchant_id) / graph.merchant(
            merchant_id
        ).initial_buffer
        assert fragility(graph, merchant_id) == pytest.approx(expected)

    def test_targets_exclude_already_failing_merchants(self, bench_network, bench_baseline):
        """A node already failing cannot carry shock-attributable damage."""
        spec = ScenarioSpec(family=ScenarioFamily.LIQUIDITY_DRAIN)
        targets = select_targets(bench_network.graph, spec, count=3, exclude=bench_baseline)
        assert targets
        assert set(targets).isdisjoint(bench_baseline)

    def test_explicit_targets_bypass_the_exclusion(self, bench_network):
        spec = ScenarioSpec(
            family=ScenarioFamily.LIQUIDITY_DRAIN,
            target_strategy=TargetStrategy.EXPLICIT,
            explicit_targets=("m0000",),
        )
        assert select_targets(bench_network.graph, spec, exclude={"m0000"}) == ["m0000"]

    def test_unknown_explicit_target_is_rejected(self, bench_network):
        spec = ScenarioSpec(
            family=ScenarioFamily.LIQUIDITY_DRAIN,
            target_strategy=TargetStrategy.EXPLICIT,
            explicit_targets=("nope",),
        )
        with pytest.raises(ValidationError, match="unknown explicit targets"):
            select_targets(bench_network.graph, spec)

    def test_shock_time_defaults_to_just_before_a_commitment(self, bench_network):
        """t=0 lets inbound payments refill the buffer before any deadline."""
        graph = bench_network.graph
        merchant_id = next(
            m
            for m in graph.merchant_ids
            if [o for o in graph.payables_of(m) if o.is_open and o.due_t > 24.0]
        )
        resolved = resolve_shock_time(graph, merchant_id, explicit=None)
        earliest = min(o.due_t for o in graph.payables_of(merchant_id) if o.is_open)
        assert resolved == pytest.approx(max(0.0, earliest - 1.0))

    def test_explicit_shock_time_wins(self, bench_network):
        assert resolve_shock_time(
            bench_network.graph, "m0000", explicit=42.0
        ) == pytest.approx(42.0)

    def test_each_family_declares_a_targeting_rule(self):
        assert set(DEFAULT_STRATEGY_FOR_FAMILY) == set(ScenarioFamily)


class TestScenarioFamilies:
    @pytest.fixture(scope="class")
    def suite(self, bench_network, bench_sim, bench_baseline):
        return scenario_suite(
            bench_network.graph,
            dataset_id=bench_network.dataset_version,
            seed=bench_network.config.seed,
            config=bench_sim,
            baseline_affected=bench_baseline,
        )

    def test_every_required_family_is_built(self, suite):
        assert {s.spec.family for s in suite} == set(ScenarioFamily)

    def test_scenarios_never_mutate_the_source_network(self, bench_network):
        """A scenario must not leave damage behind for the next one."""
        graph = bench_network.graph
        before = [(o.obligation_id, o.due_t, str(o.status)) for o in graph.obligations]
        build_scenario(
            graph,
            ScenarioSpec(family=ScenarioFamily.SUPPLIER_FAILURE),
            dataset_id=bench_network.dataset_version,
        )
        after = [(o.obligation_id, o.due_t, str(o.status)) for o in graph.obligations]
        assert before == after

    def test_delayed_inflow_shifts_a_deadline_without_removing_cash(self, suite):
        scenario = next(s for s in suite if s.spec.family is ScenarioFamily.DELAYED_INFLOW)
        assert len(scenario.mutations) == 1
        mutation = scenario.mutations[0]
        assert mutation.kind == "deadline_shift"
        assert mutation.after["due_t"] > mutation.before["due_t"]
        # The money still arrives; only the timing changed.
        assert scenario.shock.total_magnitude <= 1.0

    def test_supplier_failure_writes_off_payables(self, suite):
        scenario = next(s for s in suite if s.spec.family is ScenarioFamily.SUPPLIER_FAILURE)
        assert scenario.mutations
        assert all(m.kind == "written_off" for m in scenario.mutations)

    def test_multi_node_shock_hits_several_merchants(self, suite):
        scenario = next(s for s in suite if s.spec.family is ScenarioFamily.MULTI_NODE_SHOCK)
        assert len(scenario.shock.components) >= 2
        assert len(set(scenario.shock.origin_ids)) >= 2

    def test_mutation_families_keep_an_unperturbed_baseline_graph(self, suite):
        """Their counterfactual must be simulated on the unmutated network."""
        for scenario in suite:
            assert scenario.baseline_graph is not None
            if scenario.mutations:
                assert scenario.unperturbed_graph is not scenario.graph

    def test_scenario_ids_are_unique_and_reproducible(self, suite, bench_network):
        ids = [s.scenario_id for s in suite]
        assert len(ids) == len(set(ids))
        for scenario in suite:
            rebuilt = build_scenario(
                bench_network.graph, scenario.spec, dataset_id=bench_network.dataset_version
            )
            assert rebuilt.scenario_id == scenario.scenario_id


class TestGroundTruth:
    @pytest.fixture(scope="class")
    def truths(self, bench_network, bench_sim, bench_baseline):
        suite = scenario_suite(
            bench_network.graph,
            dataset_id=bench_network.dataset_version,
            seed=bench_network.config.seed,
            config=bench_sim,
            baseline_affected=bench_baseline,
        )
        return {
            str(s.spec.family): compute_ground_truth(
                s,
                true_edges=bench_network.ground_truth_edges,
                config=bench_sim,
                compute_optimum=False,
            )
            for s in suite
        }

    def test_most_families_produce_a_real_cascade(self, truths):
        """Severity is a sweep-level property, not a per-network guarantee.

        Whether a given shock cascades on a given random network is genuinely
        stochastic: some draws put the target in a well-capitalised
        neighbourhood that absorbs it. Demanding that all seven families bite on
        every single network would mean tuning the generator until it guarantees
        an outcome, which would make the benchmark less honest. The sweep-level
        rate is asserted by ``scripts/verify_phase2.py``; here we require that
        the majority land on this network, and that structural validity (checked
        below) holds without exception.
        """
        biting = [name for name, t in truths.items() if t.affected_nodes]
        assert len(biting) >= len(truths) // 2 + 1, (
            f"only {len(biting)}/{len(truths)} families produced a cascade: "
            f"{sorted(set(truths) - set(biting))}"
        )

    def test_a_cascade_always_carries_disrupted_volume(self, truths):
        """Whenever a family does bite, the damage must be quantified."""
        for name, truth in truths.items():
            if truth.affected_nodes:
                assert truth.disrupted_volume > 0.0, name

    def test_pure_loss_families_raise_total_disruption(self, truths):
        """Net objective change is a valid severity measure - except for delays.

        Deferring a deadline relieves the debtor of its lateness penalty at the
        same time as it starves the creditor, so DELAYED_INFLOW can legitimately
        lower total disruption while causing real harm. That is the same effect
        that makes a supplier term extension a useful *intervention*. Its
        severity is therefore asserted through the incremental measures
        (affected set, disrupted volume), not through the net objective.
        """
        for name, truth in truths.items():
            if name == str(ScenarioFamily.DELAYED_INFLOW) or not truth.affected_nodes:
                continue
            assert truth.attributable_disruption > 0.0, name

    def test_delay_stays_inside_the_horizon(self, bench_network, bench_sim, bench_baseline):
        """A deadline pushed past the horizon vanishes from the accounting."""
        from lce.benchmark.scenarios import build_scenario

        scenario = build_scenario(
            bench_network.graph,
            ScenarioSpec(family=ScenarioFamily.DELAYED_INFLOW, delay_hours=10_000.0),
            dataset_id=bench_network.dataset_version,
            baseline_affected=bench_baseline,
        )
        mutation = scenario.mutations[0]
        assert mutation.after["due_t"] < bench_sim.horizon_hours
        assert mutation.after["due_t"] > mutation.before["due_t"]

    def test_affected_sets_exclude_the_baseline(self, truths, bench_baseline):
        for name, truth in truths.items():
            assert set(truth.affected_nodes).isdisjoint(bench_baseline), name

    def test_constraint_times_are_inside_the_horizon(self, truths):
        for name, truth in truths.items():
            assert set(truth.first_constraint_t) <= set(truth.affected_nodes), name
            for t in truth.first_constraint_t.values():
                assert 0.0 <= t <= truth.horizon_hours, name

    def test_cascade_depth_is_recorded_for_affected_nodes(self, truths):
        for name, truth in truths.items():
            assert set(truth.cascade_depth) <= set(truth.affected_nodes), name
            if truth.cascade_depth:
                assert truth.max_cascade_depth == max(truth.cascade_depth.values()), name

    def test_cascade_depth_is_consistent_with_the_affected_set(self, truths):
        """Depth is only meaningful where a cascade actually happened."""
        for name, truth in truths.items():
            if not truth.affected_nodes:
                assert truth.max_cascade_depth == 0, name
            assert truth.max_cascade_depth >= 0, name

    def test_ground_truth_records_the_shock_source(self, truths):
        for name, truth in truths.items():
            assert truth.shock_source, name
            assert truth.shock_magnitude > 0, name

    def test_latent_edges_are_recorded(self, truths, bench_network):
        truth = next(iter(truths.values()))
        assert len(truth.true_edges) == len(bench_network.ground_truth_edges)
        assert truth.true_dependency_strength()
        assert truth.true_lag_hours()

    def test_liquidity_states_cover_every_merchant(self, truths, bench_network):
        truth = next(iter(truths.values()))
        assert set(truth.liquidity_states) == set(bench_network.graph.merchant_ids)

    def test_observable_graph_hides_the_dependency_overlay(self, truths):
        """The model must infer dependencies, never read them off."""
        truth = next(iter(truths.values()))
        observable = truth.observable_graph()
        assert observable.dependency_edges == []
        assert observable.stats().n_payment_events > 0
        assert observable.stats().n_obligations > 0

    def test_true_optimum_is_feasible_when_computed(self, bench_network, bench_sim):
        scenario = build_scenario(
            bench_network.graph,
            ScenarioSpec(family=ScenarioFamily.LIQUIDITY_DRAIN, magnitude=2.5),
            dataset_id=bench_network.dataset_version,
        )
        truth = compute_ground_truth(
            scenario,
            true_edges=bench_network.ground_truth_edges,
            config=bench_sim,
            compute_optimum=True,
            max_actions=2,
        )
        optimum = truth.optimal_intervention
        if not optimum.available:
            pytest.skip(f"no exact optimum available: {optimum.reason}")
        assert len(optimum.interventions) <= 2
        assert optimum.cost >= 0.0
        assert (optimum.disruption_prevented or 0.0) >= -1e-6
        assert truth.feasible_interventions


class TestValidation:
    def test_full_battery_passes_on_a_generated_dataset(self, bench_network, bench_sim):
        report = validate_dataset(bench_network, config=bench_sim, deep=True)
        assert report.passed, [c.to_dict() for c in report.failures]

    def test_flow_consistency_is_checked(self, bench_network, bench_sim):
        report = validate_dataset(bench_network, config=bench_sim, deep=False)
        names = {c.name for c in report.checks}
        assert "flow_consistency" in names
        assert "no_negative_opening_balances" in names

    def test_deep_battery_covers_the_dynamical_invariants(self, bench_network, bench_sim):
        report = validate_dataset(bench_network, config=bench_sim, deep=True)
        names = {c.name for c in report.checks}
        assert "causal_shock_does_not_reduce_disruption" in names
        assert "disruption_monotone_in_shock_magnitude" in names
        assert "intervention_conserves_principal" in names
        assert "ground_truth_optimum_feasible" in names

    def test_diagnostics_describe_structure_and_distribution(self, bench_network):
        diagnostics = compute_diagnostics(bench_network)
        assert diagnostics["n_merchants"] == len(bench_network.graph)
        assert diagnostics["amount"]["max_over_p50"] > 1.0
        assert diagnostics["buffer_gini"] > 0.0
        assert diagnostics["n_sectors"] >= 2
        assert diagnostics["max_downstream_reach"] >= 1
        assert 0.0 <= diagnostics["amount_gini"] <= 1.0

    def test_restructuring_conserves_principal(self, bench_network, bench_sim):
        """An intervention may move money in time, never create it."""
        from lce.benchmark.validation import check_intervention_conservation

        check = check_intervention_conservation(bench_network, config=bench_sim)
        assert check.passed, check.detail


class TestExport:
    @pytest.fixture
    def exported(self, tmp_path, bench_network, bench_sim, bench_baseline):
        result = export_dataset(bench_network, tmp_path, fmt="parquet", scale="small")
        suite = scenario_suite(
            bench_network.graph,
            dataset_id=bench_network.dataset_version,
            seed=bench_network.config.seed,
            config=bench_sim,
            baseline_affected=bench_baseline,
        )
        scenario = suite[0]
        truth = compute_ground_truth(
            scenario,
            true_edges=bench_network.ground_truth_edges,
            config=bench_sim,
            compute_optimum=False,
        )
        export_scenario(result.directory, scenario, truth)
        return result, scenario

    def test_export_writes_every_table(self, exported):
        result, _ = exported
        assert set(result.rows) >= {"merchants", "payments", "obligations", "dependency_edges"}
        assert result.rows["merchants"] > 0
        assert result.rows["payments"] > 0

    def test_load_withholds_ground_truth_by_default(self, exported, bench_network):
        """A loaded dataset must not hand the model the latent edges."""
        result, _ = exported
        graph, manifest = load_dataset(result.directory)
        assert graph.dependency_edges == []
        assert len(graph) == len(bench_network.graph)
        assert manifest.dataset_id == bench_network.dataset_version

    def test_load_can_opt_into_ground_truth(self, exported, bench_network):
        result, _ = exported
        graph, _ = load_dataset(result.directory, with_ground_truth=True)
        assert len(graph.dependency_edges) == len(bench_network.ground_truth_edges)

    def test_round_trip_preserves_the_event_stream(self, exported, bench_network):
        result, _ = exported
        graph, _ = load_dataset(result.directory)
        assert (
            graph.stats().n_payment_events
            == bench_network.graph.stats().n_payment_events
        )
        assert graph.stats().n_obligations == bench_network.graph.stats().n_obligations

    def test_csv_export_round_trips(self, tmp_path, bench_network):
        result = export_dataset(bench_network, tmp_path, fmt="csv", scale="small")
        graph, _ = load_dataset(result.directory)
        assert len(graph) == len(bench_network.graph)

    def test_streaming_export_matches_the_buffered_one(self, tmp_path, bench_network):
        streamed = export_dataset(
            bench_network, tmp_path / "s", fmt="parquet", streaming=True
        )
        buffered = export_dataset(
            bench_network, tmp_path / "b", fmt="parquet", streaming=False
        )
        assert streamed.rows["payments"] == buffered.rows["payments"]

    def test_scenarios_are_listed_and_replay_deterministically(self, exported):
        result, scenario = exported
        assert scenario.scenario_id in list_scenarios(result.directory)
        replay = replay_scenario(result.directory, scenario.scenario_id)
        assert replay["reproduced"]
        assert replay["dataset_matches"]
        assert replay["scenario_id_matches"]
        assert replay["affected_matches"]
        assert replay["depth_matches"]

    def test_missing_dataset_raises(self, tmp_path):
        with pytest.raises(NotFoundError):
            load_dataset(tmp_path / "absent")


class TestReproducibility:
    def test_identical_config_gives_identical_entity_ids(self):
        """Ids must be seed-derived: the simulator keys its CRN on them."""
        config = scale_config(BenchmarkScale.SMALL, seed=1234, overrides={"n_merchants": 30})
        first, second = generate_network(config), generate_network(config)
        assert [o.obligation_id for o in first.graph.obligations] == [
            o.obligation_id for o in second.graph.obligations
        ]
        assert [e.event_id for e in first.graph.payment_events] == [
            e.event_id for e in second.graph.payment_events
        ]

    def test_identical_config_gives_identical_simulation(self):
        config = scale_config(BenchmarkScale.SMALL, seed=1234, overrides={"n_merchants": 30})
        sim = SimulationConfig(horizon_hours=config.horizon_hours, seed=config.seed)
        a = LiquiditySimulator(generate_network(config).graph, sim).run(None, run_id="x")
        b = LiquiditySimulator(generate_network(config).graph, sim).run(None, run_id="y")
        assert a.disruption == b.disruption
        assert a.affected_ids == b.affected_ids

    def test_a_different_seed_changes_the_dataset(self):
        a = scale_config(BenchmarkScale.SMALL, seed=1)
        b = scale_config(BenchmarkScale.SMALL, seed=2)
        assert a.dataset_version != b.dataset_version

    def test_generator_version_is_part_of_the_dataset_identity(self):
        config = GeneratorConfig(n_merchants=20, seed=5)
        assert config.to_dict()["generator_version"] == GENERATOR_VERSION
