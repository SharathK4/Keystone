"""Reading PayPal webhook events into typed facts.

Only Payouts events change anything in this system. Everything else is recorded
and acknowledged, never acted on.

Event names are PayPal's
(https://developer.paypal.com/api/rest/webhooks/event-names/):
item events ``PAYMENT.PAYOUTS-ITEM.{SUCCEEDED, FAILED, HELD, BLOCKED, RETURNED,
UNCLAIMED, REFUNDED, CANCELED}`` and batch events
``PAYMENT.PAYOUTSBATCH.{PROCESSING, SUCCESS, DENIED}``.

An item event's ``resource`` carries ``payout_item_id``, ``payout_batch_id``,
``transaction_status`` and the original ``payout_item`` including the
``sender_item_id`` Keystone set; a batch event's ``resource`` carries
``batch_header`` with ``payout_batch_id`` and the ``sender_batch_header`` holding
Keystone's ``sender_batch_id``. Those two ids are how an event finds its
execution record, and the batch event is how an execution whose outcome was
unknown learns its PayPal batch id without anyone resubmitting money.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ITEM_PREFIX = "PAYMENT.PAYOUTS-ITEM."
BATCH_PREFIX = "PAYMENT.PAYOUTSBATCH."

#: Item event -> the record status it implies, and PayPal's item status name.
ITEM_EVENTS: dict[str, tuple[str, str]] = {
    "SUCCEEDED": ("succeeded", "SUCCESS"),
    "FAILED": ("failed", "FAILED"),
    "HELD": ("held", "ONHOLD"),
    "BLOCKED": ("failed", "BLOCKED"),
    "RETURNED": ("failed", "RETURNED"),
    "UNCLAIMED": ("failed", "UNCLAIMED"),
    "REFUNDED": ("failed", "REFUNDED"),
    "CANCELED": ("failed", "CANCELED"),
}

#: Batch event -> the record status it implies for every item in the batch.
BATCH_EVENTS: dict[str, str] = {
    "PROCESSING": "pending",
    "SUCCESS": "submitted",
    "DENIED": "failed",
}

#: Item outcomes that put a merchant back into the shortfall the payout was
#: meant to cover, and so re-enter the analysis as a new shock.
REENTRY_EVENTS = frozenset({"FAILED", "HELD", "BLOCKED", "RETURNED", "UNCLAIMED"})


@dataclass(frozen=True, slots=True)
class PayoutEvent:
    event_id: str
    event_type: str
    scope: str  # "item" | "batch"
    outcome: str  # the suffix: SUCCEEDED, FAILED, ..., PROCESSING, SUCCESS, DENIED
    implied_status: str
    paypal_status: str
    payout_batch_id: str | None
    payout_item_id: str | None
    sender_batch_id: str | None
    sender_item_id: str | None
    receiver: str | None
    error_name: str | None

    @property
    def reenters(self) -> bool:
        return self.scope == "item" and self.outcome in REENTRY_EVENTS


@dataclass(frozen=True, slots=True)
class OtherEvent:
    """Any event Keystone does not act on. Recorded, acknowledged, ignored."""

    event_id: str
    event_type: str


def _str(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def classify(event: dict[str, Any]) -> PayoutEvent | OtherEvent:
    """Turn a parsed webhook body into a typed fact. Never raises on content."""
    event_id = str(event.get("id") or "")
    event_type = str(event.get("event_type") or "")
    resource = _dict(event.get("resource"))

    if event_type.startswith(ITEM_PREFIX):
        outcome = event_type[len(ITEM_PREFIX):]
        if outcome in ITEM_EVENTS:
            implied, paypal_status = ITEM_EVENTS[outcome]
            item = _dict(resource.get("payout_item"))
            errors = _dict(resource.get("errors"))
            return PayoutEvent(
                event_id=event_id,
                event_type=event_type,
                scope="item",
                outcome=outcome,
                implied_status=implied,
                paypal_status=str(resource.get("transaction_status") or paypal_status),
                payout_batch_id=_str(resource.get("payout_batch_id")),
                payout_item_id=_str(resource.get("payout_item_id")),
                sender_batch_id=_str(resource.get("sender_batch_id")),
                sender_item_id=_str(item.get("sender_item_id")),
                receiver=_str(item.get("receiver")),
                error_name=_str(errors.get("name")),
            )

    if event_type.startswith(BATCH_PREFIX):
        outcome = event_type[len(BATCH_PREFIX):]
        if outcome in BATCH_EVENTS:
            header = _dict(resource.get("batch_header"))
            sender = _dict(header.get("sender_batch_header"))
            return PayoutEvent(
                event_id=event_id,
                event_type=event_type,
                scope="batch",
                outcome=outcome,
                implied_status=BATCH_EVENTS[outcome],
                paypal_status=str(header.get("batch_status") or outcome),
                payout_batch_id=_str(header.get("payout_batch_id")),
                payout_item_id=None,
                sender_batch_id=_str(sender.get("sender_batch_id")),
                sender_item_id=None,
                receiver=None,
                error_name=None,
            )

    return OtherEvent(event_id=event_id, event_type=event_type)
