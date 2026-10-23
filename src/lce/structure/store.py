"""Serve the value-of-speed artifact, read-only.

Imports nothing that builds: no generator, no benchmark, no optimiser. The
artifact is verified against its content hash on load, so a figure on screen is
always the figure that was built.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from lce.config import REPO_ROOT
from lce.errors import NotFoundError, ValidationError
from lce.logging import get_logger
from lce.structure.models import (
    LABEL,
    ProofView,
    PullRecord,
    PullsView,
    ScenarioSpeed,
    SpeedPoint,
    SpeedProvenance,
    SpeedScenarioRef,
    SpeedScenarioView,
    SpeedSummary,
)

logger = get_logger(__name__)

FORMAT = "keystone-speed-v1"
STRUCTURE_ROOT = REPO_ROOT / "artifacts" / "structure"


def _digest(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_artifact(root: Path, prefix: str, snapshot_id: str | None = None) -> Path:
    """The one ``<prefix>-<hash>`` artifact under ``root`` (for ``snapshot_id``, when given)."""
    found = []
    for manifest in sorted(Path(root).glob(f"{prefix}-*/manifest.json")):
        meta = json.loads(manifest.read_text(encoding="utf-8"))
        if snapshot_id is None or meta.get("snapshot_id") == snapshot_id:
            found.append(manifest.parent)
    if not found:
        raise NotFoundError(f"no {prefix} artifact has been built", path=str(root),
                            snapshot_id=snapshot_id)
    if len(found) > 1:
        raise ValidationError(f"more than one {prefix} artifact; keep one",
                              found=[p.name for p in found])
    return found[0]


def resolve_speed(root: Path = STRUCTURE_ROOT, snapshot_id: str | None = None) -> Path:
    return resolve_artifact(root, "speed", snapshot_id)


def load_verified(
    directory: Path, prefix: str, filename: str, fmt: str
) -> tuple[dict[str, Any], str]:
    """Read an artifact and refuse it unless its bytes match its name and manifest."""
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    payload = json.loads((directory / filename).read_text(encoding="utf-8"))
    digest = _digest(payload)
    if payload.get("format") != fmt:
        raise ValidationError(f"unsupported {prefix} artifact format {payload.get('format')!r}")
    if digest != manifest.get("content_hash") or directory.name != f"{prefix}-{digest[:16]}":
        raise ValidationError(
            f"{prefix} artifact does not match its content hash", path=str(directory)
        )
    return payload, digest


class SpeedStore:
    def __init__(self, directory: Path) -> None:
        directory = Path(directory)
        payload, digest = load_verified(directory, "speed", "speed.json", FORMAT)
        self.directory = directory
        self.artifact_id = directory.name
        self.content_hash = digest
        self.provenance = SpeedProvenance.model_validate(payload["provenance"])
        self.points = [SpeedPoint.model_validate(p) for p in payload["points"]]
        self.monotone_scenarios: int = payload["monotone_scenarios"]
        self._scenarios = {
            s["scenario_id"]: ScenarioSpeed.model_validate(s) for s in payload["scenarios"]
        }

    def summary(self) -> SpeedSummary:
        scenarios = list(self._scenarios.values())
        return SpeedSummary(
            artifact_id=self.artifact_id,
            content_hash=self.content_hash,
            label=LABEL,
            provenance=self.provenance,
            n_scenarios=len(scenarios),
            n_with_plan=sum(1 for s in scenarios if s.has_plan),
            points=self.points,
            monotone_scenarios=self.monotone_scenarios,
            scenarios=[
                SpeedScenarioRef(scenario_id=s.scenario_id, family=s.family, seed=s.seed,
                                 in_snapshot=s.in_snapshot, has_plan=s.has_plan)
                for s in scenarios
            ],
        )

    def scenario(self, scenario_id: str) -> SpeedScenarioView:
        try:
            found = self._scenarios[scenario_id]
        except KeyError:
            raise NotFoundError("no value-of-speed result for this scenario",
                                scenario_id=scenario_id) from None
        return SpeedScenarioView(artifact_id=self.artifact_id, content_hash=self.content_hash,
                                 label=LABEL, scenario=found)

    def scenarios(self) -> list[ScenarioSpeed]:
        return list(self._scenarios.values())


_lock = threading.Lock()
_store: SpeedStore | None = None


def get_speed_store(root: Path = STRUCTURE_ROOT) -> SpeedStore:
    global _store
    with _lock:
        if _store is None:
            _store = SpeedStore(resolve_speed(root))
            logger.info("speed_artifact_loaded", artifact_id=_store.artifact_id)
        return _store


def reset_speed_store() -> None:
    global _store
    with _lock:
        _store = None


class PullsStore:
    """Every merchant's failure, analysed offline at three shock sizes."""

    def __init__(self, directory: Path) -> None:
        payload, digest = load_verified(Path(directory), "pulls", "pulls.json", "keystone-pulls-v1")
        self.artifact_id, self.content_hash = Path(directory).name, digest
        self.view = PullsView(artifact_id=self.artifact_id, content_hash=digest, label=LABEL,
                              provenance=payload["provenance"], records=payload["records"])
        self._by_id = {r.merchant_id: r for r in self.view.records}

    def record(self, merchant_id: str) -> PullRecord:
        try:
            return self._by_id[merchant_id]
        except KeyError:
            raise NotFoundError("no pulled-stone result for this merchant",
                                merchant_id=merchant_id) from None


class ProofStore:
    """The inspection certificate: barrier, recovery, audit, results, weaknesses."""

    def __init__(self, directory: Path) -> None:
        payload, digest = load_verified(Path(directory), "proof", "proof.json", "keystone-proof-v1")
        self.artifact_id, self.content_hash = Path(directory).name, digest
        body = {k: v for k, v in payload.items() if k != "format"}
        self.view = ProofView(
            artifact_id=self.artifact_id, content_hash=digest, label=LABEL, **body
        )


_pulls: PullsStore | None = None
_proof: ProofStore | None = None


def get_pulls_store(root: Path = STRUCTURE_ROOT) -> PullsStore:
    global _pulls
    with _lock:
        if _pulls is None:
            _pulls = PullsStore(resolve_artifact(root, "pulls"))
        return _pulls


def get_proof_store(root: Path = STRUCTURE_ROOT) -> ProofStore:
    global _proof
    with _lock:
        if _proof is None:
            _proof = ProofStore(resolve_artifact(root, "proof"))
        return _proof


def reset_structure_stores() -> None:
    global _store, _pulls, _proof
    with _lock:
        _store = _pulls = _proof = None
