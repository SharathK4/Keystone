"""Inference tests: the artifact contract, the forward pass, and the API.

The load path is where a serving system fails silently. A model fitted on one
feature schema will happily produce numbers from another, and those numbers look
entirely reasonable - so most of what follows is about the loader *refusing*
things: a tampered weight file, a schema it was not fitted on, a format it does
not understand, a version that does not exist.

The other half is the separation claim. A test asserts that importing the
inference package does not drag in the dataset generator or the training
modules, because that is the difference between "we exported a model" and "the
frontend can actually run it".
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from lce.errors import ModelError, NotFoundError
from lce.inference.artifact import (
    ARTIFACT_FORMAT_VERSION,
    list_artifacts,
    load_artifact,
    resolve_artifact,
    save_artifact,
)
from lce.inference.predictor import (
    HazardPredictor,
    NetworkState,
    apply_calibrator,
    build_request_window,
    merchant_from_payload,
)
from lce.inference.service import (
    InferenceService,
    reset_service,
    shock_from_components,
)
from lce.learning.features import (
    FEATURE_SCHEMA_VERSION,
    INTERVAL_FEATURE_DIM,
    NODE_FEATURE_DIM,
    NODE_FEATURE_NAMES,
)

N_INTERVALS = 8
#: static node features + time-varying features + interval one-hot + log width + intercept
DESIGN_WIDTH = NODE_FEATURE_DIM + INTERVAL_FEATURE_DIM + N_INTERVALS + 1 + 1


# --------------------------------------------------------------------- fixtures


def _write_artifact(directory: Path, **overrides) -> Path:
    """A small, valid bundle. Weights are hand-made, which is the point: the
    loader's contract is about the *file*, not about how it was fitted."""
    rng = np.random.default_rng(7)
    payload = {
        "name": "test_hazard",
        "model_version": "hazard-test-0001",
        "model_kind": "discrete_hazard",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "weights": rng.normal(scale=0.05, size=DESIGN_WIDTH),
        "mean": np.zeros(DESIGN_WIDTH - 1),
        "scale": np.ones(DESIGN_WIDTH - 1),
        "n_hazard_intervals": N_INTERVALS,
        "calibrator": {"calibrator": "identity"},
        "threshold": 0.5,
        "node_feature_names": list(NODE_FEATURE_NAMES),
        "dataset_version": "synth-test",
        "seeds": [1, 2, 3],
        "metrics": {"pr_auc": 0.5},
    }
    payload.update(overrides)
    return save_artifact(directory, **payload)


@pytest.fixture
def artifact_dir(tmp_path) -> Path:
    return _write_artifact(tmp_path / "artifacts" / "test_hazard")


@pytest.fixture
def tiny_state() -> tuple[NetworkState, object]:
    """A three-merchant chain with a book and some history."""
    from lce.domain.events import Obligation, PaymentEvent

    merchants = [
        merchant_from_payload(
            {
                "merchant_id": f"m{i}",
                "opening_balance": 100_000.0 * (i + 1),
                "credit_limit": 20_000.0,
                "operating_floor": 5_000.0,
            }
        )
        for i in range(3)
    ]
    obligations = [
        Obligation(
            obligation_id=f"obl_{i}",
            debtor_id=f"m{i}",
            creditor_id=f"m{i + 1}",
            amount=60_000.0,
            issued_t=-48.0,
            due_t=24.0 + 12.0 * i,
        )
        for i in range(2)
    ]
    payments = [
        PaymentEvent(payer_id="m0", payee_id="m1", amount=30_000.0, t=-30.0 + 3.0 * k)
        for k in range(6)
    ] + [
        PaymentEvent(payer_id="m1", payee_id="m2", amount=20_000.0, t=-28.0 + 3.0 * k)
        for k in range(6)
    ]
    state = NetworkState(
        network_id="req", merchants=merchants, obligations=obligations, payments=payments
    )
    shock = shock_from_components([{"merchant_id": "m0", "magnitude": 90_000.0, "t": 0.0}])
    return state, shock


# ---------------------------------------------------------------- the artifact


class TestArtifact:
    def test_round_trip(self, artifact_dir):
        artifact = load_artifact(artifact_dir, expected_schema=FEATURE_SCHEMA_VERSION)
        assert artifact.manifest.model_version == "hazard-test-0001"
        assert artifact.weights.shape == (DESIGN_WIDTH,)
        assert artifact.manifest.format_version == ARTIFACT_FORMAT_VERSION
        assert artifact.manifest.content_hash

    def test_integrity_failure_is_refused(self, artifact_dir):
        """A weight file that no longer matches its hash must not be served."""
        weights = artifact_dir / "weights.npz"
        blob = bytearray(weights.read_bytes())
        blob[-1] = (blob[-1] + 1) % 256
        weights.write_bytes(bytes(blob))
        with pytest.raises(ModelError, match="integrity"):
            load_artifact(artifact_dir)

    def test_schema_mismatch_is_refused(self, tmp_path):
        directory = _write_artifact(
            tmp_path / "a", feature_schema_version="something-else-v9"
        )
        with pytest.raises(ModelError, match="feature schema mismatch"):
            load_artifact(directory, expected_schema=FEATURE_SCHEMA_VERSION)

    def test_unknown_model_kind_is_refused(self, tmp_path):
        directory = _write_artifact(tmp_path / "a", model_kind="mystery_net")
        with pytest.raises(ModelError, match="cannot be served"):
            load_artifact(directory)

    def test_unsupported_format_is_refused(self, tmp_path):
        directory = _write_artifact(tmp_path / "a")
        manifest_path = directory / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["format_version"] = ARTIFACT_FORMAT_VERSION + 5
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ModelError, match="format"):
            load_artifact(directory)

    def test_missing_bundle_is_reported(self, tmp_path):
        with pytest.raises(NotFoundError):
            load_artifact(tmp_path / "nothing")

    def test_version_resolution(self, tmp_path):
        root = tmp_path / "artifacts"
        _write_artifact(root / "one", model_version="v-one")
        _write_artifact(root / "two", model_version="v-two")
        assert len(list_artifacts(root)) == 2
        assert resolve_artifact(root, "v-one").name == "one"
        with pytest.raises(NotFoundError, match="no artifact with version"):
            resolve_artifact(root, "v-missing")

    def test_resolution_needs_something_to_resolve(self, tmp_path):
        with pytest.raises(NotFoundError, match="no model artifacts"):
            resolve_artifact(tmp_path / "empty")

    def test_isotonic_knots_survive_the_round_trip(self, tmp_path):
        directory = _write_artifact(
            tmp_path / "a",
            calibrator={"calibrator": "isotonic", "n_knots": 3},
            calibrator_knots=(np.array([0.0, 0.5, 1.0]), np.array([0.0, 0.2, 0.9])),
        )
        artifact = load_artifact(directory)
        assert artifact.calibrator_x is not None
        mapped = apply_calibrator(np.array([0.25, 0.75]), artifact)
        assert np.all(np.diff(mapped) >= 0)


# ---------------------------------------------------------------- the forward pass


class TestPredictor:
    def test_prediction_shape_and_monotonicity(self, artifact_dir, tiny_state):
        state, shock = tiny_state
        predictor = HazardPredictor(load_artifact(artifact_dir))
        window = build_request_window(
            state, shock=shock, observation_cutoff=0.0, horizon_hours=168.0
        )
        prediction = predictor.predict(window)

        assert len(prediction.nodes) == 3
        for node in prediction.nodes:
            assert 0.0 <= node.probability_constrained <= 1.0
            assert node.expected_time_to_constraint_hours > 0.0
            curve = [node.probability_by[k] for k in node.probability_by]
            assert all(0.0 <= v <= 1.0 for v in curve)

    def test_horizon_slices_are_non_decreasing(self, artifact_dir, tiny_state):
        state, shock = tiny_state
        predictor = HazardPredictor(load_artifact(artifact_dir))
        window = build_request_window(
            state, shock=shock, observation_cutoff=0.0, horizon_hours=168.0
        )
        prediction = predictor.predict(window, horizon_grid=(6.0, 24.0, 72.0, 168.0))
        for node in prediction.nodes:
            values = [node.probability_by[f"{t:.0f}h"] for t in (6.0, 24.0, 72.0, 168.0)]
            assert all(b >= a - 1e-9 for a, b in itertools.pairwise(values))

    def test_the_cutoff_is_enforced_server_side(self, artifact_dir, tiny_state):
        """Posting the future must not change the answer."""
        from lce.domain.events import PaymentEvent

        state, shock = tiny_state
        predictor = HazardPredictor(load_artifact(artifact_dir))
        clean = predictor.predict(
            build_request_window(state, shock=shock, observation_cutoff=0.0, horizon_hours=168.0)
        )

        contaminated = NetworkState(
            network_id=state.network_id,
            merchants=state.merchants,
            obligations=state.obligations,
            payments=[
                *state.payments,
                PaymentEvent(payer_id="m0", payee_id="m1", amount=999_999.0, t=40.0),
            ],
        )
        after = predictor.predict(
            build_request_window(
                contaminated, shock=shock, observation_cutoff=0.0, horizon_hours=168.0
            )
        )
        assert clean.scores() == after.scores()

    def test_a_wider_design_is_refused(self, tmp_path, tiny_state):
        state, shock = tiny_state
        directory = _write_artifact(
            tmp_path / "a", weights=np.zeros(DESIGN_WIDTH + 3),
            mean=np.zeros(DESIGN_WIDTH + 2), scale=np.ones(DESIGN_WIDTH + 2),
        )
        predictor = HazardPredictor(load_artifact(directory))
        with pytest.raises(ModelError, match="design width"):
            predictor.predict(
                build_request_window(
                    state, shock=shock, observation_cutoff=0.0, horizon_hours=168.0
                )
            )

    def test_an_empty_window_is_rejected(self, tiny_state):
        from lce.errors import ValidationError

        state, shock = tiny_state
        with pytest.raises(ValidationError, match="observation cutoff"):
            build_request_window(
                state, shock=shock, observation_cutoff=200.0, horizon_hours=168.0
            )

    def test_latent_fields_are_not_accepted_from_a_request(self):
        """A caller cannot feed the model a column it was never trained on."""
        profile = merchant_from_payload(
            {
                "merchant_id": "m9",
                "opening_balance": 1000.0,
                "exogenous_inflow_rate": 5_000_000.0,
                "payment_discipline": 0.01,
            }
        )
        assert profile.exogenous_inflow_rate == 0.0
        assert profile.payment_discipline == 0.9


# -------------------------------------------------------------------- service


class TestService:
    def test_health_reports_the_loaded_artifact(self, artifact_dir):
        service = InferenceService(artifact_dir.parent)
        health = service.health()
        assert health["status"] == "ok"
        assert health["model_version"] == "hazard-test-0001"
        assert health["feature_schema_version"] == FEATURE_SCHEMA_VERSION

    def test_repeated_inference_is_identical(self, artifact_dir, tiny_state):
        state, shock = tiny_state
        service = InferenceService(artifact_dir.parent)
        first, _ = service.predict_contagion(
            state, shock, observation_cutoff=0.0, horizon_hours=168.0
        )
        second, _ = service.predict_contagion(
            state, shock, observation_cutoff=0.0, horizon_hours=168.0
        )
        assert first.scores() == second.scores()
        assert first.hit_times() == second.hit_times()

    def test_replay_returns_before_and_after(self, artifact_dir, tiny_state):
        from lce.domain.enums import InterventionType
        from lce.domain.intervention import Intervention

        state, shock = tiny_state
        service = InferenceService(artifact_dir.parent)
        action = Intervention(
            type=InterventionType.LIQUIDITY_INJECTION,
            merchant_id="m0", t=0.0, amount=90_000.0,
        )
        result = service.replay(state, shock, [action], horizon_hours=168.0)
        assert set(result) >= {"before", "after", "disruption_prevented", "cost"}
        assert result["cost"] == pytest.approx(90_000.0)
        assert result["n_interventions"] == 1

    def test_replay_with_no_action_prevents_nothing(self, artifact_dir, tiny_state):
        state, shock = tiny_state
        service = InferenceService(artifact_dir.parent)
        result = service.replay(state, shock, [], horizon_hours=168.0)
        assert result["disruption_prevented"] == pytest.approx(0.0)
        assert result["cost"] == 0.0

    def test_recommendation_is_simulated_not_predicted(self, artifact_dir, tiny_state):
        state, shock = tiny_state
        service = InferenceService(artifact_dir.parent)
        recommendation = service.recommend(
            state, shock, horizon_hours=168.0, max_candidates=4
        )
        # Whatever it chose, the reported reduction must equal a real replay.
        replayed = service.replay(
            state, shock, recommendation.selected, horizon_hours=168.0
        )
        assert recommendation.expected_disruption_reduction == pytest.approx(
            replayed["disruption_prevented"], rel=1e-6, abs=1e-6
        )

    def test_recommendation_is_deterministic(self, artifact_dir, tiny_state):
        state, shock = tiny_state
        service = InferenceService(artifact_dir.parent)
        first = service.recommend(state, shock, horizon_hours=168.0, max_candidates=4)
        second = service.recommend(state, shock, horizon_hours=168.0, max_candidates=4)
        assert [u.merchant_id for u in first.selected] == [
            u.merchant_id for u in second.selected
        ]
        assert first.cost == pytest.approx(second.cost)

    def test_malformed_shock_is_rejected(self):
        from lce.errors import ValidationError

        with pytest.raises(ValidationError, match="at least one component"):
            shock_from_components([])
        with pytest.raises(ValidationError, match="malformed"):
            shock_from_components([{"magnitude": 1.0}])


# ------------------------------------------------------------------- the API


@pytest.fixture
def served_client(tmp_path, monkeypatch):
    """An app whose inference service is backed by a throwaway artifact."""
    from fastapi.testclient import TestClient

    from lce.api.app import create_app
    from lce.inference import service as service_module

    root = tmp_path / "artifacts"
    _write_artifact(root / "test_hazard")
    reset_service()
    monkeypatch.setattr(
        service_module, "get_service", lambda *a, **k: InferenceService(root)
    )
    with TestClient(create_app()) as client:
        yield client
    reset_service()


def _request_payload() -> dict:
    return {
        "network": {
            "network_id": "req",
            "merchants": [
                {"merchant_id": "m0", "opening_balance": 100000.0, "credit_limit": 20000.0},
                {"merchant_id": "m1", "opening_balance": 200000.0, "credit_limit": 20000.0},
                {"merchant_id": "m2", "opening_balance": 300000.0, "credit_limit": 20000.0},
            ],
            "obligations": [
                {
                    "obligation_id": "obl_0", "debtor_id": "m0", "creditor_id": "m1",
                    "amount": 60000.0, "issued_t": -48.0, "due_t": 24.0,
                },
                {
                    "obligation_id": "obl_1", "debtor_id": "m1", "creditor_id": "m2",
                    "amount": 60000.0, "issued_t": -48.0, "due_t": 36.0,
                },
            ],
            "payments": [
                {"payer_id": "m0", "payee_id": "m1", "amount": 30000.0, "t": -30.0},
                {"payer_id": "m1", "payee_id": "m2", "amount": 20000.0, "t": -28.0},
            ],
        },
        "shock": {"components": [{"merchant_id": "m0", "magnitude": 90000.0, "t": 0.0}]},
        "observation_cutoff": 0.0,
        "horizon_hours": 168.0,
    }


class TestInferenceAPI:
    def test_model_endpoint(self, served_client):
        response = served_client.get("/api/v1/model")
        assert response.status_code == 200
        assert response.json()["model_version"] == "hazard-test-0001"

    def test_predict_contagion(self, served_client):
        response = served_client.post("/api/v1/predict/contagion", json=_request_payload())
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["nodes"]) == 3
        assert body["feature_schema_version"] == FEATURE_SCHEMA_VERSION
        assert body["latency_ms"] >= 0.0
        for node in body["nodes"]:
            assert 0.0 <= node["probability_constrained"] <= 1.0

    def test_predict_is_repeatable(self, served_client):
        payload = _request_payload()
        first = served_client.post("/api/v1/predict/contagion", json=payload).json()
        second = served_client.post("/api/v1/predict/contagion", json=payload).json()
        assert [n["probability_constrained"] for n in first["nodes"]] == [
            n["probability_constrained"] for n in second["nodes"]
        ]

    def test_recommend(self, served_client):
        payload = _request_payload() | {
            "constraints": {"max_actions": 1, "budget": 500000.0},
            "max_candidates": 4,
        }
        response = served_client.post("/api/v1/interventions/recommend", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert "selected" in body
        assert body["feasibility"]["feasible"] is True
        assert body["candidates"]["n_generated"] >= 0
        for entry in body["ranked"]:
            assert set(entry["factors"])

    def test_replay(self, served_client):
        payload = _request_payload()
        payload["interventions"] = [
            {"type": "liquidity_injection", "merchant_id": "m0", "t": 0.0, "amount": 90000.0}
        ]
        payload.pop("observation_cutoff")
        response = served_client.post("/api/v1/scenarios/replay", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["n_interventions"] == 1
        assert body["cost"] == pytest.approx(90000.0)

    def test_malformed_request_is_rejected(self, served_client):
        response = served_client.post("/api/v1/predict/contagion", json={"network": {}})
        assert response.status_code == 422
        assert response.json()["code"] == "request_validation_error"

    def test_unknown_field_is_rejected(self, served_client):
        payload = _request_payload()
        payload["network"]["merchants"][0]["payment_discipline"] = 0.1
        response = served_client.post("/api/v1/predict/contagion", json=payload)
        assert response.status_code == 422

    def test_empty_network_is_rejected(self, served_client):
        payload = _request_payload()
        payload["network"]["merchants"] = []
        assert served_client.post("/api/v1/predict/contagion", json=payload).status_code == 422

    def test_horizon_before_cutoff_is_rejected(self, served_client):
        payload = _request_payload() | {"observation_cutoff": 200.0, "horizon_hours": 100.0}
        assert served_client.post("/api/v1/predict/contagion", json=payload).status_code == 422


# ------------------------------------------------------- the separation claim


class TestTrainingIsNotOnTheServingPath:
    def test_importing_inference_pulls_in_no_training_code(self):
        """A clean process must load the serving package without the generator."""
        code = (
            "import sys, json;"
            "import lce.inference;"
            "import lce.inference.service;"
            "forbidden = ('lce.data.generator','lce.benchmark','lce.learning.dataset',"
            "'lce.learning.baselines','lce.learning.experiment','lce.intervention.experiment');"
            "print(json.dumps(sorted(m for m in sys.modules "
            "if any(m == f or m.startswith(f + '.') for f in forbidden))))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
            env={**dict(__import__("os").environ), "PYTHONPATH": "src"},
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip()) == []

    def test_importing_the_whole_serving_stack_pulls_in_no_training_code(self):
        """Not just the predictor: the API a frontend actually talks to.

        The narrower check above passed while ``lce.api.app`` still dragged the
        dataset generator in through three separate package ``__init__``
        re-exports. Asserting on the module a request actually enters is the
        only version of this claim that means anything.
        """
        code = (
            "import sys, json;"
            "import lce.snapshot.store;"
            "import lce.snapshot.dashboard;"
            "import lce.api.routers.analytics;"
            "import lce.api.app;"
            "forbidden = ('lce.data.generator','lce.benchmark','lce.learning.dataset',"
            "'lce.learning.baselines','lce.learning.experiment','lce.intervention.experiment');"
            "print(json.dumps(sorted(m for m in sys.modules "
            "if any(m == f or m.startswith(f + '.') for f in forbidden))))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
            env={**dict(__import__("os").environ), "PYTHONPATH": "src", "LCE_LOG_LEVEL": "ERROR"},
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip().splitlines()[-1]) == []

    def test_a_clean_process_can_load_and_serve_an_artifact(self, tmp_path):
        """The end of the packaging claim: artifact in, prediction out, no trainer."""
        root = tmp_path / "artifacts"
        _write_artifact(root / "test_hazard")
        code = f"""
import json, sys
from lce.inference.service import InferenceService
from lce.inference.predictor import NetworkState, merchant_from_payload
from lce.inference.service import shock_from_components
from lce.domain.events import Obligation, PaymentEvent

service = InferenceService({str(root)!r})
state = NetworkState(
    network_id="clean",
    merchants=[merchant_from_payload({{"merchant_id": f"m{{i}}", "opening_balance": 100000.0}}) for i in range(3)],
    obligations=[Obligation(obligation_id="o0", debtor_id="m0", creditor_id="m1",
                            amount=50000.0, issued_t=-24.0, due_t=24.0)],
    payments=[PaymentEvent(payer_id="m0", payee_id="m1", amount=10000.0, t=-12.0)],
)
shock = shock_from_components([{{"merchant_id": "m0", "magnitude": 40000.0, "t": 0.0}}])
prediction, _ = service.predict_contagion(state, shock, observation_cutoff=0.0, horizon_hours=168.0)
forbidden = ("lce.data.generator", "lce.benchmark", "lce.learning.dataset")
leaked = sorted(m for m in sys.modules if any(m == f or m.startswith(f + ".") for f in forbidden))
print(json.dumps({{"n_nodes": len(prediction.nodes), "leaked": leaked,
                   "model_version": prediction.model_version}}))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
            env={
                **dict(__import__("os").environ),
                "PYTHONPATH": "src",
                "LCE_LOG_LEVEL": "ERROR",
            },
        )
        assert result.returncode == 0, result.stderr[-3000:]
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["n_nodes"] == 3
        assert payload["leaked"] == []
        assert payload["model_version"] == "hazard-test-0001"


# ---------------------------------------------------------- train, save, serve


@pytest.mark.slow
def test_train_export_serve_round_trip(tmp_path):
    """The full packaging path: fit a real model, export it, serve a request."""
    from lce.inference.export import export_hazard_model
    from lce.learning.baselines import DiscreteTimeHazard
    from lce.learning.dataset import build_corpus
    from lce.learning.experiment import fit_and_calibrate
    from lce.learning.splits import SplitSpec, assert_split_clean, make_temporal_split

    overrides = {
        "n_merchants": 22,
        "n_layers": 3,
        "history_hours": 15 * 24.0,
        "horizon_hours": 168.0,
    }
    corpus = build_corpus([61, 62, 63, 64], overrides=overrides, audit=False)
    split = make_temporal_split(corpus, SplitSpec(0.5, 0.25))
    assert_split_clean(corpus, split)

    fitted = fit_and_calibrate(
        "discrete_hazard",
        DiscreteTimeHazard(corpus.task),
        split.examples(corpus, "train"),
        split.examples(corpus, "validation"),
    )
    directory = export_hazard_model(
        fitted.model,
        tmp_path / "artifacts" / "trained",
        calibrator=fitted.calibrator,
        threshold=fitted.threshold,
        dataset_version=corpus.dataset_ids()[0],
        seeds=[61, 62, 63, 64],
    )

    service = InferenceService(directory.parent)
    assert service.model_version == fitted.model.model_version

    example = split.examples(corpus, "test")[0]
    assert example.window is not None
    prediction = service.predictor.predict(example.window)
    assert len(prediction.nodes) == example.n_merchants

    # The served forward pass must agree with the trained model on the same
    # example - that is what makes the exported artifact the same model.
    trained = fitted.model.predict(example)
    served = np.array([n.probability_constrained for n in prediction.nodes])
    raw = fitted.calibrator.transform(trained.score)
    assert np.allclose(served, raw, atol=1e-9)
