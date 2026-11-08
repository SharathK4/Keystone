"""Plans found by Ask Keystone, kept so a person can approve and pay them.

An answer's plan is identified by the analysis's own id (``adhoc-…``, a hash of
the snapshot, merchants, size and timing), so the same question always names
the same plan. The approval desk and the payment routes look the plan up here
and price it with the same rules as the reference plan: same fingerprint, same
pay-at-most-once keys, same webhook handling.

Kept in process memory, bounded. A plan asked about before a restart is gone
after it; the page says to ask again rather than guessing.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lce.snapshot.models import ScenarioSnapshot

MAX_PLANS = 128


@dataclass(frozen=True, slots=True)
class PlanAction:
    merchant_id: str
    action_type: str
    amount_inr: float
    cost_inr: float
    lands_at_hours: float


@dataclass(frozen=True, slots=True)
class AgentPlan:
    """One answered question: the request, the analysis and its whole plan."""

    plan_ref: str
    merchant_ids: tuple[str, ...]
    magnitude_multiple: float
    onset_hours: float
    analysis: ScenarioSnapshot
    actions: tuple[PlanAction, ...]
    computed_in_ms: float
    #: The search's own intervention objects, for replaying the plan late.
    interventions: tuple[Any, ...] = ()
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def replayed_cost_inr(self) -> float:
        return float(self.analysis.counterfactual.cost)


_plans: OrderedDict[str, AgentPlan] = OrderedDict()
_lock = threading.Lock()


def register(plan: AgentPlan) -> AgentPlan:
    with _lock:
        _plans[plan.plan_ref] = plan
        _plans.move_to_end(plan.plan_ref)
        while len(_plans) > MAX_PLANS:
            _plans.popitem(last=False)
    return plan


def get(plan_ref: str) -> AgentPlan | None:
    with _lock:
        return _plans.get(plan_ref)


def is_agent_plan(plan_ref: str | None) -> bool:
    return bool(plan_ref) and get(str(plan_ref)) is not None


def clear() -> None:
    with _lock:
        _plans.clear()


def actions_from(chosen: list[Any]) -> tuple[PlanAction, ...]:
    """The search's chosen interventions, in the order it chose them."""
    return tuple(
        PlanAction(merchant_id=str(u.merchant_id), action_type=str(u.type),
                   amount_inr=float(u.amount), cost_inr=float(u.cost),
                   lands_at_hours=float(u.t))
        for u in chosen
    )
