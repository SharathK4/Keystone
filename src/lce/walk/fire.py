"""Firing the walk's event: one failed payout through the real pipeline.

Whatever the source - a recorded PayPal delivery or a development event - the
bytes go through the same three steps a live webhook does:

1. :func:`~lce.paypal.webhooks.verify_webhook` against the certificate and
   webhook id (nothing happens to an event that does not verify);
2. :func:`~lce.execution.webhooks.ingest`, which records the event once and
   moves the trigger record through the state machine;
3. the failed-payout re-entry: the merchant's missing money becomes a shock and
   the bounded analysis runs on it, in process, timed.

The first fire for a record persists the re-entry analysis exactly as a webhook
would. A later fire finds that analysis already recorded and computes the same
shock again for the page, so every fire shows an analysis computed now.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lce.data.orm import (
    ExecutionApprovalRow,
    ExecutionRecordRow,
    PayPalIdentityRow,
    ReentryAnalysisRow,
)
from lce.errors import LCEError
from lce.execution.reentry import FailedPayoutShock, failed_payout_shock, reenter
from lce.execution.webhooks import ingest
from lce.paypal.events import ITEM_PREFIX, REENTRY_EVENTS
from lce.paypal.money import DemoScale
from lce.paypal.webhooks import CertLoader, verify_webhook
from lce.snapshot.models import ScenarioSnapshot
from lce.snapshot.store import MAX_ON_DEMAND_ACTIONS, SnapshotStore
from lce.walk.dev_events import DEV_STAMP, DEV_WEBHOOK_ID, dev_item_event, dev_signer
from lce.walk.recording import SOURCE_SIMULATOR, RecordedEvent
from lce.walk.thread import (
    PROVIDER_NAME,
    TRIGGER_ACTION_KEY,
    TRIGGER_ACTION_TYPE,
    WALK_SCENARIO_ID,
    WalkSettings,
    ensure_trigger_record,
    find_trigger_record,
    trigger,
)

SOURCE_RECORDED = "recorded"
SOURCE_DEVELOPMENT = "development"
RECORDED_STAMP = "recorded PayPal event, processed live"
SIMULATED_STAMP = "simulated by PayPal's webhook simulator, processed live"


class FireRefused(LCEError):
    """The event could not be fired, with the reason. Nothing was changed."""

    code = "fire_refused"
    http_status = 409


@dataclass(frozen=True, slots=True)
class FireOutcome:
    source: str
    stamp: str
    event_id: str
    event_type: str
    outcome: str
    transmission_time: str | None
    processed_at: str
    verification: str
    ingest_status: str
    record_status: str | None
    shock: FailedPayoutShock
    analysis: ScenarioSnapshot
    computed_in_ms: float
    reentry_status: str
    reentry_reason: str | None
    reentry_id: str | None


def _analyse(store: SnapshotStore, shock: FailedPayoutShock) -> tuple[ScenarioSnapshot, float]:
    started = time.perf_counter()
    scenario = store.analyze(
        merchant_ids=[shock.merchant_id],
        magnitude_multiple=shock.magnitude_multiple,
        onset_hours=shock.onset_hours,
        max_actions=MAX_ON_DEMAND_ACTIONS,
    )
    return scenario, round((time.perf_counter() - started) * 1000.0, 1)


def process(
    session: Session,
    store: SnapshotStore,
    *,
    raw_body: bytes,
    headers: Mapping[str, str],
    webhook_id: str,
    load_cert: CertLoader,
    source: str,
    stamp: str,
) -> FireOutcome:
    """Verify, ingest and analyse one failed-payout delivery for the walk."""
    verification = verify_webhook(headers, raw_body, webhook_id=webhook_id, load_cert=load_cert)
    if not verification.verified:
        raise FireRefused("the event did not verify, so nothing was processed",
                          reason=verification.reason)
    event: dict[str, Any] = json.loads(raw_body)
    event_type = str(event.get("event_type", ""))
    outcome = event_type.removeprefix(ITEM_PREFIX) if event_type.startswith(ITEM_PREFIX) else ""
    if outcome not in REENTRY_EVENTS:
        raise FireRefused("the event is not a failed payout", event_type=event_type)

    result = ingest(session, event=event, raw_body=raw_body, headers=headers,
                    verification=verification)
    record = session.execute(
        select(ExecutionRecordRow).where(ExecutionRecordRow.record_id == result.record_id)
    ).scalar_one_or_none() if result.record_id else None
    if record is None or record.scenario_id != WALK_SCENARIO_ID:
        raise FireRefused("the event does not belong to the walk's trigger payout",
                          event_id=result.event_id)

    receiver = str(((event.get("resource") or {}).get("payout_item") or {}).get("receiver")
                   or "") or None
    already = session.execute(
        select(ReentryAnalysisRow).where(
            ReentryAnalysisRow.record_id == record.record_id,
            ReentryAnalysisRow.trigger == outcome,
        )
    ).scalar_one_or_none()
    shock = failed_payout_shock(record, store, trigger=outcome, source_event_id=result.event_id)
    analysis: ScenarioSnapshot | None = None
    ms = 0.0
    if already is None:
        row = reenter(session, store, record, trigger=outcome, receiver=receiver,
                      source_event_id=result.event_id)
        if row.status == "analysed" and row.result:
            analysis = ScenarioSnapshot.model_validate(row.result)
            ms = float(row.provenance.get("computed_in_ms") or 0.0)
    else:
        row = already
    if analysis is None:
        analysis, ms = _analyse(store, shock)

    return FireOutcome(
        source=source,
        stamp=stamp,
        event_id=result.event_id,
        event_type=event_type,
        outcome=outcome,
        transmission_time={k.lower(): v for k, v in headers.items()}.get(
            "paypal-transmission-time"),
        processed_at=datetime.now(UTC).isoformat(),
        verification=verification.reason,
        ingest_status=result.status,
        record_status=record.status,
        shock=shock,
        analysis=analysis,
        computed_in_ms=ms,
        reentry_status=row.status,
        reentry_reason=row.reason,
        reentry_id=row.analysis_id,
    )


# ------------------------------------------------------------------ sources


def fire_development(
    session: Session, store: SnapshotStore, *, scale: DemoScale, walk: WalkSettings
) -> FireOutcome:
    """The walk's failure as a locally signed development event."""
    trig = trigger(store, walk, scale)
    record = ensure_trigger_record(
        session, trig, approver="development walk",
        reason="walk trigger for development; never sent to PayPal",
    )
    identity = session.execute(
        select(PayPalIdentityRow).where(PayPalIdentityRow.merchant_id == record.merchant_id)
    ).scalar_one_or_none()
    event = dev_item_event(record, receiver=identity.receiver if identity else None)
    raw = json.dumps(event, separators=(",", ":")).encode()
    signer = dev_signer()
    return process(session, store, raw_body=raw, headers=signer.headers(raw),
                   webhook_id=DEV_WEBHOOK_ID, load_cert=signer.loader,
                   source=SOURCE_DEVELOPMENT, stamp=DEV_STAMP)


def restore_trigger(session: Session, data: Mapping[str, Any]) -> ExecutionRecordRow:
    """The recorded trigger's record, recreated in a database that never saw it."""
    key = str(data["idempotency_key"])
    existing = find_trigger_record(session, key)
    if existing is not None:
        return existing
    approval = ExecutionApprovalRow(
        approval_id=str(data["approval_id"]),
        plan_hash=str(data["plan_hash"]),
        snapshot_id=str(data["snapshot_id"]),
        scenario_id=WALK_SCENARIO_ID,
        provider=PROVIDER_NAME,
        approver=str(data.get("approver") or "recorded walk")[:80],
        executed_total_minor=int(data["executed_minor"]),
        currency=str(data["currency"]),
        demo_scale=str(data["demo_scale"]),
        detail={"actions": [TRIGGER_ACTION_KEY], "kind": "walk_trigger", "restored": True},
    )
    record = ExecutionRecordRow(
        record_id=str(data["record_id"]),
        idempotency_key=key,
        approval_id=approval.approval_id,
        plan_hash=approval.plan_hash,
        scenario_id=WALK_SCENARIO_ID,
        action_key=TRIGGER_ACTION_KEY,
        merchant_id=str(data["merchant_id"]),
        action_type=TRIGGER_ACTION_TYPE,
        analytic_amount_inr=str(data["analytic_amount_inr"]),
        executed_minor=int(data["executed_minor"]),
        currency=str(data["currency"]),
        provider=PROVIDER_NAME,
        status="submitted",
        reason="restored from the recorded trigger",
        provider_batch_id=data.get("provider_batch_id"),
        provider_item_id=data.get("provider_item_id"),
    )
    session.add_all([approval, record])
    session.commit()
    return record


def trigger_export(record: ExecutionRecordRow, approval: ExecutionApprovalRow) -> dict[str, Any]:
    """What a fixture keeps of the trigger, so a fresh database can restore it."""
    return {
        "record_id": record.record_id,
        "approval_id": approval.approval_id,
        "approver": approval.approver,
        "idempotency_key": record.idempotency_key,
        "plan_hash": record.plan_hash,
        "snapshot_id": approval.snapshot_id,
        "merchant_id": record.merchant_id,
        "analytic_amount_inr": record.analytic_amount_inr,
        "executed_minor": record.executed_minor,
        "currency": record.currency,
        "demo_scale": approval.demo_scale,
        "provider_batch_id": record.provider_batch_id,
        "provider_item_id": record.provider_item_id,
    }


def fire_recorded(
    session: Session, store: SnapshotStore, recorded: RecordedEvent, *, webhook_id: str | None
) -> FireOutcome:
    """The committed real delivery, verified and processed as if it just arrived."""
    restore_trigger(session, recorded.trigger)
    wid = recorded.webhook_id_for or webhook_id
    if not wid:
        raise FireRefused("PAYPAL_WEBHOOK_ID is not configured, so the recorded PayPal event "
                          "cannot be verified")
    stamp = SIMULATED_STAMP if recorded.source == SOURCE_SIMULATOR else RECORDED_STAMP
    return process(session, store, raw_body=recorded.raw_body, headers=recorded.headers,
                   webhook_id=wid, load_cert=recorded.cert_loader(),
                   source=SOURCE_RECORDED, stamp=stamp)
