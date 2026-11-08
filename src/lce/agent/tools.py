"""The four things Ask Keystone can do. Each one is real computation.

* ``find_merchants`` - turn "our biggest wholesaler" into merchant ids, from the
  network itself (sector, size, what each owes this week, importance rank).
* ``simulate_missed_payment`` - replay the coming week with a payment missing,
  on the model of who pays whom learned from transaction history; report who
  runs short and when, and the cheapest rescue the search finds. Seconds of CPU.
* ``check_delay`` - replay the same week with the rescue money landing late.
* ``prepare_paypal_payout`` - price the rescue as PayPal sandbox payouts for a
  person to approve. Nothing is paid here.

Every figure a tool hands the model is pre-formatted ("$1.62M", "42%"), so the
model can quote it exactly and the grounding check can find it again.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lce.agent import plans
from lce.config import get_display_settings, get_paypal_settings
from lce.errors import LCEError, ValidationError
from lce.paypal.money import DemoScale
from lce.snapshot.store import MAX_ON_DEMAND_ORIGINS, SnapshotStore

MIN_SIZE, MAX_SIZE = 0.5, 4.0
MAX_DELAY_HOURS = 96.0

#: Plain words for the action types the search can choose.
ACTION_WORDS = {
    "liquidity_injection": "fund",
    "receivable_acceleration": "pay early",
    "supplier_term_extension": "give more time to pay",
    "repayment_restructure": "restructure repayments for",
    "credit_line_increase": "raise the credit line of",
}


def usd(inr: float | None) -> str:
    """An analytic rupee amount in dollars at the declared display rate."""
    if inr is None:
        return "-"
    value = inr * float(get_display_settings().usd_per_inr)
    a = abs(value)
    sign = "-" if value < 0 else ""
    for cut, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= cut:
            n = a / cut
            text = f"{n:.0f}" if n >= 100 else f"{n:.1f}" if n >= 10 else f"{n:.2f}"
            return f"{sign}${text.rstrip('0').rstrip('.') if '.' in text else text}{unit}"
    return f"{sign}${a:.0f}"


def pct(value: float) -> str:
    return f"{value:.0f}%" if value >= 10 else f"{value:.1f}%"


def hours_text(hours: float) -> str:
    h = round(hours)
    return f"{h} hours" if h < 48 else f"about {round(hours / 24)} days"


@dataclass
class ToolResult:
    """What a tool returns: data for the model, a line for the page."""

    data: dict[str, Any]
    summary: str
    plan: plans.AgentPlan | None = None
    payment: dict[str, Any] | None = None
    ms: float = 0.0
    error: bool = False


@dataclass
class Toolbox:
    store: SnapshotStore
    session_factory: Callable[[], Any] | None = None
    last_plan: plans.AgentPlan | None = field(default=None)
    #: The figures the last simulation handed the model, for the template answer.
    last_summary: dict[str, Any] | None = field(default=None)

    # ------------------------------------------------------------ definitions

    def definitions(self) -> list[dict[str, Any]]:
        merchants = self.store.merchants()
        sectors = sorted({m.sector for m in merchants})
        tiers = sorted({m.tier for m in merchants})

        def schema(props: dict[str, Any]) -> dict[str, Any]:
            return {"type": "object", "properties": props, "required": list(props),
                    "additionalProperties": False}

        return [
            {
                "name": "find_merchants",
                "description": (
                    "List merchants so you can turn a description ('our biggest wholesaler', "
                    "'a small manufacturer', 'the most important merchant') into merchant ids. "
                    "Returns id, sector, size tier, what it owes this week, its cash cushion, "
                    "and its importance rank (1 = the merchant whose failure would hurt the "
                    "network most)."
                ),
                "strict": True,
                "input_schema": schema({
                    "sector": {"type": "string", "enum": ["any", *sectors]},
                    "tier": {"type": "string", "enum": ["any", *tiers]},
                    "order_by": {"type": "string",
                                 "enum": ["importance", "size", "thinnest_cushion"]},
                    "limit": {"type": "integer", "description": "1 to 8"},
                }),
            },
            {
                "name": "simulate_missed_payment",
                "description": (
                    "Replay the coming week with a payment missing, on Keystone's model of who "
                    "pays whom. Returns who else runs short and when, how much payment value is "
                    "delayed, and the cheapest rescue the search found: which merchants to "
                    "fund, how much, and how much of the damage it prevents. Real computation; "
                    "takes a few seconds. Call it once per scenario."
                ),
                "strict": True,
                "input_schema": schema({
                    "merchant_ids": {"type": "array", "items": {"type": "string"},
                                     "description": "The merchant(s) whose payment is missed, "
                                                    f"1 to {MAX_ON_DEMAND_ORIGINS}."},
                    "size_multiple": {"type": "number", "description": (
                        "How big the missed payment is, as a multiple of the merchant's own "
                        f"cash cushion, {MIN_SIZE} to {MAX_SIZE}. Use 2 unless the user says "
                        "otherwise.")},
                    "starts_at_hour": {"type": "number", "description": (
                        "When in the coming week (hours from now, 0 to 167) the payment is "
                        "missed. Use 0 unless the user says otherwise.")},
                }),
            },
            {
                "name": "check_delay",
                "description": (
                    "How much of the rescue still works if the money arrives late: replays the "
                    "same week with every payment of the plan landing delay_hours later. Use "
                    "when the user asks about timing, speed or waiting."
                ),
                "strict": True,
                "input_schema": schema({
                    "plan_ref": {"type": "string",
                                 "description": "The plan_ref simulate_missed_payment returned."},
                    "delay_hours": {"type": "number", "description": "0 to 96."},
                }),
            },
            {
                "name": "prepare_paypal_payout",
                "description": (
                    "Turn the rescue plan into PayPal sandbox payouts for a person to approve "
                    "on screen. Returns each payout (merchant, PayPal account, sandbox amount) "
                    "and whether it can be paid. You cannot send money."
                ),
                "strict": True,
                "input_schema": schema({
                    "plan_ref": {"type": "string",
                                 "description": "The plan_ref simulate_missed_payment returned."},
                }),
            },
        ]

    # --------------------------------------------------------------- dispatch

    def run(self, name: str, args: dict[str, Any]) -> ToolResult:
        started = time.perf_counter()
        handlers: dict[str, Callable[..., ToolResult]] = {
                "find_merchants": self.find_merchants,
                "simulate_missed_payment": self.simulate_missed_payment,
                "check_delay": self.check_delay,
                "prepare_paypal_payout": self.prepare_paypal_payout,
        }
        handler = handlers.get(name)
        if handler is None:
            return ToolResult({"error": f"unknown tool {name}"}, f"unknown tool {name}",
                              error=True)
        try:
            result = handler(**args)
        except (LCEError, TypeError, ValueError) as exc:
            message = exc.message if isinstance(exc, LCEError) else str(exc)
            result = ToolResult({"error": message}, message, error=True)
        result.ms = round((time.perf_counter() - started) * 1000.0, 1)
        return result

    # ------------------------------------------------------------------ tools

    def find_merchants(self, sector: str = "any", tier: str = "any",
                       order_by: str = "importance", limit: int = 5) -> ToolResult:
        limit = max(1, min(int(limit), 8))
        rank = {e.merchant_id: e.rank for e in self.store.systemic().entries}
        rows = [m for m in self.store.merchants()
                if sector in ("any", m.sector) and tier in ("any", m.tier)]
        if order_by == "size":
            rows.sort(key=lambda m: -m.throughput)
        elif order_by == "thinnest_cushion":
            rows.sort(key=lambda m: (m.cover_ratio if m.cover_ratio is not None else 1e9))
        else:
            rows.sort(key=lambda m: rank.get(m.merchant_id, 10_000))
        out = [{
            "merchant_id": m.merchant_id, "sector": m.sector, "tier": m.tier,
            "owes_this_week": usd(m.payables_in_horizon),
            "cash_cushion": usd(m.liquidity_buffer),
            "importance_rank": rank.get(m.merchant_id),
        } for m in rows[:limit]]
        if not out:
            return ToolResult({"merchants": [], "note": "no merchant matches"},
                              "no merchant matches that description")
        first = out[0]
        rank_text = (f", importance rank {first['importance_rank']}"
                     if first["importance_rank"] else "")
        return ToolResult({"merchants": out},
                          f"{first['merchant_id']}: {first['tier']} {first['sector']}{rank_text}")

    def simulate_missed_payment(self, merchant_ids: list[str], size_multiple: float = 2.0,
                                starts_at_hour: float = 0.0) -> ToolResult:
        ids = sorted({str(m).strip().lower() for m in merchant_ids if str(m).strip()})
        if not 1 <= len(ids) <= MAX_ON_DEMAND_ORIGINS:
            raise ValidationError(f"name 1 to {MAX_ON_DEMAND_ORIGINS} merchants")
        size = float(size_multiple)
        if not MIN_SIZE <= size <= MAX_SIZE:
            raise ValidationError(f"size_multiple must be {MIN_SIZE} to {MAX_SIZE}")
        started = time.perf_counter()
        snapshot, chosen = self.store.analyze_with_plan(
            merchant_ids=ids, magnitude_multiple=size, onset_hours=float(starts_at_hour))
        ms = round((time.perf_counter() - started) * 1000.0, 1)
        plan = plans.register(plans.AgentPlan(
            plan_ref=snapshot.scenario_id, merchant_ids=tuple(ids), magnitude_multiple=size,
            onset_hours=float(starts_at_hour), analysis=snapshot,
            actions=plans.actions_from(chosen), computed_in_ms=ms,
            interventions=tuple(chosen)))
        self.last_plan = plan

        origins = set(snapshot.shock.origin_merchants)
        others = sorted(
            (a for a in snapshot.affected_merchants if a.merchant_id not in origins),
            key=lambda a: a.time_to_constraint_hours or 1e9)
        cf = snapshot.counterfactual
        rescue = [{
            "merchant_id": a.merchant_id,
            "action": ACTION_WORDS.get(a.action_type, a.action_type.replace("_", " ")),
            "amount": usd(a.amount_inr),
        } for a in plan.actions]
        data = {
            "plan_ref": plan.plan_ref,
            "missed_payment": usd(snapshot.shock.magnitude),
            "others_who_run_short": [{
                "merchant_id": a.merchant_id,
                "runs_short_after": hours_text(a.time_to_constraint_hours)
                if a.time_to_constraint_hours is not None else "later in the week",
                "steps_from_the_failure": a.cascade_depth,
            } for a in others],
            "payments_delayed": usd(snapshot.projected_impact.disrupted_value),
            "rescue": rescue,
            "rescue_cost": usd(cf.cost),
            "damage_prevented": pct(cf.disruption_reduction_pct),
            "payments_kept_on_time": usd(cf.commerce_preserved),
            "rescues_compared": len(snapshot.alternatives) + len(plan.actions),
            "computed_in_seconds": f"{ms / 1000:.1f}",
        }
        self.last_summary = data
        who = ", ".join(r["merchant_id"] for r in rescue) or "nobody"
        prevents = pct(cf.disruption_reduction_pct)
        who_else = {0: "no other merchant runs", 1: "1 other merchant runs"}.get(
            len(others), f"{len(others)} other merchants run")
        compared = len(snapshot.alternatives) + len(plan.actions)
        summary = (f"compared {compared} possible rescues · {who_else} short · "
                   f"best: fund {who} ({usd(cf.cost)}) · prevents {prevents}")
        return ToolResult(data, summary, plan=plan)

    def check_delay(self, plan_ref: str, delay_hours: float) -> ToolResult:
        from lce.intervention.evaluate import replay
        from lce.walk.speed import _shock

        plan = plans.get(plan_ref)
        if plan is None or not plan.interventions:
            raise ValidationError("unknown plan_ref; simulate the missed payment first")
        delay = max(0.0, min(float(delay_hours), MAX_DELAY_HOURS))
        horizon = self.store.horizon_hours
        shock = _shock(self.store, list(plan.merchant_ids), plan.magnitude_multiple,
                       plan.onset_hours)
        moved = [u.model_copy(update={"t": min(u.t + delay, horizon - 1e-6)})
                 for u in plan.interventions]
        cfg = self.store.sim_config
        shocked = replay(self.store.graph, shock, [], config=cfg, run_id="adhoc:shocked")
        late = replay(self.store.graph, shock, moved, config=cfg, run_id=f"agent:late{delay:g}")
        kept_late = shocked.value_delayed - late.value_delayed
        cut_late = (100.0 * (shocked.disruption - late.disruption) / shocked.disruption
                    if shocked.disruption > 0 else 0.0)
        cf = plan.analysis.counterfactual
        data = {
            "delay": hours_text(delay),
            "payments_kept_on_time_if_late": usd(kept_late),
            "payments_kept_on_time_if_on_time": usd(cf.commerce_preserved),
            "damage_prevented_if_late": pct(cut_late),
            "damage_prevented_if_on_time": pct(cf.disruption_reduction_pct),
        }
        return ToolResult(data, f"{hours_text(delay)} late: keeps {usd(kept_late)} on time, "
                                f"not {usd(cf.commerce_preserved)}")

    def prepare_paypal_payout(self, plan_ref: str) -> ToolResult:
        from lce.execution.plan import build_plan_from_actions
        from lce.execution.service import ExecutionService

        plan = plans.get(plan_ref)
        if plan is None:
            raise ValidationError("unknown plan_ref; simulate the missed payment first")
        paypal = get_paypal_settings()
        scale = DemoScale(rate=paypal.demo_scale, currency=paypal.demo_currency)
        recipients: dict[str, Any] = {}
        session = None
        if self.session_factory is not None:
            try:
                session = self.session_factory()
                recipients = ExecutionService(session, store=self.store).recipients()
            except Exception:  # no database: payouts show as unmapped, never invented
                recipients = {}
            finally:
                if session is not None:
                    session.close()
        executable = build_plan_from_actions(
            scenario_id=plan.plan_ref, snapshot_id=self.store.snapshot_id,
            provider="paypal_sandbox", scale=scale, recipients=recipients,
            actions=[(a.merchant_id, a.action_type, a.amount_inr, a.cost_inr)
                     for a in plan.actions],
            replayed_cost_inr=plan.replayed_cost_inr)
        connected = paypal.configured and paypal.refusal() is None
        payouts = [{
            "merchant_id": a.merchant_id,
            "paypal_account": a.recipient.receiver if a.recipient else None,
            "sandbox_amount": f"${a.executed.amount:,.2f}",
            "payable": a.executable,
            "reason": a.not_executable_reason,
        } for a in executable.actions]
        payable = [p for p in payouts if p["payable"]]
        data = {
            "plan_ref": plan.plan_ref,
            "paypal": "connected (sandbox)" if connected else "not connected on this server",
            "payouts": payouts,
            "total_sandbox": f"${executable.executed_total.amount:,.2f}",
            "needs": "a person to approve on screen",
        }
        payment = {
            "plan_ref": plan.plan_ref,
            "connected": connected,
            "complete": executable.complete,
            "payouts": payouts,
            "total": data["total_sandbox"],
        }
        summary = (f"{len(payable)} PayPal payout{'s' if len(payable) != 1 else ''} ready "
                   f"({data['total_sandbox']} sandbox) · waiting for your approval")
        if not payouts:
            summary = "the plan has nothing to pay"
        elif not payable:
            summary = f"no payout can be paid yet: {payouts[0]['reason'] or 'not payable'}"
        return ToolResult(data, summary, payment=payment)
