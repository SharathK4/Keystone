"""Razorpay ingestion.

Turns verified webhooks into domain payment events. The important property here
is **idempotency**: Razorpay retries on any non-2xx response, so the same event
will arrive more than once in normal operation. Every webhook is recorded first,
keyed on the provider's event id under a unique constraint, and a redelivery is
detected and skipped before anything touches the network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from lce.data.unit_of_work import UnitOfWork
from lce.errors import ValidationError
from lce.logging import get_logger
from lce.razorpay.mapper import MerchantResolver, payment_from_entity
from lce.razorpay.webhooks import WebhookEnvelope

logger = get_logger(__name__)

# Webhook event -> which entity in the payload carries the money movement.
_ENTITY_FOR_EVENT: dict[str, str] = {
    "payment.captured": "payment",
    "payment.authorized": "payment",
    "payment.failed": "payment",
    "payout.processed": "payout",
    "payout.reversed": "payout",
    "transfer.processed": "transfer",
    "settlement.processed": "settlement",
}


class IngestionService:
    """Persists inbound Razorpay webhooks and maps them onto the network."""

    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    def handle_webhook(
        self,
        envelope: WebhookEnvelope,
        *,
        dataset_id: str | None = None,
        resolver: MerchantResolver | None = None,
        epoch: datetime | None = None,
    ) -> dict[str, Any]:
        """Record and (when mappable) ingest a webhook.

        Returns a status payload. Never raises for an *unmappable* event -
        those are recorded and acknowledged, because failing the delivery would
        make Razorpay retry an event that can never succeed.
        """
        if self.uow.webhooks.already_seen(envelope.event_id):
            logger.info(
                "webhook_duplicate",
                event_id=envelope.event_id,
                event_type=envelope.event_type,
            )
            return {
                "status": "duplicate",
                "event_id": envelope.event_id,
                "ingested": 0,
            }

        self.uow.webhooks.record(
            envelope.event_id,
            envelope.event_type,
            envelope.payload,
            signature_verified=envelope.signature_verified,
        )
        self.uow.flush()

        if not envelope.carries_payment or dataset_id is None or resolver is None:
            reason = (
                "event carries no cash movement"
                if not envelope.carries_payment
                else "no dataset/resolver configured for ingestion"
            )
            self.uow.webhooks.mark_processed(envelope.event_id)
            self.uow.commit()
            return {
                "status": "recorded",
                "event_id": envelope.event_id,
                "ingested": 0,
                "reason": reason,
            }

        entity_name = _ENTITY_FOR_EVENT.get(envelope.event_type, "payment")
        entity = envelope.entity(entity_name)
        if entity is None:
            self.uow.webhooks.mark_processed(
                envelope.event_id, error=f"no '{entity_name}' entity in payload"
            )
            self.uow.commit()
            return {
                "status": "recorded",
                "event_id": envelope.event_id,
                "ingested": 0,
                "reason": f"no '{entity_name}' entity in payload",
            }

        try:
            event = payment_from_entity(
                entity,
                epoch or datetime.now(tz=UTC),
                resolve_merchant=resolver,
            )
        except ValidationError as exc:
            # A payload we understand the shape of but cannot map is a data
            # problem, not a transport problem: record the reason and ack, so
            # Razorpay stops retrying it.
            self.uow.webhooks.mark_processed(envelope.event_id, error=exc.message)
            self.uow.commit()
            logger.warning(
                "webhook_unmappable", event_id=envelope.event_id, error=exc.message
            )
            return {
                "status": "recorded",
                "event_id": envelope.event_id,
                "ingested": 0,
                "reason": exc.message,
            }

        # A payment already stored under this Razorpay id is a redelivery that
        # slipped past the event-id check (e.g. re-sent under a new event id).
        if event.external_id and self.uow.payments.by_external_id(event.external_id):
            self.uow.webhooks.mark_processed(envelope.event_id)
            self.uow.commit()
            return {
                "status": "duplicate_payment",
                "event_id": envelope.event_id,
                "ingested": 0,
            }

        self.uow.payments.save_many([event], dataset_id)
        self.uow.webhooks.mark_processed(envelope.event_id)
        self.uow.commit()

        logger.info(
            "webhook_ingested",
            event_id=envelope.event_id,
            event_type=envelope.event_type,
            payer=event.payer_id,
            payee=event.payee_id,
        )
        return {
            "status": "ingested",
            "event_id": envelope.event_id,
            "ingested": 1,
            "payment_event_id": event.event_id,
            "payer_id": event.payer_id,
            "payee_id": event.payee_id,
        }

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        return [
            {
                "provider_event_id": row.provider_event_id,
                "event_type": row.event_type,
                "received_at": row.received_at.isoformat() if row.received_at else None,
                "error": row.error,
            }
            for row in self.uow.webhooks.list_unprocessed(limit)
        ]
