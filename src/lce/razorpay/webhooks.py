"""Razorpay webhook verification and parsing.

Signature scheme
----------------
Razorpay signs each webhook with ``HMAC-SHA256(raw_body, webhook_secret)``, hex
encoded, in the ``X-Razorpay-Signature`` header.

Two rules make or break this:

1. **Verify against the raw body bytes.** Parsing the JSON and re-serialising it
   changes key order and whitespace, so the HMAC will not match. The FastAPI
   route therefore reads the raw request body and hands *those bytes* here,
   before any parsing.
2. **Compare in constant time.** ``hmac.compare_digest`` avoids leaking, through
   response timing, how much of a forged signature was correct.

Replay protection is handled a layer up: every webhook is written to
``webhook_events`` keyed on the provider's event id, and a redelivery is
recognised and skipped rather than injected into the network twice. Razorpay
retries on non-2xx, so this is a normal occurrence, not an attack signal.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lce.errors import SignatureVerificationError, ValidationError

SIGNATURE_HEADER = "X-Razorpay-Signature"
EVENT_ID_HEADER = "X-Razorpay-Event-Id"

# Webhook events that carry a cash movement we can turn into a PaymentEvent.
PAYMENT_EVENTS = frozenset(
    {
        "payment.captured",
        "payment.authorized",
        "payment.failed",
        "payout.processed",
        "payout.reversed",
        "transfer.processed",
        "settlement.processed",
    }
)


def compute_signature(body: bytes, secret: str) -> str:
    """HMAC-SHA256 of the raw body, hex encoded."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Constant-time signature check. Returns a bool rather than raising."""
    if not signature or not secret:
        return False
    expected = compute_signature(body, secret)
    return hmac.compare_digest(expected, signature)


def require_signature(body: bytes, signature: str, secret: str) -> None:
    """Verify or raise. Use on any path that mutates state."""
    if not verify_signature(body, signature, secret):
        raise SignatureVerificationError(
            "Razorpay webhook signature verification failed",
            has_signature=bool(signature),
        )


@dataclass(slots=True)
class WebhookEnvelope:
    """A parsed, verified webhook."""

    event_id: str
    event_type: str
    created_at: datetime
    payload: dict[str, Any]
    account_id: str | None = None
    signature_verified: bool = False

    @property
    def carries_payment(self) -> bool:
        return self.event_type in PAYMENT_EVENTS

    def entity(self, name: str) -> dict[str, Any] | None:
        """Pull ``payload.<name>.entity`` out of the nested envelope."""
        section = self.payload.get("payload", {}).get(name, {})
        entity = section.get("entity")
        return entity if isinstance(entity, dict) else None


def parse_webhook(
    body: bytes,
    *,
    signature: str | None = None,
    secret: str | None = None,
    event_id_header: str | None = None,
    verify: bool = True,
) -> WebhookEnvelope:
    """Verify and parse a webhook body into an envelope.

    Verification happens *before* parsing, on the raw bytes, which is the only
    order in which the HMAC can be checked correctly.
    """
    if verify:
        if secret is None:
            raise ValidationError("webhook verification requested without a secret")
        require_signature(body, signature or "", secret)

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"webhook body is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("webhook body must be a JSON object")

    event_type = payload.get("event")
    if not event_type:
        raise ValidationError("webhook payload has no 'event' field")

    created_raw = payload.get("created_at")
    created = (
        datetime.fromtimestamp(int(created_raw), tz=UTC)
        if isinstance(created_raw, int | float)
        else datetime.now(tz=UTC)
    )

    # Razorpay does not always include an id in the body; the header is
    # authoritative for deduplication when present.
    event_id = event_id_header or payload.get("id") or _synthetic_event_id(body)

    return WebhookEnvelope(
        event_id=str(event_id),
        event_type=str(event_type),
        created_at=created,
        payload=payload,
        account_id=payload.get("account_id"),
        signature_verified=verify,
    )


def _synthetic_event_id(body: bytes) -> str:
    """Stable fallback id derived from the body.

    Content-addressed so an identical redelivery still deduplicates correctly
    even when Razorpay omits both the header and an in-body id.
    """
    return f"sha256:{hashlib.sha256(body).hexdigest()[:32]}"
