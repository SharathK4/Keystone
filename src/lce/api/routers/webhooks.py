"""Razorpay webhook receiver.

Two things this route must get right, and both are easy to get wrong:

1. **Verify the signature against the raw body.** The body is read with
   ``await request.body()`` and passed to the verifier untouched; FastAPI's
   automatic JSON parsing is deliberately not used, because re-serialising the
   parsed object changes the bytes and breaks the HMAC.
2. **Acknowledge anything it has durably recorded.** Razorpay retries on
   non-2xx. Returning 500 for a payload we cannot map would cause it to retry
   forever, so unmappable-but-recorded events return 200 with a reason. Only a
   *bad signature* and a genuine persistence failure are errors.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from lce.api.deps import Config, Ingestion
from lce.api.schemas import WebhookAck
from lce.errors import SignatureVerificationError, ValidationError
from lce.execution.models import PayPalWebhookAck
from lce.execution.webhooks import Reentry
from lce.logging import get_logger
from lce.paypal.webhooks import (
    SIMULATOR_WEBHOOK_ID,
    CertLoader,
    fetch_certificate,
    verify_webhook,
)
from lce.razorpay.mapper import identity_resolver
from lce.razorpay.webhooks import parse_webhook

logger = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post(
    "/razorpay",
    response_model=WebhookAck,
    summary="Receive a Razorpay webhook",
)
async def razorpay_webhook(
    request: Request,
    ingestion: Ingestion,
    settings: Config,
    x_razorpay_signature: str | None = Header(default=None),
    x_razorpay_event_id: str | None = Header(default=None),
) -> WebhookAck:
    raw_body = await request.body()

    if not settings.razorpay.webhook_configured:
        # Refuse rather than accept unverified writes: an endpoint that ingests
        # unauthenticated payloads into the payment graph is a data-integrity
        # hole, not a convenience.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RAZORPAY_WEBHOOK_SECRET is not configured; refusing unverified webhooks",
        )

    try:
        envelope = parse_webhook(
            raw_body,
            signature=x_razorpay_signature,
            secret=settings.razorpay.require_webhook_secret(),
            event_id_header=x_razorpay_event_id,
            verify=True,
        )
    except SignatureVerificationError as exc:
        logger.warning("webhook_signature_rejected", has_signature=bool(x_razorpay_signature))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message
        ) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message
        ) from exc

    # Ingestion into a live network needs an explicit merchant mapping. Until
    # one is configured, events are recorded and acknowledged but not attached
    # to a dataset - see IngestionService.handle_webhook.
    result = ingestion.handle_webhook(envelope, dataset_id=None, resolver=identity_resolver({}))
    return WebhookAck(**result)


@router.get("/pending", summary="Webhooks recorded but not yet processed")
def pending(ingestion: Ingestion, limit: int = 100) -> list[dict[str, object]]:
    return ingestion.pending(limit)


# ------------------------------------------------------------------ PayPal


def paypal_cert_loader() -> CertLoader:
    """Dependency so tests can supply certificates without a network."""
    return fetch_certificate


CertLoaderDep = Annotated[CertLoader, Depends(paypal_cert_loader)]


@dataclass(frozen=True, slots=True)
class _PayPalOutcome:
    code: int
    detail: str = ""
    ack: PayPalWebhookAck | None = None
    reentry: Reentry | None = None


def _capture(raw_body: bytes, headers: Mapping[str, str], event_id: str) -> None:
    """Keep a verified delivery as received, when a live run asked for it.

    Opt-in (``LCE_RECORD_DELIVERIES_DIR``), after verification only, and never
    allowed to fail the acknowledgement.
    """
    from pathlib import Path

    from lce.walk.recording import capture_delivery
    from lce.walk.thread import get_walk_settings

    directory = get_walk_settings().record_deliveries_dir
    if not directory:
        return
    try:
        capture_delivery(Path(directory), raw_body=raw_body, headers=headers, event_id=event_id)
    except OSError as exc:
        logger.warning("paypal_delivery_not_captured", error=type(exc).__name__)


def _process_paypal(
    raw_body: bytes, headers: Mapping[str, str], load_cert: CertLoader
) -> _PayPalOutcome:
    """Verify, persist once, apply. Runs off the event loop (DB and cert I/O)."""
    from lce.config import get_paypal_settings
    from lce.data.database import get_session_factory
    from lce.execution.webhooks import ingest

    settings = get_paypal_settings()
    if settings.refusal() is not None or not settings.webhook_configured:
        return _PayPalOutcome(
            503, "PAYPAL_WEBHOOK_ID is not configured; refusing unverified webhooks"
        )

    verification = verify_webhook(headers, raw_body, webhook_id=settings.require_webhook_id(),
                                  load_cert=load_cert)
    if not verification.verified and settings.accept_simulator_events:
        simulated = verify_webhook(headers, raw_body, webhook_id=SIMULATOR_WEBHOOK_ID,
                                   load_cert=load_cert)
        if simulated.verified:
            verification = simulated
    if not verification.verified:
        logger.warning("paypal_webhook_rejected", reason=verification.reason,
                       transmission_id=verification.transmission_id)
        return _PayPalOutcome(400, verification.reason)

    try:
        event = json.loads(raw_body)
    except ValueError:
        return _PayPalOutcome(400, "body is not JSON")
    if not isinstance(event, dict) or not event.get("id"):
        return _PayPalOutcome(400, "event has no id")

    _capture(raw_body, headers, str(event["id"]))

    try:
        with get_session_factory()() as session:
            result = ingest(session, event=event, raw_body=raw_body, headers=headers,
                            verification=verification)
    except Exception as exc:  # no database: PayPal will redeliver later
        logger.error("paypal_webhook_unpersisted", error=type(exc).__name__)
        return _PayPalOutcome(503, "the event could not be recorded; PayPal will redeliver it")

    ack = PayPalWebhookAck(
        status=result.status,  # type: ignore[arg-type]
        event_id=result.event_id,
        event_type=result.event_type,
        record_id=result.record_id,
        record_status=result.record_status,
        applied=result.applied,
        reason=result.reason,
        reentry_scheduled=result.reentry is not None,
    )
    return _PayPalOutcome(200, ack=ack, reentry=result.reentry)


@router.post(
    "/paypal",
    response_model=PayPalWebhookAck,
    summary="Receive a PayPal sandbox webhook (verified, recorded once, applied)",
)
async def paypal_webhook(
    request: Request, background: BackgroundTasks, load_cert: CertLoaderDep
) -> PayPalWebhookAck:
    """Verify the signature over the raw body; nothing is processed before that.

    Every verified event is acknowledged with 200 - including duplicates and
    types Keystone ignores - so PayPal does not retry what is already recorded.
    A failed payout is re-analysed *after* this response is sent.
    """
    from lce.execution.reentry import run_scheduled

    raw_body = await request.body()
    headers = dict(request.headers)
    outcome = await run_in_threadpool(_process_paypal, raw_body, headers, load_cert)
    if outcome.code != 200 or outcome.ack is None:
        raise HTTPException(status_code=outcome.code, detail=outcome.detail)
    if outcome.reentry is not None:
        r = outcome.reentry
        background.add_task(run_scheduled, r.record_id, r.trigger,
                            receiver=r.receiver, source_event_id=r.source_event_id)
    return outcome.ack
