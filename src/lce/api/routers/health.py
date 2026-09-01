"""Health and readiness.

Three endpoints, because they answer different questions:

``/health/live``   is the process up? No dependencies touched - a liveness probe
                   that checked the database would restart a healthy app during
                   a database blip.
``/health/ready``  can it serve traffic? Requires the database.
``/health``        full status including Razorpay, for humans and dashboards.

Razorpay being unconfigured is reported but does **not** make the service
unready: the modelling half of the system runs without any Razorpay account.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from lce import __version__
from lce.api.deps import Config
from lce.api.schemas import HealthResponse, ReadinessResponse
from lce.data.database import healthcheck
from lce.razorpay.client import RazorpayClient

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", summary="Liveness probe")
def live() -> dict[str, str]:
    """Process is running. Deliberately checks nothing else."""
    return {"status": "ok", "version": __version__}


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness probe")
def ready(response: Response) -> ReadinessResponse:
    """Ready only if the database answers."""
    db = healthcheck()
    is_ready = db.get("status") == "ok"
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(ready=is_ready, detail={"database": db})


@router.get("", response_model=HealthResponse, summary="Full health report")
def health(settings: Config) -> HealthResponse:
    db = healthcheck()

    razorpay: dict[str, Any]
    if settings.razorpay.configured:
        with RazorpayClient(settings.razorpay) as client:
            razorpay = client.health()
    else:
        razorpay = {"status": "not_configured", "mode": str(settings.razorpay.mode)}

    checks = {
        "database": db.get("status", "unknown"),
        # Razorpay is optional; "not_configured" is a normal state, not a fault.
        "razorpay": razorpay.get("status", "unknown"),
        "artifacts": "ok" if settings.model_artifact_dir.exists() else "missing",
    }
    overall = "ok" if checks["database"] == "ok" else "error"
    if overall == "ok" and razorpay.get("status") == "error":
        overall = "degraded"

    return HealthResponse(
        status=overall,
        version=__version__,
        environment=str(settings.env),
        database=db,
        razorpay=razorpay,
        checks=checks,
    )


@router.get("/config", summary="Effective configuration (secrets redacted)")
def config(settings: Config) -> dict[str, Any]:
    """Non-secret view of the running configuration.

    Useful for confirming which seed and which objective weights a deployment is
    actually using. Secrets are redacted by ``Settings.safe_dump``.
    """
    return settings.safe_dump()
