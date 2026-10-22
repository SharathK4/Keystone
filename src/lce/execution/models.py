"""Response models for the execution routes.

Same rules as the analytical contract (:mod:`lce.snapshot.models`): immutable,
``extra="forbid"``, a value that is unknown is ``null`` rather than a guess.

Two kinds of amount appear here and they never share a field:

* ``analytic_amount_inr`` - the model's amount in rupees, as the exact text of
  the number the snapshot holds;
* ``executed`` - what is (or would be) sent to the PayPal sandbox, as PayPal's
  own ``{currency, value}`` object with a string value.

Every response carries ``demo`` so a sandbox figure is never shown without the
scale that produced it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())


class MoneyView(_Contract):
    currency: str
    value: str


class DemoScaleView(_Contract):
    currency: str
    scale: str
    unit: str
    rounding: str
    note: str = (
        "A declared display scale from analytic rupees to the sandbox currency, "
        "not an exchange rate. PayPal cannot pay out INR."
    )


class CapabilityView(_Contract):
    name: str
    available: bool | None
    reason: str
    endpoint: str | None
    http_status: int | None
    verified_in_sandbox: bool


class PayPalStatusView(_Contract):
    """What the PayPal sandbox can do for this app, each answer with its reason."""

    provider: Literal["paypal_sandbox"]
    mode: str
    configured: bool
    refused: str | None
    probed: bool
    probed_at: datetime | None
    api_host: str
    capabilities: list[CapabilityView]
    executable_intervention_types: list[str]
    demo: DemoScaleView


class RecipientView(_Contract):
    recipient_type: str
    receiver: str
    label: str


class PlannedActionView(_Contract):
    key: str
    source: Literal["recommended", "offer"]
    merchant_id: str
    action_type: str
    analytic_amount_inr: str
    executed: MoneyView
    mapping: str | None
    recipient: RecipientView | None
    executable: bool
    reason: str | None


class ExecutablePlanView(_Contract):
    """The exact plan an approver sees. ``plan_hash`` is what they approve."""

    scenario_id: str
    snapshot_id: str
    provider: str
    plan_hash: str
    complete: bool
    incomplete_reason: str | None
    executes_whole_plan: bool
    replayed_cost_inr: str
    executed_total: MoneyView
    actions: list[PlannedActionView]
    demo: DemoScaleView


class ApprovalRequest(_Contract):
    approver: str = Field(min_length=1, max_length=80)
    plan_hash: str = Field(min_length=64, max_length=64)


class ApprovalView(_Contract):
    approval_id: str
    scenario_id: str
    snapshot_id: str
    plan_hash: str
    provider: str
    approver: str
    approved_at: datetime
    executed_total: MoneyView
    demo_scale: str


class ExecuteRequest(_Contract):
    approval_id: str = Field(min_length=1, max_length=40)


class ExecutionRecordView(_Contract):
    record_id: str
    approval_id: str
    plan_hash: str
    scenario_id: str
    action_key: str
    merchant_id: str
    action_type: str
    analytic_amount_inr: str
    executed: MoneyView
    provider: str
    status: Literal["planned", "submitted", "pending", "held", "succeeded", "failed", "unknown"]
    reason: str | None
    paypal_payout_batch_id: str | None
    paypal_payout_item_id: str | None
    paypal_status: str | None
    paypal_debug_id: str | None
    created_at: datetime
    updated_at: datetime


class LedgerEventView(_Contract):
    event_id: str
    recorded_at: datetime
    kind: str
    status: str | None
    record_id: str | None
    detail: dict[str, object]


class LedgerView(_Contract):
    """Approvals, records and the append-only audit trail for one scenario."""

    scenario_id: str
    approvals: list[ApprovalView]
    records: list[ExecutionRecordView]
    events: list[LedgerEventView]
    demo: DemoScaleView


class ReentryPlanView(_Contract):
    """The plan the bounded analysis proposes for the new shock. Never executed
    without its own approval."""

    action_type: str
    merchant_id: str
    analytic_amount_inr: str


class ReentryView(_Contract):
    """A failed payout, re-entered as a shock and analysed within fixed bounds.

    Provenance kind is *computed now*: a bounded analysis run when PayPal
    reported the failure, on the snapshot named in ``snapshot_id``.
    """

    analysis_id: str
    record_id: str
    scenario_id: str
    merchant_id: str
    trigger: str
    status: Literal["analysed", "skipped"]
    reason: str | None
    missing_inr: str | None
    snapshot_id: str | None
    computed_in_ms: float | None
    adhoc_scenario_id: str | None
    merchants_reached: int | None
    disrupted_value_inr: str | None
    new_plan: list[ReentryPlanView]
    created_at: datetime


class DemoStatusView(_Contract):
    """How the hosted demo treats execution, so a page can say so up front."""

    mode: Literal["off", "recorded"]
    token_required: bool
    token_header: str
    executes_only: str | None
    rate_limit: int | None
    rate_window_s: int | None
    note: str | None


class PayPalWebhookAck(_Contract):
    """What happened to one delivery. Returned with 200 for every verified event."""

    status: Literal["accepted", "duplicate", "ignored"]
    event_id: str
    event_type: str
    record_id: str | None
    record_status: str | None
    applied: bool
    reason: str | None
    reentry_scheduled: bool
