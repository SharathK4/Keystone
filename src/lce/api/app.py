"""FastAPI application factory.

Cross-cutting concerns live here: request-id propagation, structured access
logging, and mapping the domain error hierarchy onto HTTP status codes so
routes can raise :class:`~lce.errors.LCEError` subclasses and get correct
responses without per-route try/except.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from lce import __version__
from lce.api.routers import (
    analysis,
    analytics,
    health,
    inference,
    networks,
    runs,
    webhooks,
)
from lce.config import Settings, get_settings
from lce.errors import LCEError
from lce.logging import configure_logging, get_logger, set_request_id

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

DESCRIPTION = """
Predicts how a single missed payment cascades through a merchant network, and
finds the cheapest financial intervention that stops it.

**Typical flow**

1. `POST /api/v1/datasets` - generate a merchant ecosystem
2. `POST /api/v1/datasets/{id}/dependencies` - learn latent payment dependencies
3. `POST /api/v1/datasets/{id}/shocks` - define a missed payment
4. `POST /api/v1/datasets/{id}/simulate` - see what actually happens
5. `POST /api/v1/datasets/{id}/predict` - see what the model *predicts* happens
6. `POST /api/v1/datasets/{id}/evaluate` - score the prediction against reality
7. `POST /api/v1/datasets/{id}/interventions/optimize` - find the cheapest fix
8. `POST /api/v1/datasets/{id}/systemic-importance` - rank load-bearing merchants

Every step writes a run to `/api/v1/runs` recording its dataset version, seed,
config hash and code version, so any number here can be traced and replayed.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(settings.log_level, settings.log_format, force=True)
    settings.ensure_artifact_dir()
    logger.info("api_starting", version=__version__, **settings.safe_dump())
    yield
    logger.info("api_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. A factory so tests can construct isolated apps."""
    cfg = settings or get_settings()
    configure_logging(cfg.log_level, cfg.log_format)

    app = FastAPI(
        title="Liquidity Contagion Engine",
        description=DESCRIPTION,
        version=__version__,
        root_path=cfg.api_root_path,
        lifespan=lifespan,
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        """Attach a request id and log one structured line per request."""
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        set_request_id(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
            raise
        finally:
            set_request_id(None)

        duration_ms = (time.perf_counter() - started) * 1000.0
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration_ms, 2),
            request_id=request_id,
        )
        return response

    @app.exception_handler(LCEError)
    async def handle_domain_error(request: Request, exc: LCEError) -> JSONResponse:
        """Domain errors carry their own status and stable error code."""
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "code": exc.code,
                "message": exc.message,
                "context": _jsonable(exc.context),
                "request_id": request.headers.get(REQUEST_ID_HEADER),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "request_validation_error",
                "message": "request payload failed validation",
                "context": {"errors": _jsonable_list(exc.errors())},
                "request_id": request.headers.get(REQUEST_ID_HEADER),
            },
        )

    prefix = "/api/v1"
    app.include_router(health.router, prefix=prefix)
    app.include_router(networks.router, prefix=prefix)
    app.include_router(analysis.router, prefix=prefix)
    app.include_router(runs.router, prefix=prefix)
    app.include_router(webhooks.router, prefix=prefix)
    # The serving surface. Registered last so a deployment that only wants
    # inference and analytics can drop the routers above without reordering.
    app.include_router(inference.router, prefix=prefix)
    app.include_router(analytics.router, prefix=prefix)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {
            "name": "liquidity-contagion-engine",
            "version": __version__,
            "docs": "/docs",
            "health": f"{prefix}/health",
        }

    return app


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: _coerce(v) for k, v in (payload or {}).items()}


def _jsonable_list(items: Sequence[Any]) -> list[Any]:
    return [
        {k: _coerce(v) for k, v in item.items()} if isinstance(item, dict) else _coerce(item)
        for item in items
    ]


def _coerce(value: Any) -> Any:
    """Make arbitrary error context JSON-serialisable."""
    if isinstance(value, str | int | float | bool | type(None)):
        return value
    if isinstance(value, list | tuple):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    return str(value)


app = create_app()
