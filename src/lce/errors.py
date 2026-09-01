"""Domain and application error hierarchy.

Every error carries a stable ``code`` so the API layer can map exceptions to
HTTP responses without string matching.
"""

from __future__ import annotations


class LCEError(Exception):
    """Base class for every error raised by this package."""

    code: str = "lce_error"
    http_status: int = 500

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, object] = dict(context)

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "context": self.context}


class ConfigError(LCEError):
    code = "config_error"


class ValidationError(LCEError):
    """Invalid input that the caller can fix."""

    code = "validation_error"
    http_status = 422


class NotFoundError(LCEError):
    code = "not_found"
    http_status = 404


class ConflictError(LCEError):
    code = "conflict"
    http_status = 409


class GraphError(LCEError):
    code = "graph_error"
    http_status = 400


class SimulationError(LCEError):
    code = "simulation_error"


class ModelError(LCEError):
    code = "model_error"


class LeakageError(LCEError):
    """A model was offered information it is not allowed to see.

    Raised by the Phase-3 leakage audit when a feature builder's output moves in
    response to a latent generator parameter, a post-origin event, or the
    shock-perturbed obligation book. Treated as an error rather than a warning
    because a leaked benchmark reports numbers that cannot be reproduced on real
    data, which is worse than reporting none.
    """

    code = "leakage_error"


class OptimizationError(LCEError):
    code = "optimization_error"


class DependencyUnavailableError(LCEError):
    """An optional third-party dependency (torch, ortools, ...) is not installed."""

    code = "dependency_unavailable"
    http_status = 503


class RazorpayError(LCEError):
    code = "razorpay_error"
    http_status = 502


class SignatureVerificationError(RazorpayError):
    code = "invalid_signature"
    http_status = 400
