"""Model artifact registry.

Every trained artifact is written next to a JSON manifest recording what it was
trained on and how, so a model file is never an orphan. The manifest carries the
dataset version, the seed bundle, the config hash, the training metrics and the
code version - which is the minimum needed to answer "where did this number come
from?" six weeks later.

Artifacts live under ``MODEL_ARTIFACT_DIR`` in ``<name>/<version>/`` directories.
Versions are content-addressed from the config, so retraining with identical
settings and seed overwrites in place rather than accumulating near-duplicates.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lce import __version__
from lce.config import get_settings
from lce.errors import NotFoundError
from lce.logging import get_logger

logger = get_logger(__name__)

MANIFEST_NAME = "manifest.json"
ARTIFACT_NAME = "model.pt"


@dataclass(slots=True)
class ModelManifest:
    """Everything needed to interpret and reproduce an artifact."""

    name: str
    version: str
    kind: str
    created_at: str
    code_version: str
    dataset_version: str | None = None
    seed: int | None = None
    config: dict[str, Any] = field(default_factory=dict)
    seeds: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    training: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "kind": self.kind,
            "created_at": self.created_at,
            "code_version": self.code_version,
            "dataset_version": self.dataset_version,
            "seed": self.seed,
            "config": self.config,
            "seeds": self.seeds,
            "metrics": self.metrics,
            "training": self.training,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ModelManifest:
        return cls(
            name=payload["name"],
            version=payload["version"],
            kind=payload.get("kind", "unknown"),
            created_at=payload.get("created_at", ""),
            code_version=payload.get("code_version", ""),
            dataset_version=payload.get("dataset_version"),
            seed=payload.get("seed"),
            config=payload.get("config", {}),
            seeds=payload.get("seeds", {}),
            metrics=payload.get("metrics", {}),
            training=payload.get("training", {}),
            notes=payload.get("notes", ""),
        )


class ModelRegistry:
    """Filesystem-backed artifact store."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else get_settings().model_artifact_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, name: str, version: str) -> Path:
        return self.root / name / version

    def save(
        self,
        name: str,
        version: str,
        *,
        kind: str,
        config: dict[str, Any],
        artifact_writer: Any = None,
        dataset_version: str | None = None,
        seed: int | None = None,
        seeds: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
        training: dict[str, Any] | None = None,
        notes: str = "",
    ) -> ModelManifest:
        """Persist an artifact plus its manifest.

        ``artifact_writer`` is a callable taking the destination path - typically
        ``predictor.save``. Passing ``None`` records a manifest for a model that
        has no binary payload (the analytic predictors), which keeps their
        provenance in the same place as the learned ones.
        """
        target = self.path_for(name, version)
        target.mkdir(parents=True, exist_ok=True)

        if artifact_writer is not None:
            artifact_writer(target / ARTIFACT_NAME)

        manifest = ModelManifest(
            name=name,
            version=version,
            kind=kind,
            created_at=datetime.now(tz=UTC).isoformat(),
            code_version=__version__,
            dataset_version=dataset_version,
            seed=seed,
            config=config,
            seeds=seeds or {},
            metrics=metrics or {},
            training=training or {},
            notes=notes,
        )
        (target / MANIFEST_NAME).write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        logger.info("model_saved", name=name, version=version, path=str(target))
        return manifest

    def load_manifest(self, name: str, version: str) -> ModelManifest:
        path = self.path_for(name, version) / MANIFEST_NAME
        if not path.exists():
            raise NotFoundError(
                f"no manifest for model {name!r} version {version!r}",
                name=name,
                version=version,
            )
        return ModelManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def artifact_path(self, name: str, version: str) -> Path:
        path = self.path_for(name, version) / ARTIFACT_NAME
        if not path.exists():
            raise NotFoundError(
                f"no artifact for model {name!r} version {version!r}",
                name=name,
                version=version,
            )
        return path

    def exists(self, name: str, version: str) -> bool:
        return (self.path_for(name, version) / MANIFEST_NAME).exists()

    def list_versions(self, name: str) -> list[str]:
        base = self.root / name
        if not base.exists():
            return []
        return sorted(
            p.name for p in base.iterdir() if p.is_dir() and (p / MANIFEST_NAME).exists()
        )

    def list_models(self) -> dict[str, list[str]]:
        if not self.root.exists():
            return {}
        return {
            p.name: self.list_versions(p.name)
            for p in sorted(self.root.iterdir())
            if p.is_dir()
        }

    def latest(self, name: str) -> ModelManifest | None:
        """Most recently created version of a model."""
        manifests = [self.load_manifest(name, v) for v in self.list_versions(name)]
        if not manifests:
            return None
        return max(manifests, key=lambda m: m.created_at)

    def delete(self, name: str, version: str) -> bool:
        target = self.path_for(name, version)
        if not target.exists():
            return False
        shutil.rmtree(target)
        logger.info("model_deleted", name=name, version=version)
        return True
