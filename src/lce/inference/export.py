"""Training-side export: fitted model plus calibrator into a servable bundle.

This is the only module in the inference package that imports training code, and
it runs at training time. Nothing here is on the serving path - the service loads
:mod:`lce.inference.artifact` and never touches this file, which is what makes
the "clean process" guarantee mean something.

What is exported is the *whole decision*, not just the weights: the calibration
map fitted on validation and the decision threshold chosen there travel with the
coefficients. Exporting weights alone would ship a model whose probabilities are
uncalibrated and whose threshold has to be re-derived by whoever serves it, which
is how a well-measured model becomes a badly-behaved deployment.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from lce.errors import ModelError
from lce.inference.artifact import save_artifact
from lce.learning.features import FEATURE_SCHEMA_VERSION, NODE_FEATURE_NAMES
from lce.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from lce.learning.baselines import DiscreteTimeHazard
    from lce.learning.calibration import Calibrator

logger = get_logger(__name__)


CalibratorKnots = tuple[np.ndarray, np.ndarray]


def calibrator_payload(
    calibrator: Calibrator | None,
) -> tuple[dict[str, Any], CalibratorKnots | None]:
    """Split a fitted calibrator into manifest scalars and weight-file arrays."""
    if calibrator is None:
        return {"calibrator": "identity"}, None
    payload = dict(calibrator.to_dict())
    knots: CalibratorKnots | None = None
    if payload.get("calibrator") == "isotonic":
        x = getattr(calibrator, "x", None)
        y = getattr(calibrator, "y", None)
        if x is None or y is None or len(x) == 0:
            # An isotonic calibrator that never saw data is an identity map; say
            # so rather than exporting an empty step function that would
            # interpolate against nothing at serving time.
            return {"calibrator": "identity", "note": "isotonic fit was empty"}, None
        knots = (np.asarray(x, dtype=float), np.asarray(y, dtype=float))
    return payload, knots


def export_hazard_model(
    model: DiscreteTimeHazard,
    directory: Path,
    *,
    name: str = "contagion_hazard",
    calibrator: Calibrator | None = None,
    threshold: float = 0.5,
    dataset_version: str | None = None,
    seeds: list[int] | None = None,
    metrics: dict[str, Any] | None = None,
    training: dict[str, Any] | None = None,
) -> Path:
    """Write a fitted :class:`DiscreteTimeHazard` as a servable bundle."""
    if not model.is_fitted:
        raise ModelError("refusing to export an unfitted model")

    calibrator_meta, knots = calibrator_payload(calibrator)
    directory = Path(directory)

    written = save_artifact(
        directory,
        name=name,
        model_version=model.model_version,
        model_kind="discrete_hazard",
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        weights=model.weights,
        mean=model.standardiser.mean,
        scale=model.standardiser.scale,
        n_hazard_intervals=model.task.n_hazard_intervals,
        feature_mask=model.feature_mask,
        calibrator=calibrator_meta,
        calibrator_knots=knots,
        threshold=threshold,
        node_feature_names=list(NODE_FEATURE_NAMES),
        horizon_grid=list(model.task.horizon_grid),
        training=dict(training or {}) | {"config": model.config(), "fit": model.fit_report},
        dataset_version=dataset_version,
        seeds=list(seeds or []),
        metrics=dict(metrics or {}),
    )
    logger.info(
        "model_exported",
        path=str(written),
        model_version=model.model_version,
        calibrator=calibrator_meta.get("calibrator"),
        threshold=threshold,
    )
    return written
