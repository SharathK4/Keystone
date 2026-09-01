"""The canonical ingestion pipeline.

    raw record
      -> validate      is this a well-formed, usable record at all?
      -> normalize     units, enums, timestamps into canonical form
      -> map entities  provider ids -> our merchant ids
      -> deduplicate   has this record already been ingested?
      -> canonical temporal transaction event

Each stage can reject, and a rejection is a *recorded outcome* rather than an
exception: a batch of 10,000 imported payments will always contain some rows we
cannot map, and failing the whole batch on the first one makes the importer
unusable. :class:`PipelineResult` therefore carries accepted events alongside
per-record rejections with reasons, so an operator can see exactly what did not
make it in and why.

Provenance
----------
Every accepted event gets a :class:`Provenance` record: the source system, the
raw provider id, the raw payload hash, the ingestion time, and the pipeline
version. That is what makes "where did this number come from?" answerable months
later - the canonical event alone cannot answer it, because normalization is
lossy by design.

Money and time are preserved in *both* forms. The canonical event carries
rupees and simulation-hours because that is what the maths core consumes, and
the provenance record keeps the original paise integer and the original Unix
timestamp, so a rounding dispute can always be settled against the source.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from lce.domain.base import to_sim_time
from lce.domain.events import EXTERNAL_SINK, PaymentEvent
from lce.errors import ValidationError
from lce.logging import get_logger
from lce.razorpay.mapper import (
    MerchantResolver,
    map_channel,
    map_status,
    paise_to_rupees,
)

logger = get_logger(__name__)

PIPELINE_VERSION = "1.0.0"


class SourceSystem(StrEnum):
    RAZORPAY_WEBHOOK = "razorpay_webhook"
    RAZORPAY_API = "razorpay_api"
    SYNTHETIC = "synthetic"
    MANUAL = "manual"


class RejectionReason(StrEnum):
    """Why a raw record did not become a canonical event."""

    MISSING_ID = "missing_id"
    MISSING_AMOUNT = "missing_amount"
    NON_POSITIVE_AMOUNT = "non_positive_amount"
    MISSING_TIMESTAMP = "missing_timestamp"
    UNMAPPABLE_COUNTERPARTY = "unmappable_counterparty"
    SELF_PAYMENT = "self_payment"
    DUPLICATE = "duplicate"
    UNSUPPORTED_STATUS = "unsupported_status"
    MALFORMED = "malformed"


# Statuses that represent money actually moving. An authorized-but-uncaptured
# payment is a reservation, not a cash flow, and counting it as one would
# overstate every merchant's inflow.
SETTLED_STATUSES = frozenset({"captured", "processed", "settled"})


@dataclass(slots=True)
class Provenance:
    """The audit trail linking a canonical event back to its raw source."""

    event_id: str
    source_system: str
    source_id: str
    source_payload_hash: str
    ingested_at: str
    pipeline_version: str = PIPELINE_VERSION
    raw_amount_minor: int | None = None
    raw_timestamp: int | None = None
    raw_currency: str | None = None
    source_reference: str | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source_system": self.source_system,
            "source_id": self.source_id,
            "source_payload_hash": self.source_payload_hash,
            "ingested_at": self.ingested_at,
            "pipeline_version": self.pipeline_version,
            "raw_amount_minor": self.raw_amount_minor,
            "raw_timestamp": self.raw_timestamp,
            "raw_currency": self.raw_currency,
            "source_reference": self.source_reference,
            "notes": self.notes,
        }


@dataclass(slots=True)
class Rejection:
    """A raw record that did not survive the pipeline, and why."""

    source_id: str | None
    reason: RejectionReason
    detail: str = ""
    stage: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "reason": str(self.reason),
            "detail": self.detail,
            "stage": self.stage,
        }


@dataclass(slots=True)
class PipelineResult:
    """Outcome of running a batch through the pipeline."""

    events: list[PaymentEvent] = field(default_factory=list)
    provenance: list[Provenance] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)

    @property
    def accepted(self) -> int:
        return len(self.events)

    @property
    def rejected(self) -> int:
        return len(self.rejections)

    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rejection in self.rejections:
            key = str(rejection.reason)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def summary(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "acceptance_rate": (
                self.accepted / (self.accepted + self.rejected)
                if (self.accepted + self.rejected)
                else 0.0
            ),
            "rejection_reasons": self.reason_counts(),
        }

    def extend(self, other: PipelineResult) -> None:
        self.events.extend(other.events)
        self.provenance.extend(other.provenance)
        self.rejections.extend(other.rejections)


def payload_hash(payload: dict[str, Any]) -> str:
    """Stable hash of a raw payload, for provenance and change detection."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


# ------------------------------------------------------------------- stages


def validate_record(record: dict[str, Any]) -> Rejection | None:
    """Stage 1: is this a well-formed, usable payment record?"""
    if not isinstance(record, dict):
        return Rejection(None, RejectionReason.MALFORMED, "record is not an object", "validate")

    source_id = record.get("id")
    if not source_id:
        return Rejection(None, RejectionReason.MISSING_ID, "no 'id' field", "validate")

    if "amount" not in record or record.get("amount") is None:
        return Rejection(
            str(source_id), RejectionReason.MISSING_AMOUNT, "no 'amount' field", "validate"
        )
    try:
        amount = float(record["amount"])
    except (TypeError, ValueError):
        return Rejection(
            str(source_id),
            RejectionReason.MISSING_AMOUNT,
            f"amount is not numeric: {record['amount']!r}",
            "validate",
        )
    if amount <= 0:
        return Rejection(
            str(source_id),
            RejectionReason.NON_POSITIVE_AMOUNT,
            f"amount is {amount}",
            "validate",
        )

    if record.get("created_at") is None:
        return Rejection(
            str(source_id),
            RejectionReason.MISSING_TIMESTAMP,
            "no 'created_at' field",
            "validate",
        )

    status = str(record.get("status", "")).lower()
    if status and status not in SETTLED_STATUSES:
        return Rejection(
            str(source_id),
            RejectionReason.UNSUPPORTED_STATUS,
            f"status {status!r} does not represent settled cash",
            "validate",
        )
    return None


@dataclass(slots=True)
class NormalizedRecord:
    """Stage 2 output: canonical units and types, entities not yet resolved."""

    source_id: str
    amount_rupees: float
    raw_amount_minor: int
    occurred_at: datetime
    raw_timestamp: int
    currency: str
    method: str | None
    status: str
    payer_hint: str | None
    payee_hint: str | None
    order_id: str | None


def normalize_record(record: dict[str, Any]) -> NormalizedRecord:
    """Stage 2: units, timestamps and enums into canonical form.

    Raw minor units and the raw epoch are carried through rather than discarded,
    because provenance needs the pre-conversion values to be auditable.
    """
    raw_amount = int(record["amount"])
    raw_ts = int(record["created_at"])
    notes = record.get("notes") or {}

    return NormalizedRecord(
        source_id=str(record["id"]),
        amount_rupees=paise_to_rupees(raw_amount),
        raw_amount_minor=raw_amount,
        occurred_at=datetime.fromtimestamp(raw_ts, tz=UTC),
        raw_timestamp=raw_ts,
        currency=str(record.get("currency", "INR")),
        method=record.get("method"),
        status=str(record.get("status", "captured")),
        payer_hint=(
            notes.get("payer_merchant_id")
            or record.get("customer_id")
            or record.get("contact_id")
            or record.get("source")
        ),
        payee_hint=(
            notes.get("payee_merchant_id")
            or record.get("account_id")
            or record.get("recipient")
        ),
        order_id=record.get("order_id"),
    )


def map_entities(
    normalized: NormalizedRecord,
    resolver: MerchantResolver,
    *,
    default_payee: str | None = None,
    allow_external: bool = True,
) -> tuple[str, str] | Rejection:
    """Stage 3: provider identifiers to our merchant ids.

    An unresolvable counterparty maps to the external sink when
    ``allow_external`` is set - cash genuinely does enter the network from
    outside it, and refusing those records would silently drop most consumer
    revenue. When both sides are unresolvable there is no merchant involved at
    all, so the record is rejected.
    """
    payer = resolver(normalized.payer_hint)
    payee = resolver(normalized.payee_hint) or default_payee

    if payer is None and payee is None:
        return Rejection(
            normalized.source_id,
            RejectionReason.UNMAPPABLE_COUNTERPARTY,
            f"neither {normalized.payer_hint!r} nor {normalized.payee_hint!r} resolved",
            "map_entities",
        )
    if not allow_external and (payer is None or payee is None):
        return Rejection(
            normalized.source_id,
            RejectionReason.UNMAPPABLE_COUNTERPARTY,
            "external counterparties are not permitted for this import",
            "map_entities",
        )

    payer = payer or EXTERNAL_SINK
    payee = payee or EXTERNAL_SINK
    if payer == payee:
        return Rejection(
            normalized.source_id,
            RejectionReason.SELF_PAYMENT,
            f"payer and payee both resolve to {payer!r}",
            "map_entities",
        )
    return payer, payee


def run_pipeline(
    records: Iterable[dict[str, Any]],
    *,
    resolver: MerchantResolver,
    epoch: datetime,
    source_system: SourceSystem = SourceSystem.RAZORPAY_API,
    seen_source_ids: set[str] | None = None,
    default_payee: str | None = None,
    allow_external: bool = True,
    settlement_lag_hours: float = 0.0,
) -> PipelineResult:
    """Run a batch of raw records through every stage.

    ``seen_source_ids`` carries ids already present in the store; it is mutated
    as the batch is processed so duplicates *within* the batch are caught too,
    not just duplicates against history.
    """
    seen = seen_source_ids if seen_source_ids is not None else set()
    result = PipelineResult()

    for record in records:
        rejection = validate_record(record)
        if rejection is not None:
            result.rejections.append(rejection)
            continue

        try:
            normalized = normalize_record(record)
        except (TypeError, ValueError, KeyError) as exc:
            result.rejections.append(
                Rejection(
                    str(record.get("id")),
                    RejectionReason.MALFORMED,
                    f"normalization failed: {exc}",
                    "normalize",
                )
            )
            continue

        if normalized.source_id in seen:
            result.rejections.append(
                Rejection(
                    normalized.source_id,
                    RejectionReason.DUPLICATE,
                    "source id already ingested",
                    "deduplicate",
                )
            )
            continue

        mapped = map_entities(
            normalized,
            resolver,
            default_payee=default_payee,
            allow_external=allow_external,
        )
        if isinstance(mapped, Rejection):
            result.rejections.append(mapped)
            continue
        payer, payee = mapped

        event = PaymentEvent(
            payer_id=payer,
            payee_id=payee,
            amount=normalized.amount_rupees,
            t=to_sim_time(normalized.occurred_at, epoch),
            channel=map_channel(normalized.method),
            status=map_status(normalized.status),
            settlement_lag_hours=settlement_lag_hours,
            external_id=normalized.source_id,
            is_synthetic=False,
            metadata={
                "source": str(source_system),
                "order_id": normalized.order_id,
                "currency": normalized.currency,
                "method": normalized.method,
                "unresolved_payer": payer == EXTERNAL_SINK,
                "unresolved_payee": payee == EXTERNAL_SINK,
            },
        )
        result.events.append(event)
        result.provenance.append(
            Provenance(
                event_id=event.event_id,
                source_system=str(source_system),
                source_id=normalized.source_id,
                source_payload_hash=payload_hash(record),
                ingested_at=datetime.now(tz=UTC).isoformat(),
                raw_amount_minor=normalized.raw_amount_minor,
                raw_timestamp=normalized.raw_timestamp,
                raw_currency=normalized.currency,
                source_reference=normalized.order_id,
            )
        )
        seen.add(normalized.source_id)

    logger.info("pipeline_batch_complete", source=str(source_system), **result.summary())
    return result


def reconcile_totals(
    result: PipelineResult, records: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Check that accepted value matches the raw value of accepted records.

    A unit-conversion slip (paise read as rupees) is invisible in row counts and
    catastrophic in the maths, so the conversion is reconciled explicitly rather
    than trusted.
    """
    accepted_ids = {p.source_id for p in result.provenance}
    # Deduplicate by source id before summing. A batch may legitimately contain
    # the same record twice (an overlapping import window); exactly one instance
    # was accepted, so counting both here would fail a reconciliation that is
    # actually correct.
    raw_by_id: dict[str, int] = {}
    for record in records:
        source_id = str(record.get("id"))
        if source_id in accepted_ids and source_id not in raw_by_id:
            raw_by_id[source_id] = int(record["amount"])
    raw_minor = sum(raw_by_id.values())
    canonical_rupees = sum(e.amount for e in result.events)
    expected_rupees = paise_to_rupees(raw_minor)
    drift = abs(canonical_rupees - expected_rupees)

    if drift > 0.01:
        raise ValidationError(
            "pipeline value reconciliation failed: canonical total "
            f"{canonical_rupees:.2f} != expected {expected_rupees:.2f}",
            drift=drift,
        )
    return {
        "raw_amount_minor": raw_minor,
        "canonical_amount": canonical_rupees,
        "expected_amount": expected_rupees,
        "drift": drift,
        "reconciled": True,
    }
