"""Run tracking.

Every unit of work - generation, simulation, training, prediction, optimisation,
evaluation - opens a run, and the run records what it needs to be replayed:
dataset version, model version, base seed, the full seed bundle, the config and
its hash, the code version, and the git SHA when available.

The tracker writes to the database when one is configured and always writes a
JSON manifest to the artifact directory, so provenance survives even when the
run is executed against a throwaway database or none at all.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from lce import __version__
from lce.config import get_settings
from lce.data.unit_of_work import UnitOfWork
from lce.domain.base import new_id
from lce.domain.enums import RunKind, RunStatus
from lce.logging import bind_run, get_logger
from lce.seeds import SeedBundle

logger = get_logger(__name__)

MANIFEST_DIRNAME = "runs"


@lru_cache(maxsize=1)
def git_sha() -> str | None:
    """Current commit, when the working tree is a git repo. Never raises."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - env dependent
        return None


@dataclass(slots=True)
class RunRecord:
    """In-memory view of a tracked run."""

    run_id: str
    kind: RunKind
    name: str = ""
    experiment_id: str | None = None
    parent_run_id: str | None = None
    dataset_version: str | None = None
    model_version: str | None = None
    shock_id: str | None = None
    plan_id: str | None = None
    seed: int = 0
    config: dict[str, Any] = field(default_factory=dict)
    config_hash: str | None = None
    seeds: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    status: RunStatus = RunStatus.PENDING
    started_at: str = ""
    finished_at: str | None = None
    duration_ms: float | None = None
    error: str | None = None
    code_version: str = __version__
    git_sha: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "kind": str(self.kind),
            "name": self.name,
            "experiment_id": self.experiment_id,
            "parent_run_id": self.parent_run_id,
            "dataset_version": self.dataset_version,
            "model_version": self.model_version,
            "shock_id": self.shock_id,
            "plan_id": self.plan_id,
            "seed": self.seed,
            "config": self.config,
            "config_hash": self.config_hash,
            "seeds": self.seeds,
            "metrics": self.metrics,
            "status": str(self.status),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "code_version": self.code_version,
            "git_sha": self.git_sha,
        }


class RunTracker:
    """Opens, completes and persists runs."""

    def __init__(
        self,
        *,
        uow: UnitOfWork | None = None,
        persist_db: bool = True,
        manifest_dir: Path | None = None,
    ) -> None:
        self._uow = uow
        self.persist_db = persist_db and uow is not None
        settings = get_settings()
        self.manifest_dir = manifest_dir or (settings.model_artifact_dir / MANIFEST_DIRNAME)

    @contextmanager
    def run(
        self,
        kind: RunKind,
        *,
        name: str = "",
        run_id: str | None = None,
        experiment_id: str | None = None,
        parent_run_id: str | None = None,
        dataset_version: str | None = None,
        model_version: str | None = None,
        shock_id: str | None = None,
        plan_id: str | None = None,
        seed: int = 0,
        config: dict[str, Any] | None = None,
        config_hash: str | None = None,
        seeds: SeedBundle | dict[str, Any] | None = None,
    ) -> Iterator[RunRecord]:
        """Track a unit of work.

        The record is created before the body executes and finalised afterwards,
        including on failure - a crashed run leaves a ``failed`` row with its
        traceback rather than disappearing.
        """
        record = RunRecord(
            run_id=run_id or new_id("run"),
            kind=kind,
            name=name,
            experiment_id=experiment_id,
            parent_run_id=parent_run_id,
            dataset_version=dataset_version,
            model_version=model_version,
            shock_id=shock_id,
            plan_id=plan_id,
            seed=seed,
            config=config or {},
            config_hash=config_hash,
            seeds=seeds.to_dict() if isinstance(seeds, SeedBundle) else (seeds or {}),
            status=RunStatus.RUNNING,
            started_at=datetime.now(tz=UTC).isoformat(),
            git_sha=git_sha(),
        )

        if self.persist_db and self._uow is not None:
            self._uow.runs.start(
                record.run_id,
                kind,
                name=name,
                experiment_id=experiment_id,
                parent_run_id=parent_run_id,
                dataset_version=dataset_version,
                model_version=model_version,
                shock_id=shock_id,
                plan_id=plan_id,
                seed=seed,
                config=record.config,
                config_hash=config_hash,
                seeds=record.seeds,
                git_sha=record.git_sha,
                code_version=record.code_version,
            )
            self._uow.flush()

        started = time.perf_counter()
        with bind_run(record.run_id):
            logger.info("run_started", run_id=record.run_id, kind=str(kind), name=name)
            try:
                yield record
            except Exception as exc:
                record.status = RunStatus.FAILED
                record.error = f"{type(exc).__name__}: {exc}"
                record.finished_at = datetime.now(tz=UTC).isoformat()
                record.duration_ms = (time.perf_counter() - started) * 1000.0
                self._finalise(record)
                logger.error("run_failed", run_id=record.run_id, error=record.error)
                raise
            record.status = RunStatus.COMPLETED
            record.finished_at = datetime.now(tz=UTC).isoformat()
            record.duration_ms = (time.perf_counter() - started) * 1000.0
            self._finalise(record)
            logger.info(
                "run_completed", run_id=record.run_id, duration_ms=record.duration_ms
            )

    def _finalise(self, record: RunRecord) -> None:
        if self.persist_db and self._uow is not None:
            try:
                if record.status is RunStatus.COMPLETED:
                    self._uow.runs.complete(
                        record.run_id,
                        metrics=record.metrics,
                        duration_ms=record.duration_ms,
                    )
                else:
                    self._uow.runs.fail(record.run_id, record.error or "unknown error")
                self._uow.commit()
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning(
                    "run_persist_failed", run_id=record.run_id, error=str(exc)
                )
        self.write_manifest(record)

    def write_manifest(self, record: RunRecord) -> Path:
        """Always-on filesystem provenance, independent of the database."""
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        path = self.manifest_dir / f"{record.run_id}.json"
        path.write_text(
            json.dumps(record.to_dict(), indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        return path

    def read_manifest(self, run_id: str) -> dict[str, Any] | None:
        path = self.manifest_dir / f"{run_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_manifests(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.manifest_dir.exists():
            return []
        files = sorted(
            self.manifest_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [json.loads(p.read_text(encoding="utf-8")) for p in files[:limit]]
