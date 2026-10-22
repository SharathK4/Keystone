"""PayPal webhook ingestion and reconciliation for execution records.

Ingestion mirrors the Razorpay receiver: the caller has already verified the
signature over the raw body; this module persists the event once, applies it,
and reports what happened. Persisting first, under the existing
``(provider, provider_event_id)`` unique key, is the race-safe dedupe: two
concurrent deliveries of one event produce one row, and the loser is told it was
a duplicate.

Applying an event goes through :func:`lce.execution.state.advance`, so duplicate
and out-of-order deliveries cannot regress a record, and a terminal status is
never changed.

Reconciliation answers "what happened to a payout whose outcome is unknown?"
without ever sending money again. If the record has a PayPal batch id it is read
back with ``GET``. If it does not - PayPal never answered the original request -
the batch id is learned from a ``PAYMENT.PAYOUTSBATCH.*`` event carrying
Keystone's ``sender_batch_id``. If no such event has arrived, the record stays
``unknown`` with that reason: what PayPal answers for a batch it never created is
unverified, and resubmitting could pay twice. A retry of a failed payout comes
only from a new plan with its own approval.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lce.data.orm import ExecutionEventRow, ExecutionRecordRow, PayPalWebhookEventRow
from lce.errors import PayPalError
from lce.execution.state import advance
from lce.logging import get_logger
from lce.paypal.client import PayPalClient
from lce.paypal.events import (
    BATCH_PREFIX,
    ITEM_EVENTS,
    REENTRY_EVENTS,
    OtherEvent,
    PayoutEvent,
    classify,
)
from lce.paypal.webhooks import Verification

logger = get_logger(__name__)

#: Non-secret transmission headers kept as provenance.
PROVENANCE_HEADERS = (
    "paypal-transmission-id",
    "paypal-transmission-time",
    "paypal-auth-algo",
    "paypal-cert-url",
)

#: PayPal item ``transaction_status`` (as read back by ``GET``) -> event outcome.
STATUS_TO_OUTCOME = {
    "SUCCESS": "SUCCEEDED",
    "FAILED": "FAILED",
    "ONHOLD": "HELD",
    "BLOCKED": "BLOCKED",
    "RETURNED": "RETURNED",
    "UNCLAIMED": "UNCLAIMED",
    "REFUNDED": "REFUNDED",
    "REVERSED": "REFUNDED",
}


@dataclass(frozen=True, slots=True)
class Reentry:
    """A failure that should be analysed as a new shock, after the response."""

    record_id: str
    trigger: str
    receiver: str | None
    source_event_id: str | None


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: str  # accepted | duplicate | ignored
    event_id: str
    event_type: str
    record_id: str | None
    record_status: str | None
    applied: bool
    reason: str | None
    reentry: Reentry | None


def _now() -> datetime:
    return datetime.now(UTC)


def _ledger(
    session: Session, record: ExecutionRecordRow, kind: str, detail: Mapping[str, Any]
) -> None:
    session.add(
        ExecutionEventRow(
            event_id=f"evt_{uuid.uuid4().hex[:24]}",
            scenario_id=record.scenario_id,
            plan_hash=record.plan_hash,
            record_id=record.record_id,
            kind=kind,
            status=record.status,
            detail={k: v for k, v in detail.items() if v is not None},
        )
    )


def find_record(session: Session, evt: PayoutEvent) -> ExecutionRecordRow | None:
    """The record an event is about. Keystone's own ids first, PayPal's after."""
    model = ExecutionRecordRow
    keys = [k for k in (evt.sender_item_id, evt.sender_batch_id) if k]
    for key in keys:
        row = session.execute(
            select(model).where(model.idempotency_key == key)
        ).scalar_one_or_none()
        if row is not None:
            return row
    if evt.payout_item_id:
        row = session.execute(
            select(model).where(model.provider_item_id == evt.payout_item_id)
        ).scalar_one_or_none()
        if row is not None:
            return row
    if evt.payout_batch_id:
        return session.execute(
            select(model).where(model.provider_batch_id == evt.payout_batch_id)
        ).scalars().first()
    return None


def apply_payout_fact(
    session: Session,
    record: ExecutionRecordRow,
    *,
    outcome: str,
    implied_status: str,
    paypal_status: str,
    scope: str,
    payout_batch_id: str | None,
    payout_item_id: str | None,
    error_name: str | None,
    receiver: str | None,
    source: str,
    source_event_id: str | None,
) -> tuple[bool, str, Reentry | None]:
    """Apply one fact about a payout. Returns (applied, reason, reentry)."""
    learned = []
    if payout_batch_id and not record.provider_batch_id:
        record.provider_batch_id = payout_batch_id
        learned.append("payout_batch_id")
    if payout_item_id and not record.provider_item_id:
        record.provider_item_id = payout_item_id
        learned.append("payout_item_id")

    step = advance(record.status, implied_status)
    if step.changed:
        record.status = step.status
        record.provider_status = paypal_status
        if step.status in {"succeeded", "pending", "submitted"}:
            record.reason = None
        else:
            record.reason = f"PayPal {scope} {outcome.lower()}" + (
                f": {error_name}" if error_name else ""
            )
    _ledger(
        session,
        record,
        "provider_status_applied" if step.changed else "provider_status_not_applied",
        {
            "source": source,
            "source_event_id": source_event_id,
            "outcome": outcome,
            "paypal_status": paypal_status,
            "reason": step.reason,
            "learned": learned or None,
        },
    )
    reentry = None
    if step.changed and scope == "item" and outcome in REENTRY_EVENTS:
        reentry = Reentry(record.record_id, outcome, receiver, source_event_id)
    return step.changed, step.reason, reentry


def ingest(
    session: Session,
    *,
    event: dict[str, Any],
    raw_body: bytes,
    headers: Mapping[str, str],
    verification: Verification,
) -> IngestResult:
    """Persist one verified event once, then apply it. Commits."""
    fact = classify(event)
    h = {k.lower(): v for k, v in headers.items()}
    row = PayPalWebhookEventRow(
        event_id=fact.event_id,
        event_type=fact.event_type[:64],
        verification={"method": "self", "verified": verification.verified,
                      "reason": verification.reason},
        transmission={k: h.get(k) for k in PROVENANCE_HEADERS},
        raw_sha256=hashlib.sha256(raw_body).hexdigest(),
        payload=event,
        processed=False,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        seen = session.execute(
            select(PayPalWebhookEventRow).where(PayPalWebhookEventRow.event_id == fact.event_id)
        ).scalar_one_or_none()
        if seen is None:
            raise  # not a duplicate: a genuine failure, surfaced as one
        return IngestResult("duplicate", fact.event_id, fact.event_type, None, None, False,
                            "this event id was already received", None)

    if isinstance(fact, OtherEvent):
        row.processed, row.processed_at = True, _now()
        row.outcome = "ignored: not an event Keystone acts on"
        session.commit()
        logger.info("paypal_webhook_ignored", event_type=fact.event_type)
        return IngestResult("ignored", fact.event_id, fact.event_type, None, None, False,
                            row.outcome, None)

    record = find_record(session, fact)
    if record is None:
        row.processed, row.processed_at = True, _now()
        row.outcome = "no execution record matches this payout"
        session.commit()
        return IngestResult("accepted", fact.event_id, fact.event_type, None, None, False,
                            row.outcome, None)

    applied, reason, reentry = apply_payout_fact(
        session, record,
        outcome=fact.outcome, implied_status=fact.implied_status,
        paypal_status=fact.paypal_status, scope=fact.scope,
        payout_batch_id=fact.payout_batch_id, payout_item_id=fact.payout_item_id,
        error_name=fact.error_name, receiver=fact.receiver,
        source="webhook", source_event_id=fact.event_id,
    )
    row.processed, row.processed_at = True, _now()
    row.outcome = reason
    session.commit()
    return IngestResult("accepted", fact.event_id, fact.event_type, record.record_id,
                        record.status, applied, reason, reentry)


# ----------------------------------------------------------------- reconcile


def batch_id_from_events(session: Session, sender_batch_id: str) -> str | None:
    """A PayPal batch id learned from a stored batch event carrying our key."""
    rows = session.execute(
        select(PayPalWebhookEventRow).where(
            PayPalWebhookEventRow.event_type.like(f"{BATCH_PREFIX}%"),
        )
    ).scalars()
    for row in rows:
        fact = classify(row.payload or {})
        if isinstance(fact, PayoutEvent) and fact.sender_batch_id == sender_batch_id:
            return fact.payout_batch_id
    return None


def reconcile(
    session: Session, client: PayPalClient, record: ExecutionRecordRow
) -> tuple[ExecutionRecordRow, Reentry | None]:
    """Read a payout's state back from PayPal. Never sends money. Commits."""
    if record.status == "planned":
        return record, None

    if not record.provider_batch_id:
        learned = batch_id_from_events(session, record.idempotency_key)
        if learned is None:
            record.reason = (
                "outcome unknown: no PayPal batch is known for this payout yet. "
                "Waiting for PayPal's batch event; nothing is resubmitted."
            )
            _ledger(session, record, "reconcile_unresolved", {"reason": record.reason})
            session.commit()
            return record, None
        record.provider_batch_id = learned
        _ledger(session, record, "batch_id_learned", {"payout_batch_id": learned,
                                                      "source": "stored batch event"})

    try:
        batch = client.get_payout(record.provider_batch_id)
    except PayPalError as exc:
        _ledger(session, record, "reconcile_failed",
                {"name": exc.context.get("name"), "debug_id": exc.context.get("debug_id")})
        session.commit()
        raise

    item = next(
        (
            i for i in batch.get("items", [])
            if str((i.get("payout_item") or {}).get("sender_item_id", ""))
            == record.idempotency_key[:63]
        ),
        None,
    )
    reentry = None
    if item is not None:
        paypal_status = str(item.get("transaction_status", "")).upper()
        outcome = STATUS_TO_OUTCOME.get(paypal_status)
        if outcome is not None:
            errors = item.get("errors") if isinstance(item.get("errors"), dict) else {}
            _, _, reentry = apply_payout_fact(
                session, record, outcome=outcome, implied_status=ITEM_EVENTS[outcome][0],
                paypal_status=paypal_status, scope="item",
                payout_batch_id=None, payout_item_id=str(item.get("payout_item_id") or "") or None,
                error_name=str(errors.get("name") or "") or None,
                receiver=str((item.get("payout_item") or {}).get("receiver") or "") or None,
                source="reconcile", source_event_id=None,
            )
        elif paypal_status == "PENDING":
            apply_payout_fact(
                session, record, outcome="PENDING", implied_status="pending",
                paypal_status=paypal_status, scope="item", payout_batch_id=None,
                payout_item_id=str(item.get("payout_item_id") or "") or None,
                error_name=None, receiver=None, source="reconcile", source_event_id=None,
            )
    else:
        header = batch.get("batch_header") or {}
        batch_status = str(header.get("batch_status", "")).upper()
        implied = {"DENIED": "failed", "CANCELED": "failed", "PENDING": "pending",
                   "PROCESSING": "pending", "SUCCESS": "submitted"}.get(batch_status)
        if implied:
            apply_payout_fact(
                session, record, outcome=batch_status, implied_status=implied,
                paypal_status=batch_status, scope="batch", payout_batch_id=None,
                payout_item_id=None, error_name=None, receiver=None, source="reconcile",
                source_event_id=None,
            )
    session.commit()
    return record, reentry
