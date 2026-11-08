"""The plan a human approves and PayPal executes, read from the frozen snapshot.

Nothing here optimises. The selected plan was chosen offline and replayed; this
module only reads it back out of the snapshot, prices it in the sandbox currency
by the declared demo scale, and fingerprints the result.

Reading it back is less obvious than it sounds. A snapshot scenario carries
``recommended_intervention`` (the first selected candidate) and ``offer`` (a
contract written for the plan's first action), and those can be two different
merchants: the snapshot does not list the plan as one object. So the plan is
assembled from both, and then **checked**: the actions' committed capital must
add up to ``counterfactual.cost``, the replayed plan's total. If it does not,
the plan is marked incomplete and cannot be approved - executing part of a plan
whose measured benefit belongs to the whole would be executing something
nobody measured.

The fingerprint (:func:`plan_hash`) covers everything an approver is shown:
snapshot and scenario, provider, demo scale and rounding, and for every action
the merchant, type, analytic amount, executed amount and recipient. Change any
of them and an existing approval stops matching.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from lce.domain.enums import InterventionType
from lce.paypal.money import DemoScale, Money
from lce.snapshot.models import ScenarioSnapshot

#: Action types with an implemented PayPal mapping. Everything else is recorded
#: as a plan with a stated reason. Invoice-based mappings are added only after
#: the sandbox spike that proves the behaviour they rely on.
PAYPAL_MAPPINGS: dict[str, str] = {
    str(InterventionType.LIQUIDITY_INJECTION): "payout",
}

#: Relative tolerance on "the actions add up to the replayed plan's cost".
COMPLETENESS_RTOL = 1e-9

PLAN_HASH_VERSION = "keystone-plan-v1"


@dataclass(frozen=True, slots=True)
class Recipient:
    """Where a merchant's payout would go in the sandbox."""

    recipient_type: str  # EMAIL | PHONE | PAYPAL_ID
    receiver: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class PlannedAction:
    key: str
    source: str  # "recommended" | "offer" | "plan" (an Ask Keystone plan)
    merchant_id: str
    action_type: str
    analytic_amount_inr: float
    analytic_cost_inr: float | None
    executed: Money
    mapping: str | None
    recipient: Recipient | None

    @property
    def executable(self) -> bool:
        return self.mapping is not None and self.recipient is not None

    @property
    def not_executable_reason(self) -> str | None:
        if self.mapping is None:
            return f"no PayPal mapping for {self.action_type}; recorded as a plan"
        if self.recipient is None:
            return f"no PayPal sandbox identity mapped for {self.merchant_id}"
        return None


@dataclass(frozen=True, slots=True)
class ExecutablePlan:
    snapshot_id: str
    scenario_id: str
    provider: str
    scale: DemoScale
    actions: tuple[PlannedAction, ...]
    replayed_cost_inr: float
    complete: bool
    incomplete_reason: str | None
    plan_hash: str = field(default="")

    @property
    def executed_total(self) -> Money:
        total = Money.of(0, self.scale.currency)
        for action in self.actions:
            if action.executable:
                total = total + action.executed
        return total


def build_plan(
    scenario: ScenarioSnapshot,
    *,
    snapshot_id: str,
    provider: str,
    scale: DemoScale,
    recipients: Mapping[str, Recipient],
) -> ExecutablePlan | None:
    """The scenario's selected plan, priced for execution. ``None`` if there is none."""
    recommended = scenario.recommended_intervention
    if recommended is None:
        return None

    raw: list[tuple[str, str, str, str, float, float | None]] = [
        (
            recommended.intervention_id,
            "recommended",
            recommended.merchant_id,
            recommended.type,
            recommended.amount,
            recommended.cost,
        )
    ]
    offer = scenario.offer
    if offer is not None and offer.merchant_id != recommended.merchant_id:
        # The contract's action carries no cost field. Only an injection's cost
        # is its amount by definition; any other type leaves the sum unknown.
        cost = (
            offer.proposed_amount
            if offer.intervention_type == str(InterventionType.LIQUIDITY_INJECTION)
            else None
        )
        raw.append(
            (
                offer.offer_id,
                "offer",
                offer.merchant_id,
                offer.intervention_type,
                offer.proposed_amount,
                cost,
            )
        )

    actions = tuple(
        PlannedAction(
            key=key,
            source=source,
            merchant_id=merchant_id,
            action_type=action_type,
            analytic_amount_inr=amount,
            analytic_cost_inr=cost,
            executed=scale.apply(amount),
            mapping=PAYPAL_MAPPINGS.get(action_type),
            recipient=recipients.get(merchant_id),
        )
        for key, source, merchant_id, action_type, amount, cost in raw
    )

    replayed = scenario.counterfactual.cost
    costs = [a.analytic_cost_inr for a in actions]
    if any(c is None for c in costs):
        complete, reason = False, "an action's committed capital is not recorded in the snapshot"
    else:
        total = sum(c for c in costs if c is not None)
        if abs(total - replayed) > COMPLETENESS_RTOL * max(1.0, abs(replayed)):
            complete = False
            reason = (
                "the actions in the snapshot do not add up to the replayed plan's "
                "committed capital, so the plan is not fully represented"
            )
        else:
            complete, reason = True, None

    plan = ExecutablePlan(
        snapshot_id=snapshot_id,
        scenario_id=scenario.scenario_id,
        provider=provider,
        scale=scale,
        actions=actions,
        replayed_cost_inr=replayed,
        complete=complete,
        incomplete_reason=reason,
    )
    return _with_hash(plan)


def build_plan_from_actions(
    *,
    scenario_id: str,
    snapshot_id: str,
    provider: str,
    scale: DemoScale,
    recipients: Mapping[str, Recipient],
    actions: list[tuple[str, str, float, float]],
    replayed_cost_inr: float,
) -> ExecutablePlan:
    """A plan given as the search's whole list of actions (Ask Keystone).

    ``actions`` are ``(merchant_id, action_type, amount_inr, cost_inr)`` in the
    order the search chose them. Keys are positional and deterministic - never
    the analysis's random intervention ids - so asking the same question again
    yields the same fingerprint. The completeness check is the same as
    :func:`build_plan`'s.
    """
    planned = tuple(
        PlannedAction(
            key=f"{scenario_id}:{i}:{merchant_id}",
            source="plan",
            merchant_id=merchant_id,
            action_type=action_type,
            analytic_amount_inr=amount,
            analytic_cost_inr=cost,
            executed=scale.apply(amount),
            mapping=PAYPAL_MAPPINGS.get(action_type),
            recipient=recipients.get(merchant_id),
        )
        for i, (merchant_id, action_type, amount, cost) in enumerate(actions, start=1)
    )
    total = sum(a.analytic_cost_inr or 0.0 for a in planned)
    complete = bool(planned) and abs(total - replayed_cost_inr) <= COMPLETENESS_RTOL * max(
        1.0, abs(replayed_cost_inr))
    plan = ExecutablePlan(
        snapshot_id=snapshot_id,
        scenario_id=scenario_id,
        provider=provider,
        scale=scale,
        actions=planned,
        replayed_cost_inr=replayed_cost_inr,
        complete=complete,
        incomplete_reason=None if complete else (
            "the actions do not add up to the replayed plan's committed capital"),
    )
    return _with_hash(plan)


def _canonical(plan: ExecutablePlan) -> dict[str, object]:
    return {
        "version": PLAN_HASH_VERSION,
        "snapshot_id": plan.snapshot_id,
        "scenario_id": plan.scenario_id,
        "provider": plan.provider,
        "demo": plan.scale.describe(),
        "actions": [
            {
                "key": a.key,
                "merchant_id": a.merchant_id,
                "type": a.action_type,
                "analytic_amount_inr": repr(a.analytic_amount_inr),
                "executed": a.executed.to_wire(),
                "mapping": a.mapping,
                "recipient": (
                    None
                    if a.recipient is None
                    else {"type": a.recipient.recipient_type, "receiver": a.recipient.receiver}
                ),
            }
            for a in plan.actions
        ],
    }


def plan_hash(plan: ExecutablePlan) -> str:
    blob = json.dumps(_canonical(plan), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _with_hash(plan: ExecutablePlan) -> ExecutablePlan:
    from dataclasses import replace

    return replace(plan, plan_hash=plan_hash(plan))


def idempotency_key(plan_hash_value: str, action_key: str) -> str:
    """One key per (approved plan, action): the local unique key and PayPal's ids."""
    digest = hashlib.sha256(f"{plan_hash_value}|{action_key}".encode()).hexdigest()
    return f"ks-{digest[:40]}"


def scale_from(currency: str, rate: Decimal) -> DemoScale:
    return DemoScale(rate=rate, currency=currency)
