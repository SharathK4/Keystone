"""Execution routes: PayPal status, the plan, approval, execution and the ledger.

What does not need what:

* ``GET /execution/paypal`` needs neither a database nor credentials; it says
  what is missing instead of failing.
* ``GET /execution/plans/{id}`` needs the snapshot; without a database it still
  answers, with no recipients mapped.
* Approval, execution, refresh and the ledger write and read records, so they
  need a database. Without one they return 503 with the reason, and the rest of
  the API - all of the analysis - keeps working.

None of these routes is ever called by a page *read* of the analysis, and none
of them optimises anything: the plan is read from the frozen snapshot.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from lce.config import get_paypal_settings
from lce.data.orm import ExecutionApprovalRow, ExecutionEventRow, ExecutionRecordRow
from lce.errors import ServiceUnavailable
from lce.execution.models import (
    ApprovalRequest,
    ApprovalView,
    CapabilityView,
    DemoScaleView,
    ExecutablePlanView,
    ExecuteRequest,
    ExecutionRecordView,
    LedgerEventView,
    LedgerView,
    MoneyView,
    PayPalStatusView,
    PlannedActionView,
    RecipientView,
)
from lce.execution.paypal import (
    PayPalSandboxProvider,
    cached_report,
    refused_report,
)
from lce.execution.plan import ExecutablePlan
from lce.execution.service import NO_DATABASE, ExecutionService
from lce.paypal.money import DemoScale, Money
from lce.snapshot.store import SnapshotStore, require_store

router = APIRouter(prefix="/execution", tags=["execution"])

Store = Annotated[SnapshotStore, Depends(require_store)]

def _service(store: Store) -> Iterator[ExecutionService]:
    """One service for every execution route, with or without a database."""
    from lce.data.database import get_session_factory

    try:
        session: Session | None = get_session_factory()()
    except Exception:  # no driver, bad URL: the plan still serves; writes say why
        session = None
    try:
        yield ExecutionService(
            session,
            store=store,
            capability_report=lambda: cached_report(PayPalSandboxProvider()),
        )
    finally:
        if session is not None:
            session.close()


@contextmanager
def _db() -> Iterator[None]:
    """A database that is configured but unreachable is a 503 with the reason."""
    try:
        yield
    except (OperationalError, ProgrammingError) as exc:
        raise ServiceUnavailable(NO_DATABASE, error=type(exc.orig).__name__) from None


Service = Annotated[ExecutionService, Depends(_service)]


# ----------------------------------------------------------------- converters


def _money(m: Money) -> MoneyView:
    return MoneyView(**m.to_wire())


def _demo(scale: DemoScale) -> DemoScaleView:
    return DemoScaleView(**scale.describe())


def _demo_now() -> DemoScaleView:
    s = get_paypal_settings()
    return _demo(DemoScale(rate=s.demo_scale, currency=s.demo_currency))


def plan_view(plan: ExecutablePlan) -> ExecutablePlanView:
    return ExecutablePlanView(
        scenario_id=plan.scenario_id,
        snapshot_id=plan.snapshot_id,
        provider=plan.provider,
        plan_hash=plan.plan_hash,
        complete=plan.complete,
        incomplete_reason=plan.incomplete_reason,
        executes_whole_plan=all(a.mapping is not None for a in plan.actions),
        replayed_cost_inr=repr(plan.replayed_cost_inr),
        executed_total=_money(plan.executed_total),
        actions=[
            PlannedActionView(
                key=a.key,
                source=a.source,  # type: ignore[arg-type]
                merchant_id=a.merchant_id,
                action_type=a.action_type,
                analytic_amount_inr=repr(a.analytic_amount_inr),
                executed=_money(a.executed),
                mapping=a.mapping,
                recipient=(
                    None
                    if a.recipient is None
                    else RecipientView(
                        recipient_type=a.recipient.recipient_type,
                        receiver=a.recipient.receiver,
                        label=a.recipient.label,
                    )
                ),
                executable=a.executable,
                reason=a.not_executable_reason,
            )
            for a in plan.actions
        ],
        demo=_demo(plan.scale),
    )


def approval_view(row: ExecutionApprovalRow) -> ApprovalView:
    return ApprovalView(
        approval_id=row.approval_id,
        scenario_id=row.scenario_id,
        snapshot_id=row.snapshot_id,
        plan_hash=row.plan_hash,
        provider=row.provider,
        approver=row.approver,
        approved_at=row.approved_at,
        executed_total=_money(Money.from_minor(row.executed_total_minor, row.currency)),
        demo_scale=row.demo_scale,
    )


def record_view(row: ExecutionRecordRow) -> ExecutionRecordView:
    return ExecutionRecordView(
        record_id=row.record_id,
        approval_id=row.approval_id,
        plan_hash=row.plan_hash,
        scenario_id=row.scenario_id,
        action_key=row.action_key,
        merchant_id=row.merchant_id,
        action_type=row.action_type,
        analytic_amount_inr=row.analytic_amount_inr,
        executed=_money(Money.from_minor(row.executed_minor, row.currency)),
        provider=row.provider,
        status=row.status,  # type: ignore[arg-type]
        reason=row.reason,
        paypal_payout_batch_id=row.provider_batch_id,
        paypal_payout_item_id=row.provider_item_id,
        paypal_status=row.provider_status,
        paypal_debug_id=row.debug_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def event_view(row: ExecutionEventRow) -> LedgerEventView:
    return LedgerEventView(
        event_id=row.event_id,
        recorded_at=row.created_at,
        kind=row.kind,
        status=row.status,
        record_id=row.record_id,
        detail=dict(row.detail or {}),
    )


# --------------------------------------------------------------------- routes


@router.get(
    "/paypal",
    response_model=PayPalStatusView,
    summary="What the PayPal sandbox can do for this app, with reasons",
)
def paypal_status(probe: bool = True, refresh: bool = False) -> PayPalStatusView:
    settings = get_paypal_settings()
    if settings.refusal() is not None:
        report = refused_report(settings)
    else:
        report = cached_report(PayPalSandboxProvider(settings), probe=probe, refresh=refresh)
    return PayPalStatusView(
        provider="paypal_sandbox",
        mode=report.mode,
        configured=report.configured,
        refused=report.refused,
        probed=report.probed,
        probed_at=report.probed_at,
        api_host=settings.api_host,
        capabilities=[CapabilityView(**c.to_dict()) for c in report.capabilities],
        executable_intervention_types=report.executable_intervention_types,
        demo=_demo(DemoScale(rate=settings.demo_scale, currency=settings.demo_currency)),
    )


@router.get(
    "/plans/{scenario_id}",
    response_model=ExecutablePlanView,
    summary="The selected plan, priced for the sandbox, with the hash to approve",
)
def get_plan(scenario_id: str, service: Service) -> ExecutablePlanView:
    return plan_view(service.plan(scenario_id))


@router.post(
    "/plans/{scenario_id}/approvals",
    response_model=ApprovalView,
    status_code=201,
    summary="Approve one exact plan (by hash). Nothing executes without this.",
)
def approve_plan(scenario_id: str, body: ApprovalRequest, service: Service) -> ApprovalView:
    with _db():
        return approval_view(
            service.approve(scenario_id, approver=body.approver, plan_hash=body.plan_hash)
        )


@router.post(
    "/plans/{scenario_id}/executions",
    response_model=list[ExecutionRecordView],
    summary="Execute an approved plan in the PayPal sandbox (idempotent)",
)
def execute_plan(
    scenario_id: str, body: ExecuteRequest, service: Service
) -> list[ExecutionRecordView]:
    with _db():
        return [
            record_view(r) for r in service.execute(scenario_id, approval_id=body.approval_id)
        ]


@router.post(
    "/records/{record_id}/refresh",
    response_model=ExecutionRecordView,
    summary="Reconcile one record with PayPal (explicit, never automatic)",
)
def refresh_record(record_id: str, service: Service) -> ExecutionRecordView:
    with _db():
        return record_view(service.refresh(record_id))


@router.get(
    "/ledger",
    response_model=LedgerView,
    summary="Approvals, records and the append-only audit trail for one scenario",
)
def ledger(service: Service, scenario_id: str = Query(min_length=1)) -> LedgerView:
    with _db():
        return _ledger(service, scenario_id)


def _ledger(service: ExecutionService, scenario_id: str) -> LedgerView:
    return LedgerView(
        scenario_id=scenario_id,
        approvals=[approval_view(a) for a in service.approvals(scenario_id)],
        records=[record_view(r) for r in service.records(scenario_id)],
        events=[event_view(e) for e in service.ledger(scenario_id)],
        demo=_demo_now(),
    )
