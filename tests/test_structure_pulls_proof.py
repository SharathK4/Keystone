"""The pulled-stone and proof artifacts behind Folios II and V.

Both are content-addressed and verified on load; a slice of the pulls is
rebuilt and must equal the committed records; every claim in the proof's
"what failed" block is checked against the artifacts it summarises.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lce.structure.pulls import build_pulls
from lce.structure.store import STRUCTURE_ROOT, ProofStore, PullsStore, SpeedStore, resolve_artifact

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "artifacts" / "snapshots" / "snap-a4c99d5a49a64ffd"
FORBIDDEN_FIELD_TOKENS = ("tick", "timeline", "cursor", "playing", "play_state", "is_paused",
                          "current_time", "step_index", "frame")


@pytest.fixture(scope="module")
def pulls() -> PullsStore:
    return PullsStore(resolve_artifact(STRUCTURE_ROOT, "pulls"))


@pytest.fixture(scope="module")
def proof() -> ProofStore:
    return ProofStore(resolve_artifact(STRUCTURE_ROOT, "proof"))


def _walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield f"{path}.{k}", k, v
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


class TestPulls:
    def test_every_merchant_is_pulled_once(self, pulls):
        snapshot = json.loads((SNAPSHOT / "snapshot.json").read_text(encoding="utf-8"))
        ids = sorted(m["merchant_id"] for m in snapshot["merchants"])
        assert [r.merchant_id for r in pulls.view.records] == ids
        assert pulls.view.provenance.snapshot_id == "snap-a4c99d5a49a64ffd"

    def test_the_range_brackets_the_central_figure(self, pulls):
        for r in pulls.view.records:
            lo, hi = r.range.n_affected
            assert lo <= r.n_affected <= hi, r.merchant_id
            dlo, dhi = r.range.disrupted_value_inr
            assert dlo - 1e-6 <= r.disrupted_value_inr <= dhi + 1e-6, r.merchant_id
            if r.first_constraint_hours is not None:
                flo, fhi = r.range.first_constraint_hours
                assert flo <= r.first_constraint_hours <= fhi, r.merchant_id
            assert r.n_affected == len(r.affected)

    def test_a_rebuilt_slice_equals_the_committed_records(self, pulls):
        ids = ["m0013", "m0070"]
        rebuilt = build_pulls(snapshot_dir=SNAPSHOT, merchant_ids=ids)["records"]
        committed = [json.loads(pulls.record(i).model_dump_json()) for i in ids]
        assert rebuilt == committed

    def test_a_tampered_artifact_is_refused(self, pulls, tmp_path):
        import shutil

        from lce.errors import ValidationError

        copy = tmp_path / pulls.artifact_id
        shutil.copytree(Path(STRUCTURE_ROOT) / pulls.artifact_id, copy)
        text = (copy / "pulls.json").read_text(encoding="utf-8")
        (copy / "pulls.json").write_text(text.replace('"n_affected": 3', '"n_affected": 4', 1),
                                         encoding="utf-8")
        with pytest.raises(ValidationError):
            PullsStore(copy)


class TestProof:
    def test_the_barrier_holds_in_every_scenario(self, proof):
        assert len(proof.view.leakage) == 7
        for row in proof.view.leakage:
            assert row.holds and row.last_observed_hours < row.onset_hours

    def test_the_audit_passed_and_names_no_legacy_provider(self, proof):
        assert proof.view.audit and all(a.passed for a in proof.view.audit)
        assert not any("razorpay" in a.name for a in proof.view.audit)

    def test_recovery_scores_strengths_with_intervals(self, proof):
        params = {p.name: p for p in proof.view.recovery.parameters}
        assert set(params) == {"conditional_probability", "pass_through", "lag_mean_hours"}
        for p in params.values():
            assert p.low <= p.rank_correlation <= p.high
            assert p.n_edges == proof.view.recovery.n_matched

    def test_what_failed_agrees_with_the_artifacts(self, proof, pulls):
        failed = {f.title: f.detail for f in proof.view.failed}
        depths = [max((a.cascade_depth or 0) for a in r.affected) if r.affected else 0
                  for r in pulls.view.records]
        cascade = failed["Shocks barely cascade on this network"]
        assert f"{sum(1 for d in depths if d == 1)} constrain another merchant one hop away" in cascade
        speed = SpeedStore(resolve_artifact(STRUCTURE_ROOT, "speed"))
        no_plan = sum(1 for s in speed.scenarios() if not s.has_plan)
        assert failed["Some benchmark scenarios have no plan at all"].startswith(
            f"{no_plan} of {len(speed.scenarios())}")
        worst = min(proof.view.recovery.parameters, key=lambda p: p.rank_correlation)
        assert f"{worst.rank_correlation:.2f}" in failed["Links are observed; only their strengths are inferred"]

    def test_results_carry_their_intervals(self, proof):
        for r in proof.view.results:
            if r.low is not None and r.high is not None:
                assert r.low - 1e-9 <= r.value <= r.high + 1e-9, r.label


class TestServing:
    def test_routes(self):
        from lce.api.app import create_app

        with TestClient(create_app()) as client:
            body = client.get("/api/v1/structure/pulls").json()
            assert len(body["records"]) == 100
            one = client.get("/api/v1/structure/pulls/m0013")
            assert one.status_code == 200 and one.json()["merchant_id"] == "m0013"
            assert client.get("/api/v1/structure/pulls/m9999").status_code == 404
            proof = client.get("/api/v1/structure/proof").json()
            assert proof["label"] == "Replayed offline, inside the synthetic generator"

    def test_no_simulator_vocabulary(self, pulls, proof):
        payload = {"pulls": pulls.view.model_dump(mode="json"), "proof": proof.view.model_dump(mode="json")}
        assert [p for p, k, _ in _walk(payload) if any(t in k.lower() for t in FORBIDDEN_FIELD_TOKENS)] == []
        words = re.compile(r"simulat|engine|risk score|ai-powered", re.I)
        assert [p for p, _k, v in _walk(payload) if isinstance(v, str) and words.search(v)] == []
