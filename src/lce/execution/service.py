"""Approve, execute, reconcile and audit - the only path that moves money.

The lifecycle, and the guard on each step:

``plan``      read from the frozen snapshot (:mod:`lce.execution.plan`), priced at
              the declared demo scale, fingerprinted. Nothing is optimised here.
``approve``   a named human approves **one exact plan hash**. If the plan has
              changed since it was shown - a different snapshot, scale, amount
              or recipient - the hash differs and the approval is refused.
``execute``   requires an approval whose hash still matches, a complete plan,
              a sandbox configuration and a recipient for every mapped action.
              Each action gets an idempotency key derived from (plan hash,
              action); the database refuses a second record for that key, and
              the same key is sent to PayPal as ``PayPal-Request-Id`` and
              ``sender_batch_id``, so PayPal refuses a duplicate too. A record is
              committed *before* the request is sent, so a crash mid-flight
              leaves a trace rather than a mystery.
``refresh``   an explicit, human-initiated reconcile: ``GET`` the payout and
              map PayPal's item status onto the record through the state
              machine. It **never resubmits**: an action whose outcome is
              unknown learns its batch id from PayPal's batch event, or stays
              unknown. A retry needs a new plan and a fresh approval.
``ledger``    the append-only audit trail.

There is no automatic retry anywhere in this module.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from lce.config import PayPalSettings, get_paypal_settings
from lce.data.orm import (
    ExecutionApprovalRow,
    ExecutionEventRow,
    ExecutionRecordRow,
    PayPalIdentityRow,
)
from lce.errors import (
    ConfigError,
    ExecutionRefused,
    NotFoundError,
    PayPalError,
    PayPalTransportError,
    ServiceUnavailable,
    ValidationError,
)
from lce.execution.paypal import PROVIDER_NAME, CapabilityReport
from lce.execution.plan import (
    ExecutablePlan,
    PlannedAction,
    Recipient,
    build_plan,
    idempotency_key,
)
from lce.execution.webhooks import Reentry, reconcile
from lce.logging import get_logger
from lce.paypal.client import PayPalClient
from lce.paypal.money import DemoScale, Money
from lce.snapshot.store import SnapshotStore

logger = get_logger(__name__)

RECIPIENT_TYPES = frozenset({"EMAIL", "PHONE", "PAYPAL_ID"})

NO_DATABASE = (
    "execution records need a database: set DATABASE_URL and run "
    "`alembic upgrade head`. The analysis does not need one."
)

#: PayPal's ``batch_status`` when no item status is available yet.
BATCH_STATUS = {
    "PENDING": "pending",
    "PROCESSING": "pending",
    "SUCCESS": "submitted",
    "DENIED": "failed",
    "CANCELED": "failed",
}

EMAIL_SUBJECT = "Keystone sandbox payout"
EMAIL_MESSAGE = (
    "Sandbox payout from a Keystone plan approved by a person. "
    "Demo scale; no real money moves."
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


@dataclass(frozen=True, slots=True)
class PreparedPayout:
    """Everything sent to PayPal for one action - kept so a resubmit is identical."""

    request_id: str
    body: dict[str, Any]


def payout_request(plan: ExecutablePlan, action: PlannedAction) -> PreparedPayout:
    """The Payouts request for one action. Deterministic in (plan, action)."""
    if action.recipient is None or action.mapping != "payout":
        raise ExecutionRefused("action is not a payout to a mapped recipient", key=action.key)
    key = idempotency_key(plan.plan_hash, action.key)
    body = {
        "sender_batch_header": {
            "sender_batch_id": key,
            "recipient_type": action.recipient.recipient_type,
            "email_subject": EMAIL_SUBJECT,
            "email_message": EMAIL_MESSAGE,
        },
        "items": [
            {
                "recipient_type": action.recipient.recipient_type,
                "amount": action.executed.to_wire(),
                "receiver": action.recipient.receiver,
                "sender_item_id": key[:63],
                "note": (
                    f"Keystone plan {plan.plan_hash[:12]}, action {action.key[:24]}, "
                    f"scenario {plan.scenario_id}. Sandbox at demo scale "
                    f"{plan.scale.rate:f} {plan.scale.currency}/INR."
                ),
            }
        ],
    }
    return PreparedPayout(request_id=key, body=body)


class ExecutionService:
    def __init__(
        self,
        session: Session | None,
        *,
        store: SnapshotStore,
        settings: PayPalSettings | None = None,
        client_factory: Callable[[PayPalSettings], PayPalClient] | None = None,
        capability_report: Callable[[], CapabilityReport] | None = None,
    ) -> None:
        self._session = session
        self.store = store
        self.settings = settings or get_paypal_settings()
        self._client_factory = client_factory or (lambda s: PayPalClient(s))
        self._capability_report = capability_report
        self._client: PayPalClient | None = None

    # -------------------------------------------------------------- database

    @property
    def session(self) -> Session:
        if self._session is None:
            raise ServiceUnavailable(NO_DATABASE)
        return self._session

    # ------------------------------------------------------------ identities

    def recipients(self) -> dict[str, Recipient]:
        """Mapped sandbox identities; empty, not an error, without a database.

        The plan is still the plan without recipients - it just cannot be
        approved - so reading it must not require a database.
        """
        if self._session is None:
            return {}
        try:
            rows = self._session.execute(select(PayPalIdentityRow)).scalars().all()
        except (OperationalError, ProgrammingError):
            self._session.rollback()
            return {}
        return {r.merchant_id: Recipient(r.recipient_type, r.receiver, r.label) for r in rows}

    def map_identity(
        self, merchant_id: str, *, recipient_type: str, receiver: str, label: str = ""
    ) -> PayPalIdentityRow:
        recipient_type = recipient_type.upper()
        if recipient_type not in RECIPIENT_TYPES:
            raise ValidationError(
                "recipient_type must be EMAIL, PHONE or PAYPAL_ID", got=recipient_type
            )
        receiver = receiver.strip()
        if not receiver or len(receiver) > 127:
            raise ValidationError("receiver must be 1-127 characters")
        if merchant_id not in {m.merchant_id for m in self.store.merchants()}:
            raise NotFoundError("no such merchant in the snapshot", merchant_id=merchant_id)
        row = self.session.execute(
            select(PayPalIdentityRow).where(PayPalIdentityRow.merchant_id == merchant_id)
        ).scalar_one_or_none()
        if row is None:
            row = PayPalIdentityRow(merchant_id=merchant_id, recipient_type=recipient_type,
                                    receiver=receiver, label=label)
            self.session.add(row)
        else:
            row.recipient_type, row.receiver, row.label = recipient_type, receiver, label
        self.session.flush()
        return row

    # ------------------------------------------------------------------- plan

    @property
    def scale(self) -> DemoScale:
        return DemoScale(rate=self.settings.demo_scale, currency=self.settings.demo_currency)

    def plan(self, scenario_id: str) -> ExecutablePlan:
        from lce.agent.plans import get as agent_plan
        from lce.execution.plan import build_plan_from_actions

        asked = agent_plan(scenario_id)
        if asked is not None:
            return build_plan_from_actions(
                scenario_id=scenario_id,
                snapshot_id=self.store.snapshot_id,
                provider=PROVIDER_NAME,
                scale=self.scale,
                recipients=self.recipients(),
                actions=[(a.merchant_id, a.action_type, a.amount_inr, a.cost_inr)
                         for a in asked.actions],
                replayed_cost_inr=asked.replayed_cost_inr,
            )
        scenario = self.store.scenario(scenario_id)
        plan = build_plan(
            scenario,
            snapshot_id=self.store.snapshot_id,
            provider=PROVIDER_NAME,
            scale=self.scale,
            recipients=self.recipients(),
        )
        if plan is None:
            raise NotFoundError(
                "this scenario has no selected plan to execute", scenario_id=scenario_id
            )
        return plan

    # ---------------------------------------------------------------- approve

    def approve(self, scenario_id: str, *, approver: str, plan_hash: str) -> ExecutionApprovalRow:
        approver = approver.strip()
        if not approver or len(approver) > 80:
            raise ValidationError("approver must be 1-80 characters")
        plan = self.plan(scenario_id)
        if plan_hash != plan.plan_hash:
            self._event(plan, "approval_refused", "refused",
                        {"reason": "plan changed since it was shown", "offered": plan_hash[:16]})
            self.session.commit()
            raise ExecutionRefused(
                "the plan has changed since it was shown; review it again before approving",
                expected=plan.plan_hash,
            )
        if not plan.complete:
            raise ExecutionRefused("this plan is incomplete and cannot be approved",
                                   reason=plan.incomplete_reason)
        gaps = sorted(a.merchant_id for a in plan.actions if a.mapping and a.recipient is None)
        if gaps:
            raise ExecutionRefused(
                "every paid action needs a PayPal sandbox identity before the plan "
                "can be approved",
                unmapped=gaps,
            )
        row = ExecutionApprovalRow(
            approval_id=_new_id("apr"),
            plan_hash=plan.plan_hash,
            snapshot_id=plan.snapshot_id,
            scenario_id=plan.scenario_id,
            provider=plan.provider,
            approver=approver,
            executed_total_minor=plan.executed_total.minor_units,
            currency=plan.scale.currency,
            demo_scale=f"{plan.scale.rate:f}",
            detail={"actions": [a.key for a in plan.actions]},
        )
        self.session.add(row)
        self._event(plan, "approval_recorded", None,
                    {"approval_id": row.approval_id, "approver": approver,
                     "executed_total": plan.executed_total.to_wire()})
        self.session.commit()
        return row

    # ---------------------------------------------------------------- execute

    def _approval(self, approval_id: str) -> ExecutionApprovalRow:
        row = self.session.execute(
            select(ExecutionApprovalRow).where(ExecutionApprovalRow.approval_id == approval_id)
        ).scalar_one_or_none()
        if row is None:
            raise ExecutionRefused("no such approval; nothing is executed without one",
                                   approval_id=approval_id)
        return row

    def _paypal(self) -> PayPalClient:
        if self._client is None:
            self.settings.require_credentials()
            self._client = self._client_factory(self.settings)
        return self._client

    def execute(self, scenario_id: str, *, approval_id: str) -> list[ExecutionRecordRow]:
        approval = self._approval(approval_id)
        plan = self.plan(scenario_id)
        if approval.scenario_id != scenario_id or approval.plan_hash != plan.plan_hash:
            self._event(plan, "execution_refused", "refused",
                        {"reason": "approval does not match the current plan",
                         "approval_id": approval_id})
            self.session.commit()
            raise ExecutionRefused(
                "this approval is for a different plan than the one now in force; "
                "approve the current plan before executing",
                approval_id=approval_id,
            )
        if not plan.complete:
            raise ExecutionRefused("this plan is incomplete", reason=plan.incomplete_reason)
        gaps = [a.merchant_id for a in plan.actions if a.mapping and a.recipient is None]
        if gaps:
            raise ExecutionRefused(
                "every paid action needs a PayPal sandbox identity before the plan can run",
                unmapped=sorted(gaps),
            )
        try:
            self.settings.require_credentials()
        except ConfigError as exc:
            raise ExecutionRefused(exc.message) from None

        report = self._capability_report() if self._capability_report else None
        records: list[ExecutionRecordRow] = []
        for action in plan.actions:
            records.append(self._execute_action(plan, approval, action, report))
        return records

    def _existing(self, key: str) -> ExecutionRecordRow | None:
        return self.session.execute(
            select(ExecutionRecordRow).where(ExecutionRecordRow.idempotency_key == key)
        ).scalar_one_or_none()

    def _execute_action(
        self,
        plan: ExecutablePlan,
        approval: ExecutionApprovalRow,
        action: PlannedAction,
        report: CapabilityReport | None,
    ) -> ExecutionRecordRow:
        key = idempotency_key(plan.plan_hash, action.key)
        existing = self._existing(key)
        if existing is not None:
            # The same approved action again: answer with what already happened.
            self._event(plan, "duplicate_request_ignored", existing.status,
                        {"idempotency_key": key}, record_id=existing.record_id)
            self.session.commit()
            return existing

        record = ExecutionRecordRow(
            record_id=_new_id("rec"),
            idempotency_key=key,
            approval_id=approval.approval_id,
            plan_hash=plan.plan_hash,
            scenario_id=plan.scenario_id,
            action_key=action.key,
            merchant_id=action.merchant_id,
            action_type=action.action_type,
            analytic_amount_inr=repr(action.analytic_amount_inr),
            executed_minor=action.executed.minor_units,
            currency=action.executed.currency,
            provider=PROVIDER_NAME,
            status="planned",
        )

        if action.mapping is None:
            record.reason = action.not_executable_reason
            return self._persist_new(plan, record, "recorded_as_plan")
        if report is not None and not report.available("payouts"):
            payouts = next((c for c in report.capabilities if c.name == "payouts"), None)
            record.reason = "PayPal Payouts is not available for this app" + (
                f": {payouts.reason}" if payouts else ""
            )
            return self._persist_new(plan, record, "recorded_as_plan")

        prepared = payout_request(plan, action)
        record.status = "unknown"
        record.reason = "submitted; awaiting PayPal's answer"
        persisted = self._persist_new(plan, record, "submission_started")
        if persisted is not record:
            # A concurrent request recorded (and is submitting) this action.
            return persisted
        self._submit(plan, record, prepared)
        return record

    def _persist_new(
        self, plan: ExecutablePlan, record: ExecutionRecordRow, kind: str
    ) -> ExecutionRecordRow:
        self.session.add(record)
        try:
            self.session.flush()
        except IntegrityError:
            # Another request recorded this action first; return that record.
            self.session.rollback()
            existing = self._existing(record.idempotency_key)
            assert existing is not None
            return existing
        self._event(plan, kind, record.status,
                    {"merchant_id": record.merchant_id, "executed":
                     Money.from_minor(record.executed_minor, record.currency).to_wire(),
                     "reason": record.reason}, record_id=record.record_id)
        self.session.commit()
        return record

    def _submit(
        self, plan: ExecutablePlan, record: ExecutionRecordRow, prepared: PreparedPayout
    ) -> None:
        try:
            answer = self._paypal().create_payout(prepared.body, request_id=prepared.request_id)
        except PayPalTransportError:
            record.status, record.reason = "unknown", (
                "PayPal did not answer; the outcome is unknown. Refresh to reconcile."
            )
            self._event(plan, "submission_outcome_unknown", "unknown", {},
                        record_id=record.record_id)
            self.session.commit()
            return
        except PayPalError as exc:
            record.status = "failed"
            record.reason = f"PayPal rejected the payout: {exc.context.get('name') or exc.message}"
            record.debug_id = _str_or_none(exc.context.get("debug_id"))
            self._event(plan, "submission_rejected", "failed",
                        {"http_status": exc.context.get("status"), "name": exc.context.get("name"),
                         "debug_id": record.debug_id}, record_id=record.record_id)
            self.session.commit()
            return

        header = answer.get("batch_header", {}) if isinstance(answer, dict) else {}
        record.provider_batch_id = _str_or_none(header.get("payout_batch_id"))
        batch_status = str(header.get("batch_status", "")).upper()
        record.provider_status = batch_status or None
        record.status = BATCH_STATUS.get(batch_status, "submitted")
        record.reason = None if record.status != "failed" else f"batch {batch_status.lower()}"
        self._event(plan, "submitted", record.status,
                    {"payout_batch_id": record.provider_batch_id,
                     "batch_status": batch_status}, record_id=record.record_id)
        self.session.commit()

    # ---------------------------------------------------------------- refresh

    def refresh(self, record_id: str) -> tuple[ExecutionRecordRow, Reentry | None]:
        """Explicit, human-initiated reconcile. Reads only; never resubmits.

        Returns the record and, if PayPal now reports a failure that should
        re-enter the analysis, the re-entry to schedule after the response.
        """
        record = self.session.execute(
            select(ExecutionRecordRow).where(ExecutionRecordRow.record_id == record_id)
        ).scalar_one_or_none()
        if record is None:
            raise NotFoundError("no such execution record", record_id=record_id)
        if record.status == "planned":
            return record, None
        return reconcile(self.session, self._paypal(), record)

    # ----------------------------------------------------------------- ledger

    def records(self, scenario_id: str) -> list[ExecutionRecordRow]:
        return list(
            self.session.execute(
                select(ExecutionRecordRow)
                .where(ExecutionRecordRow.scenario_id == scenario_id)
                .order_by(ExecutionRecordRow.id)
            ).scalars()
        )

    def ledger(self, scenario_id: str) -> list[ExecutionEventRow]:
        return list(
            self.session.execute(
                select(ExecutionEventRow)
                .where(ExecutionEventRow.scenario_id == scenario_id)
                .order_by(ExecutionEventRow.id)
            ).scalars()
        )

    def approvals(self, scenario_id: str) -> list[ExecutionApprovalRow]:
        return list(
            self.session.execute(
                select(ExecutionApprovalRow)
                .where(ExecutionApprovalRow.scenario_id == scenario_id)
                .order_by(ExecutionApprovalRow.id)
            ).scalars()
        )

    # ------------------------------------------------------------------ audit

    def _event(
        self,
        plan: ExecutablePlan,
        kind: str,
        status: str | None,
        detail: Mapping[str, Any],
        *,
        record_id: str | None = None,
    ) -> None:
        self.session.add(
            ExecutionEventRow(
                event_id=_new_id("evt"),
                scenario_id=plan.scenario_id,
                plan_hash=plan.plan_hash,
                record_id=record_id,
                kind=kind,
                status=status,
                detail={k: v for k, v in detail.items() if v is not None},
            )
        )
        logger.info("execution_event", kind=kind, status=status, scenario_id=plan.scenario_id,
                    plan=plan.plan_hash[:12], record_id=record_id)


def _str_or_none(value: object) -> str | None:
    return None if value in (None, "") else str(value)
