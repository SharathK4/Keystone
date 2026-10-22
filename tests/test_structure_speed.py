"""Gate H - the value-of-speed artifact.

The committed artifact is checked against the committed snapshot; a slice of it
is rebuilt from scratch and must come back byte-for-byte; serving must not
import anything that builds.
"""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lce.benchmark.scenarios import ScenarioFamily
from lce.errors import ValidationError
from lce.structure.build import (
    _snapshot_reference,
    build_speed,
    check_against_snapshot,
    content_hash,
)
from lce.structure.store import STRUCTURE_ROOT, SpeedStore, resolve_speed

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "artifacts" / "snapshots" / "snap-a4c99d5a49a64ffd"
SINGLE = "scn-07c647ec1459"
FORBIDDEN_FIELD_TOKENS = ("tick", "timeline", "cursor", "playing", "play_state", "is_paused",
                          "current_time", "step_index", "frame")


@pytest.fixture(scope="module")
def store() -> SpeedStore:
    return SpeedStore(resolve_speed(STRUCTURE_ROOT))


def _walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield f"{path}.{k}", k, v
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


class TestArtifact:
    def test_it_loads_and_matches_its_content_hash(self, store):
        assert store.artifact_id == f"speed-{store.content_hash[:16]}"
        assert store.provenance.snapshot_id == "snap-a4c99d5a49a64ffd"
        assert store.provenance.kind == "replayed_offline"
        assert store.provenance.delays_hours == [0.0, 1.0, 6.0, 12.0, 24.0, 48.0, 72.0]

    def test_it_covers_the_fourteen_scenario_benchmark(self, store):
        scenarios = store.scenarios()
        assert sorted({s.seed for s in scenarios}) == [7, 2025]
        assert len(scenarios) == 14
        assert sum(s.in_snapshot for s in scenarios) == 7

    def test_zero_delay_reproduces_the_snapshot_exactly(self, store):
        _sid, reference = _snapshot_reference(SNAPSHOT)
        assert set(reference) <= {s.scenario_id for s in store.scenarios()}
        assert check_against_snapshot(store.scenarios(), reference) == []

    def test_at_zero_delay_both_arms_are_the_same_plan(self, store):
        for s in store.scenarios():
            at0 = s.delays[0]
            assert at0.delay_hours == 0
            assert at0.frozen == at0.replanned, s.scenario_id

    def test_a_tampered_artifact_is_refused(self, store, tmp_path):
        copy = tmp_path / store.artifact_id
        shutil.copytree(store.directory, copy)
        text = (copy / "speed.json").read_text(encoding="utf-8")
        (copy / "speed.json").write_text(text.replace('"seed": 7', '"seed": 8', 1),
                                         encoding="utf-8")
        with pytest.raises(ValidationError):
            SpeedStore(copy)


class TestFindings:
    def test_monotonicity_is_reported_not_assumed(self, store):
        for s in store.scenarios():
            for arm in ("frozen", "replanned"):
                cuts = [getattr(d, arm).disruption_cut_pct for d in s.delays]
                falls = all(b <= a + 1e-9 for a, b in itertools.pairwise(cuts))
                assert getattr(s.monotonicity, f"{arm}_non_increasing") is falls, s.scenario_id
            assert bool(s.monotonicity.rises) != (
                s.monotonicity.frozen_non_increasing and s.monotonicity.replanned_non_increasing
            )

    def test_at_48_hours_no_payment_is_kept_on_time_yet_the_cut_is_real(self, store):
        """The cut that survives a 48 h delay is payments made *less* late, not on time."""
        s = store.scenario(SINGLE).scenario
        at48 = next(d for d in s.delays if d.delay_hours == 48).frozen
        assert at48.payments_kept_on_time_inr == pytest.approx(0.0, abs=1.0)  # summation noise
        assert at48.disruption_cut_pct == pytest.approx(20.93, abs=0.01)
        assert at48.reduction.delay > 0.95 * at48.reduction.total

    def test_aggregate_intervals_are_well_formed(self, store):
        summary = store.summary()
        assert summary.points and summary.n_with_plan >= 4
        for p in summary.points:
            assert p.n_scenarios == summary.n_with_plan
            for iv in (p.frozen_cut_pct, p.replanned_cut_pct, p.replanning_gain_pp):
                assert iv.low <= iv.mean + 1e-9 and iv.mean <= iv.high + 1e-9


class TestReproducible:
    def test_a_rebuilt_slice_is_identical_and_matches_the_artifact(self, store):
        def build() -> dict:
            return build_speed(snapshot_dir=SNAPSHOT, seeds=(2025,), delays=(0.0, 48.0),
                               families=(ScenarioFamily.SINGLE_MISSED_INFLOW,))

        first, second = build(), build()
        assert content_hash(first) == content_hash(second)

        rebuilt = first["scenarios"][0]
        committed = json.loads(store.scenario(SINGLE).scenario.model_dump_json())
        assert rebuilt["scenario_id"] == SINGLE
        by_delay = {d["delay_hours"]: d for d in committed["delays"]}
        for d in rebuilt["delays"]:
            assert d == by_delay[d["delay_hours"]]
        assert rebuilt["unplanned"] == committed["unplanned"]


class TestServing:
    def test_routes_serve_the_artifact_without_a_database(self):
        from lce.api.app import create_app

        with TestClient(create_app()) as client:
            summary = client.get("/api/v1/structure/speed")
            assert summary.status_code == 200
            body = summary.json()
            assert body["label"] == "Replayed offline, inside the synthetic generator"
            assert body["provenance"]["delay_note"].startswith("Delays are scenario parameters")
            one = client.get(f"/api/v1/structure/speed/scenarios/{SINGLE}")
            assert one.status_code == 200 and one.json()["scenario"]["has_plan"] is True
            assert client.get("/api/v1/structure/speed/scenarios/scn-nope").status_code == 404

    def test_no_simulator_vocabulary_in_any_field_or_label(self, store):
        payload = {"summary": store.summary().model_dump(mode="json"),
                   "scenarios": [store.scenario(s.scenario_id).model_dump(mode="json")
                                 for s in store.scenarios()]}
        fields = [p for p, key, _ in _walk(payload)
                  if any(t in key.lower() for t in FORBIDDEN_FIELD_TOKENS)]
        assert fields == []
        words = [p for p, _key, v in _walk(payload) if isinstance(v, str)
                 and any(w in v.lower() for w in ("simulat", "engine", "tick"))]
        assert words == []

    def test_serving_imports_nothing_that_builds(self):
        code = """
import json, sys
import lce.structure.models
from lce.structure.store import get_speed_store
get_speed_store().summary()
forbidden = ("lce.data.generator", "lce.benchmark", "lce.structure.build",
             "lce.intervention", "lce.simulation")
print(json.dumps(sorted(m for m in sys.modules
                        if any(m == f or m.startswith(f + ".") for f in forbidden))))
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                cwd=ROOT, env={**dict(__import__("os").environ),
                                               "PYTHONPATH": "src", "LCE_LOG_LEVEL": "ERROR"})
        assert result.returncode == 0, result.stderr[-3000:]
        assert json.loads(result.stdout.strip().splitlines()[-1]) == []
