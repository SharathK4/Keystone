"""Production inference: load a model artifact and serve it.

Separate from training by construction. Importing this package must not pull in
the dataset generator, the corpus builder, the optimiser's experiment runner, or
anything else that only exists to *produce* a model - a serving process should
contain what it needs to answer a request and nothing more. The one exception is
:mod:`lce.inference.export`, which runs at training time and is never imported by
:mod:`lce.inference.service`; it is reachable only if a caller asks for it by
name, and a test asserts the serving path stays clean.

``artifact``   the bundle format, its integrity and schema checks, and the
               version resolver
``predictor``  the forward pass, and the reconstruction of an observable window
               from request data
``service``    load-once predict / recommend / replay
``export``     training-side: fitted model plus calibrator into a bundle
"""

from __future__ import annotations

from lce.inference.artifact import (
    ARTIFACT_FORMAT_VERSION,
    ArtifactManifest,
    ModelArtifact,
    list_artifacts,
    load_artifact,
    resolve_artifact,
    save_artifact,
)
from lce.inference.predictor import (
    ContagionPrediction,
    HazardPredictor,
    NetworkState,
    NodePrediction,
    build_request_window,
    merchant_from_payload,
)
from lce.inference.service import (
    API_VERSION,
    InferenceService,
    Recommendation,
    get_service,
    require_service,
    reset_service,
    shock_from_components,
)

__all__ = [
    "API_VERSION",
    "ARTIFACT_FORMAT_VERSION",
    "ArtifactManifest",
    "ContagionPrediction",
    "HazardPredictor",
    "InferenceService",
    "ModelArtifact",
    "NetworkState",
    "NodePrediction",
    "Recommendation",
    "build_request_window",
    "get_service",
    "list_artifacts",
    "load_artifact",
    "merchant_from_payload",
    "require_service",
    "reset_service",
    "resolve_artifact",
    "save_artifact",
    "shock_from_components",
]
