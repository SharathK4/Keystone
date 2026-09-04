"""The model artifact: what gets exported, and what the loader refuses.

Training and serving are separate processes with separate dependencies. Whatever
crosses between them has to be a *file*, self-describing enough that a loader can
tell whether it is safe to use - not a pickle of a live training object that only
deserialises if the training package happens to be importable.

What a bundle contains
----------------------
``weights.npz``    fitted coefficients, the standardiser's mean and scale, and the
                   feature mask if one was used
``manifest.json``  model version and kind, the feature schema version, the number
                   of hazard intervals, the calibrator and its parameters, the
                   decision threshold, training dataset metadata, code version,
                   and a content hash over the weights

What the loader checks, in order
--------------------------------
1. the bundle is complete and parseable;
2. the recorded content hash matches the weights actually on disk - a bundle that
   has been truncated or edited is refused rather than served;
3. the feature schema version matches the one this code builds. A model fitted on
   different columns will happily produce numbers from the wrong ones, and those
   numbers look entirely reasonable. This is the check that stops a silent
   wrong-answer failure, so it is an error and not a warning;
4. the design width implied by the weights matches the design this code assembles.

No training code is imported here, by construction. The forward pass of a
discrete-time hazard is a standardisation, a matrix product, a sigmoid and a
cumulative product; reproducing that in the serving package is a few lines, and
it is what lets the artifact load in a process that has never seen the dataset
generator. The *layout* of the design matrix is not duplicated - it comes from
:func:`~lce.learning.features.build_hazard_design`, which both sides call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from lce import __version__
from lce.errors import ModelError, NotFoundError

ARTIFACT_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
WEIGHTS_NAME = "weights.npz"

#: Model kinds this artifact format can serve. The graph model is trained and
#: saved through torch and is deliberately out of scope here: serving it would
#: require torch in the inference process, which the laptop constraint does not
#: assume.
SERVABLE_KINDS = frozenset({"discrete_hazard"})


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class ArtifactManifest:
    """Everything needed to decide whether a bundle may be served."""

    name: str
    model_version: str
    model_kind: str
    feature_schema_version: str
    format_version: int = ARTIFACT_FORMAT_VERSION
    n_hazard_intervals: int = 8
    horizon_grid: list[float] = field(default_factory=list)
    calibrator: dict[str, Any] = field(default_factory=dict)
    threshold: float = 0.5
    node_feature_names: list[str] = field(default_factory=list)
    design_width: int = 0
    training: dict[str, Any] = field(default_factory=dict)
    dataset_version: str | None = None
    seeds: list[int] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    code_version: str = __version__
    created_at: str = ""
    content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model_version": self.model_version,
            "model_kind": self.model_kind,
            "feature_schema_version": self.feature_schema_version,
            "format_version": self.format_version,
            "n_hazard_intervals": self.n_hazard_intervals,
            "horizon_grid": self.horizon_grid,
            "calibrator": self.calibrator,
            "threshold": self.threshold,
            "node_feature_names": self.node_feature_names,
            "design_width": self.design_width,
            "training": self.training,
            "dataset_version": self.dataset_version,
            "seeds": self.seeds,
            "metrics": self.metrics,
            "code_version": self.code_version,
            "created_at": self.created_at,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ArtifactManifest:
        known = set(cls.__slots__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in payload.items() if k in known})


@dataclass(slots=True)
class ModelArtifact:
    """A loaded, verified bundle. Immutable in practice - never refitted."""

    manifest: ArtifactManifest
    weights: np.ndarray
    mean: np.ndarray
    scale: np.ndarray
    feature_mask: np.ndarray | None = None
    calibrator_x: np.ndarray | None = None
    calibrator_y: np.ndarray | None = None
    path: Path | None = None

    @property
    def model_version(self) -> str:
        return self.manifest.model_version

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.manifest.name,
            "model_version": self.manifest.model_version,
            "model_kind": self.manifest.model_kind,
            "feature_schema_version": self.manifest.feature_schema_version,
            "n_hazard_intervals": self.manifest.n_hazard_intervals,
            "design_width": self.manifest.design_width,
            "calibrator": self.manifest.calibrator.get("calibrator"),
            "threshold": self.manifest.threshold,
            "dataset_version": self.manifest.dataset_version,
            "code_version": self.manifest.code_version,
            "content_hash": self.manifest.content_hash[:16],
            "path": str(self.path) if self.path else None,
        }


def save_artifact(
    directory: Path,
    *,
    name: str,
    model_version: str,
    model_kind: str,
    feature_schema_version: str,
    weights: np.ndarray,
    mean: np.ndarray,
    scale: np.ndarray,
    n_hazard_intervals: int,
    feature_mask: np.ndarray | None = None,
    calibrator: dict[str, Any] | None = None,
    calibrator_knots: tuple[np.ndarray, np.ndarray] | None = None,
    threshold: float = 0.5,
    node_feature_names: list[str] | None = None,
    horizon_grid: list[float] | None = None,
    training: dict[str, Any] | None = None,
    dataset_version: str | None = None,
    seeds: list[int] | None = None,
    metrics: dict[str, Any] | None = None,
) -> Path:
    """Write a bundle. The content hash is computed over the weights as written."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    payload: dict[str, np.ndarray] = {
        "weights": np.asarray(weights, dtype=float),
        "mean": np.asarray(mean, dtype=float),
        "scale": np.asarray(scale, dtype=float),
    }
    if feature_mask is not None:
        payload["feature_mask"] = np.asarray(feature_mask, dtype=bool)
    if calibrator_knots is not None:
        # Isotonic calibration is a step function, so its parameters are two
        # arrays rather than two scalars and they belong with the weights.
        payload["calibrator_x"] = np.asarray(calibrator_knots[0], dtype=float)
        payload["calibrator_y"] = np.asarray(calibrator_knots[1], dtype=float)
    weights_path = directory / WEIGHTS_NAME
    np.savez(str(weights_path), **payload)  # type: ignore[arg-type]

    manifest = ArtifactManifest(
        name=name,
        model_version=model_version,
        model_kind=model_kind,
        feature_schema_version=feature_schema_version,
        n_hazard_intervals=n_hazard_intervals,
        horizon_grid=list(horizon_grid or []),
        calibrator=dict(calibrator or {"calibrator": "identity"}),
        threshold=float(threshold),
        node_feature_names=list(node_feature_names or []),
        design_width=int(np.asarray(weights).shape[-1]),
        training=dict(training or {}),
        dataset_version=dataset_version,
        seeds=list(seeds or []),
        metrics=dict(metrics or {}),
        created_at=datetime.now(tz=UTC).isoformat(),
        content_hash=_hash_file(weights_path),
    )
    (directory / MANIFEST_NAME).write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return directory


def load_artifact(directory: Path, *, expected_schema: str | None = None) -> ModelArtifact:
    """Load and verify a bundle, or refuse it with a specific reason."""
    directory = Path(directory)
    manifest_path = directory / MANIFEST_NAME
    weights_path = directory / WEIGHTS_NAME
    if not manifest_path.exists() or not weights_path.exists():
        raise NotFoundError(
            f"no model artifact at {directory}: expected {MANIFEST_NAME} and {WEIGHTS_NAME}",
            path=str(directory),
        )

    manifest = ArtifactManifest.from_dict(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )

    if manifest.format_version != ARTIFACT_FORMAT_VERSION:
        raise ModelError(
            f"artifact format {manifest.format_version} is not supported "
            f"(this build reads {ARTIFACT_FORMAT_VERSION})",
            path=str(directory),
        )
    if manifest.model_kind not in SERVABLE_KINDS:
        raise ModelError(
            f"model kind {manifest.model_kind!r} cannot be served by this package; "
            f"servable kinds are {sorted(SERVABLE_KINDS)}",
            path=str(directory),
        )

    actual_hash = _hash_file(weights_path)
    if manifest.content_hash and actual_hash != manifest.content_hash:
        raise ModelError(
            "artifact integrity check failed: the weights on disk do not match "
            "the hash recorded when they were exported",
            expected=manifest.content_hash[:16],
            actual=actual_hash[:16],
        )

    if expected_schema is not None and manifest.feature_schema_version != expected_schema:
        raise ModelError(
            (
                f"feature schema mismatch: the artifact was fitted on "
                f"{manifest.feature_schema_version!r} but this build produces "
                f"{expected_schema!r}. Serving it would feed the model different "
                "columns than it was trained on."
            ),
            artifact_schema=manifest.feature_schema_version,
            runtime_schema=expected_schema,
        )

    archive = np.load(str(weights_path))
    return ModelArtifact(
        manifest=manifest,
        weights=archive["weights"],
        mean=archive["mean"],
        scale=archive["scale"],
        feature_mask=archive.get("feature_mask"),
        calibrator_x=archive.get("calibrator_x"),
        calibrator_y=archive.get("calibrator_y"),
        path=directory,
    )


def list_artifacts(root: Path) -> list[dict[str, Any]]:
    """Every bundle under ``root``, newest first. The registry's index."""
    root = Path(root)
    if not root.exists():
        return []
    found: list[dict[str, Any]] = []
    for manifest_path in sorted(root.glob("*/" + MANIFEST_NAME)):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        found.append(
            {
                "path": str(manifest_path.parent),
                "name": payload.get("name"),
                "model_version": payload.get("model_version"),
                "model_kind": payload.get("model_kind"),
                "feature_schema_version": payload.get("feature_schema_version"),
                "created_at": payload.get("created_at"),
            }
        )
    return sorted(found, key=lambda row: row.get("created_at") or "", reverse=True)


def resolve_artifact(root: Path, version: str | None = None) -> Path:
    """Pick a bundle by version, or the most recent one.

    The version resolver. Explicit beats implicit: a request naming a version it
    cannot find fails rather than quietly falling back to whatever is newest.
    """
    entries = list_artifacts(root)
    if not entries:
        raise NotFoundError(f"no model artifacts under {root}", path=str(root))
    if version is None:
        return Path(entries[0]["path"])
    for entry in entries:
        if entry.get("model_version") == version or entry.get("name") == version:
            return Path(entry["path"])
    raise NotFoundError(
        f"no artifact with version {version!r} under {root}",
        available=[e.get("model_version") for e in entries],
    )
