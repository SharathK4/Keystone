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

from fastapi import APIRouter, Header, HTTPException, Request, status

from lce.api.deps import Config, Ingestion
from lce.api.schemas import WebhookAck
from lce.errors import SignatureVerificationError, ValidationError
from lce.logging import get_logger
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
