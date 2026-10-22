"""A failed payout re-enters the analysis as a new shock.

When PayPal reports that a payout item failed, was held, blocked, returned or
went unclaimed, the merchant it was meant for is back in the shortfall the
payout was sized to cover. That is a shock, and it gets the same treatment as
any other: a bounded analysis that proposes a plan, scored by replay.

What this does, and what it refuses to do:

* The shock is **typed** (:class:`FailedPayoutShock`) and sized by the failed
  payout's own **analytic** amount in rupees - never by the sandbox amount - as
  a multiple of the merchant's liquidity slack, which is how the bounded
  analysis sizes every shock.
* The merchant must be in the **live subset** (mapped in the identity table, to
  the receiver the payout went to). If not, the reason is recorded and nothing
  is analysed.
* The analysis is the existing bounded one (``SnapshotStore.analyze``: at most
  5 origins, 12 candidates, 2 actions). It runs in-process, after the webhook
  has been acknowledged, never on a page read.
* The result - including the new plan - is persisted with its provenance. It is
  **never executed**: a new plan has a new hash and needs a fresh approval.
* One analysis per (record, trigger): a redelivered failure is recorded once.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lce import __version__
from lce.data.orm import ExecutionRecordRow, PayPalIdentityRow, ReentryAnalysisRow
from lce.logging import get_logger
from lce.snapshot.analysis import _liquidity_slack
from lce.snapshot.store import (
    MAX_ON_DEMAND_ACTIONS,
    MAX_ON_DEMAND_CANDIDATES,
    MAX_ON_DEMAND_ORIGINS,
    SnapshotStore,
)

logger = get_logger(__name__)

#: The analysis window opens at the moment the failure is learned.
ONSET_HOURS = 0.0


@dataclass(frozen=True, slots=True)
class FailedPayoutShock:
    """The money a merchant was promised did not arrive."""

    merchant_id: str
    kind: str  # always "failed_payout"
    trigger: str  # PayPal's outcome: FAILED, HELD, BLOCKED, RETURNED, UNCLAIMED
    missing_inr: float  # the payout's analytic amount, in rupees
    slack_inr: float
    magnitude_multiple: float
    onset_hours: float
    record_id: str
    source_event_id: str | None


def failed_payout_shock(
    record: ExecutionRecordRow, store: SnapshotStore, *, trigger: str,
    source_event_id: str | None,
) -> FailedPayoutShock:
    missing = float(record.analytic_amount_inr)
    slack = max(_liquidity_slack(store, record.merchant_id), 1.0)
    return FailedPayoutShock(
        merchant_id=record.merchant_id,
        kind="failed_payout",
        trigger=trigger,
        missing_inr=missing,
        slack_inr=slack,
        magnitude_multiple=missing / slack,
        onset_hours=ONSET_HOURS,
        record_id=record.record_id,
        source_event_id=source_event_id,
    )


def _existing(session: Session, record_id: str, trigger: str) -> ReentryAnalysisRow | None:
    return session.execute(
        select(ReentryAnalysisRow).where(
            ReentryAnalysisRow.record_id == record_id, ReentryAnalysisRow.trigger == trigger
        )
    ).scalar_one_or_none()


def reenter(
    session: Session,
    store: SnapshotStore,
    record: ExecutionRecordRow,
    *,
    trigger: str,
    receiver: str | None = None,
    source_event_id: str | None = None,
) -> ReentryAnalysisRow:
    """Analyse one failed payout as a shock, once. Returns the persisted row."""
    already = _existing(session, record.record_id, trigger)
    if already is not None:
        return already

    identity = session.execute(
        select(PayPalIdentityRow).where(PayPalIdentityRow.merchant_id == record.merchant_id)
    ).scalar_one_or_none()
    provenance: dict[str, Any] = {
        "snapshot_id": store.snapshot_id,
        "limits": {
            "max_origins": MAX_ON_DEMAND_ORIGINS,
            "max_candidates": MAX_ON_DEMAND_CANDIDATES,
            "max_actions": MAX_ON_DEMAND_ACTIONS,
        },
        "source_event_id": source_event_id,
        "code_version": __version__,
        "analysed_at": datetime.now(UTC).isoformat(),
        "kind": "computed_now",
    }

    status, reason = "analysed", None
    shock: dict[str, Any] = {}
    result: dict[str, Any] = {}
    if identity is None:
        status, reason = "skipped", (
            f"{record.merchant_id} is not in the live subset (no PayPal sandbox identity mapped)"
        )
    elif receiver is not None and receiver.lower() != identity.receiver.lower():
        status, reason = "skipped", (
            "the failed payout's receiver does not match the identity mapped for "
            f"{record.merchant_id}; not attributed"
        )
    else:
        typed = failed_payout_shock(record, store, trigger=trigger,
                                    source_event_id=source_event_id)
        shock = asdict(typed)
        started = time.perf_counter()
        scenario = store.analyze(
            merchant_ids=[typed.merchant_id],
            magnitude_multiple=typed.magnitude_multiple,
            onset_hours=typed.onset_hours,
            max_actions=MAX_ON_DEMAND_ACTIONS,
        )
        provenance["computed_in_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        result = scenario.model_dump(mode="json")
        if scenario.recommended_intervention is None:
            reason = "the bounded search found no action that reduced the new disruption"

    row = ReentryAnalysisRow(
        analysis_id=f"rea_{uuid.uuid4().hex[:24]}",
        record_id=record.record_id,
        scenario_id=record.scenario_id,
        merchant_id=record.merchant_id,
        trigger=trigger,
        source_event_id=source_event_id,
        status=status,
        reason=reason,
        shock=shock,
        result=result,
        provenance=provenance,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        existing = _existing(session, record.record_id, trigger)
        assert existing is not None
        return existing
    session.commit()
    logger.info("reentry_analysed", record_id=record.record_id, trigger=trigger, status=status)
    return row


def run_scheduled(
    record_id: str,
    trigger: str,
    *,
    receiver: str | None = None,
    source_event_id: str | None = None,
    store: SnapshotStore | None = None,
) -> ReentryAnalysisRow | None:
    """Run one re-entry after the HTTP response, in its own session.

    The request's session is closed by the time a background task runs, so this
    opens its own. Failures are logged, never raised: the webhook was already
    acknowledged and PayPal must not be asked to redeliver because an analysis
    could not run.
    """
    from lce.data.database import get_session_factory
    from lce.snapshot.store import require_store

    try:
        with get_session_factory()() as session:
            record = session.execute(
                select(ExecutionRecordRow).where(ExecutionRecordRow.record_id == record_id)
            ).scalar_one_or_none()
            if record is None:
                logger.warning("reentry_record_missing", record_id=record_id)
                return None
            return reenter(session, store or require_store(), record, trigger=trigger,
                           receiver=receiver, source_event_id=source_event_id)
    except Exception as exc:  # acknowledged already; record the failure, do not raise
        logger.error("reentry_failed", record_id=record_id, trigger=trigger,
                     error=type(exc).__name__)
        return None
