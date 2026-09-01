"""The liquidity cascade simulator.

This is the ground-truth generator of the whole system. Everything else -
the contagion predictor, the intervention optimiser, the evaluation harness -
is measured against what this engine actually produces.

Mechanics
---------
Time advances in fixed ticks of ``dt`` hours over ``[0, T]``. Within each tick
``[t, t+dt)`` the order of operations is fixed and deliberate:

1. **Interventions** scheduled in this tick take effect.
2. **In-flight inflows** whose availability time has arrived are credited.
3. **Exogenous accrual**: ``L_i += (lambda_i - mu_i) dt``. Operating burn is
   non-discretionary and may push a node below its floor - that is what the
   deficit term of the objective measures.
4. **Shock mass** for this tick is applied.
5. **Settlement**: every open obligation whose deadline has passed is attempted,
   in priority order, subject to the payer's buffer.
6. **Default sweep**: obligations past ``d_o + grace`` are written off.
7. **Observation**: time-integrated statistics are accumulated.

Steps 2-4 precede settlement so cash that arrives in a tick is usable in that
same tick, and a shock bites before the payments it is meant to disrupt.

Common random numbers
---------------------
Every stochastic decision - a payer's discretionary lateness, a settlement lag -
is drawn from a hash of ``(run_seed, stream, obligation_id)`` rather than from a
sequentially-consumed RNG. Two runs that differ only in their intervention set
therefore make *identical* idiosyncratic draws, so the measured difference
``D(G,S,{}) - D(G,S,U)`` is the causal effect of the intervention and not
sampling noise. Without this, the optimiser would be ranking candidates by
Monte-Carlo error.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from lce.config import ObjectiveSettings, SimulationSettings, get_settings
from lce.domain.base import AMOUNT_TOL, new_id
from lce.domain.enums import (
    InterventionType,
    NodeStatus,
    ObligationStatus,
    PropagationEventType,
    ShockKind,
)
from lce.domain.events import EXTERNAL_SINK, Obligation, PaymentEvent
from lce.domain.intervention import Intervention, InterventionPlan
from lce.domain.objectives import compute_disruption, phi
from lce.domain.propagation import CascadeResult, NodeOutcome, PropagationEvent
from lce.domain.shock import Shock
from lce.errors import SimulationError
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.logging import get_logger
from lce.seeds import derive_seed
from lce.simulation.state import NodeState, PendingInflow

logger = get_logger(__name__)

# Rail latency between a payment being made and the funds being spendable.
DEFAULT_SETTLEMENT_LAG_HOURS = 4.0
# Discretionary lateness is expressed as a fraction of the grace period: a
# payer who *intends* to pay slips, but does not blow past the point where the
# obligation is written off. Sampling from the lag tail instead would produce
# baseline defaults that have nothing to do with contagion.
MAX_DISCRETIONARY_SLIP_OF_GRACE = 0.75

# Obligation states that still represent undelivered value at the horizon, and
# therefore still carry a value-weighted delay charge. CANCELLED and
# RESTRUCTURED are excluded because the liability genuinely no longer exists
# (a restructure's tranches carry it instead); DEFAULTED is included because
# the creditor is still owed - it simply will not be paid.
_UNPAID_AT_HORIZON = frozenset(
    {
        ObligationStatus.PENDING,
        ObligationStatus.PARTIALLY_SETTLED,
        ObligationStatus.DEFAULTED,
    }
)


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Everything that determines a run's output, besides the graph and shock."""

    horizon_hours: float = 168.0
    tick_hours: float = 1.0
    grace_period_hours: float = 48.0
    settlement_lag_hours: float = DEFAULT_SETTLEMENT_LAG_HOURS
    partial_payment_enabled: bool = True
    min_partial_fraction: float = 0.10
    apply_payment_discipline: bool = True
    seed: int = 20250101
    record_events: bool = True
    max_events: int = 200_000

    @classmethod
    def from_settings(
        cls, settings: SimulationSettings | None = None, *, seed: int | None = None
    ) -> SimulationConfig:
        cfg = settings or get_settings().simulation
        return cls(
            horizon_hours=cfg.horizon_hours,
            tick_hours=cfg.tick_hours,
            grace_period_hours=cfg.grace_period_hours,
            partial_payment_enabled=cfg.partial_payment_enabled,
            min_partial_fraction=cfg.min_partial_fraction,
            seed=seed if seed is not None else get_settings().random_seed,
        )

    def to_dict(self) -> dict[str, float | bool | int]:
        return {
            "horizon_hours": self.horizon_hours,
            "tick_hours": self.tick_hours,
            "grace_period_hours": self.grace_period_hours,
            "settlement_lag_hours": self.settlement_lag_hours,
            "partial_payment_enabled": self.partial_payment_enabled,
            "min_partial_fraction": self.min_partial_fraction,
            "apply_payment_discipline": self.apply_payment_discipline,
            "seed": self.seed,
        }

    @property
    def n_ticks(self) -> int:
        return max(1, int(math.ceil(self.horizon_hours / self.tick_hours)))


@dataclass(slots=True)
class _Starvation:
    """Record of an upstream miss that left a node short."""

    hop: int
    event_id: str
    t: float
    amount: float


class LiquiditySimulator:
    """Discrete-event liquidity cascade simulator.

    One instance may be reused across many runs; every :meth:`run` call rebuilds
    working state from the graph, so runs never leak into one another.
    """

    def __init__(
        self,
        graph: TemporalPaymentGraph,
        config: SimulationConfig | None = None,
        objective: ObjectiveSettings | None = None,
    ) -> None:
        self.graph = graph
        self.config = config or SimulationConfig()
        self.objective = objective or get_settings().objective

        # per-run working state
        self._nodes: dict[str, NodeState] = {}
        self._obligations: dict[str, Obligation] = {}
        self._open_by_debtor: dict[str, set[str]] = defaultdict(set)
        self._pending: list[PendingInflow] = []
        self._events: list[PropagationEvent] = []
        self._payments: list[PaymentEvent] = []
        self._starvation: dict[str, _Starvation] = {}
        self._missed_counted: set[str] = set()
        self._default_counted: set[str] = set()
        self._release_t: dict[str, float] = {}
        self._seq: int = 0
        self._run_seed: int = 0
        self._truncated: bool = False

    # ------------------------------------------------------------------ setup

    def _reset(self, run_seed: int) -> None:
        self._nodes = {
            mid: NodeState.from_profile(profile)
            for mid, profile in self.graph.merchants.items()
        }
        self._obligations = {}
        self._open_by_debtor = defaultdict(set)
        for obligation in self.graph.obligations:
            fresh = obligation.model_copy(
                update={
                    "status": ObligationStatus.PENDING,
                    "amount_paid": 0.0,
                    "settled_t": None,
                }
            )
            self._obligations[fresh.obligation_id] = fresh
            self._open_by_debtor[fresh.debtor_id].add(fresh.obligation_id)
        self._pending = []
        self._events = []
        self._payments = []
        self._starvation = {}
        self._missed_counted = set()
        self._default_counted = set()
        self._release_t = {}
        self._seq = 0
        self._run_seed = run_seed
        self._truncated = False

    # ----------------------------------------------- common random numbers

    def _crn(self, stream: str, key: str) -> float:
        """Deterministic uniform in [0, 1) keyed by ``(run_seed, stream, key)``.

        Independent of call order, which is what makes counterfactual runs
        comparable.
        """
        return derive_seed(self._run_seed, stream, key) / 2**32

    def _release_time(self, obligation: Obligation) -> float:
        """When the debtor is *willing* to pay, ignoring whether it can afford to.

        Models discretionary lateness: a payer with discipline ``p`` pays on the
        deadline with probability ``p``, otherwise slips by a log-normal delay.
        """
        cached = self._release_t.get(obligation.obligation_id)
        if cached is not None:
            return cached

        release = obligation.due_t
        if self.config.apply_payment_discipline and obligation.debtor_id in self._nodes:
            discipline = self._nodes[obligation.debtor_id].profile.payment_discipline
            if self._crn("discipline", obligation.obligation_id) > discipline:
                ceiling = MAX_DISCRETIONARY_SLIP_OF_GRACE * self.config.grace_period_hours
                slip = self._crn("defer", obligation.obligation_id) * ceiling
                release = obligation.due_t + max(0.0, slip)

        self._release_t[obligation.obligation_id] = release
        return release

    # ------------------------------------------------------------- recording

    def _emit(
        self,
        t: float,
        event_type: PropagationEventType,
        merchant_id: str,
        *,
        counterparty_id: str | None = None,
        obligation_id: str | None = None,
        amount: float = 0.0,
        caused_by: str | None = None,
        hop: int = 0,
        detail: dict[str, object] | None = None,
    ) -> str:
        """Append a propagation event and return its id."""
        event_id = new_id("prp")
        if not self.config.record_events:
            return event_id
        if len(self._events) >= self.config.max_events:
            self._truncated = True
            return event_id

        node = self._nodes.get(merchant_id)
        self._events.append(
            PropagationEvent(
                event_id=event_id,
                sequence=self._seq,
                t=t,
                type=event_type,
                merchant_id=merchant_id,
                counterparty_id=counterparty_id,
                obligation_id=obligation_id,
                amount=amount,
                caused_by=caused_by,
                hop=hop,
                balance_after=node.cash if node else None,
                buffer_after=node.buffer if node else None,
                status_after=node.status if node else None,
                detail=detail or {},
            )
        )
        self._seq += 1
        return event_id

    # ------------------------------------------------------------------ run

    def run(
        self,
        shock: Shock | None = None,
        plan: InterventionPlan | None = None,
        *,
        run_id: str | None = None,
        config_hash: str | None = None,
    ) -> CascadeResult:
        """Simulate ``shock`` (and optionally ``plan``) over the horizon.

        Passing ``shock=None`` produces the *undisturbed baseline*, which the
        evaluation layer subtracts to isolate shock-attributable damage.
        """
        cfg = self.config
        if cfg.horizon_hours <= 0 or cfg.tick_hours <= 0:
            raise SimulationError("horizon and tick must be positive")

        # NB: deliberately NOT keyed on run_id. Every run of the same
        # configuration must draw identical idiosyncratic randomness so that
        # D(baseline) - D(plan) is a causal effect rather than sampling noise.
        run_seed = derive_seed(cfg.seed, "sim")
        self._reset(run_seed)

        interventions = list(plan.interventions) if plan is not None else []
        interventions.sort(key=lambda u: (u.t, u.intervention_id))
        pending_interventions = list(interventions)

        if shock is not None:
            for component in shock.components:
                if component.merchant_id not in self._nodes:
                    raise SimulationError(
                        f"shock targets unknown merchant {component.merchant_id!r}"
                    )
                self._nodes[component.merchant_id].was_shocked = True
                self._nodes[component.merchant_id].hop_distance = 0

        dt = cfg.tick_hours
        for tick in range(cfg.n_ticks):
            t0 = tick * dt
            t1 = min(t0 + dt, cfg.horizon_hours)
            if t1 <= t0:
                break

            pending_interventions = self._apply_interventions(pending_interventions, t0, t1)
            self._deliver_inflows(t0, t1)
            self._accrue(t1 - t0, t0)
            if shock is not None:
                self._apply_shock(shock, t0, t1)
            self._settle(t0, t1)
            self._sweep_defaults(t1)
            self._observe(t0, t1 - t0)

        return self._finalise(shock, plan, run_id=run_id, config_hash=config_hash)

    # -------------------------------------------------------------- tick steps

    def _apply_interventions(
        self, queue: list[Intervention], t0: float, t1: float
    ) -> list[Intervention]:
        """Apply every intervention scheduled in ``[t0, t1)``; return the remainder."""
        remaining: list[Intervention] = []
        for u in queue:
            if u.t >= t1:
                remaining.append(u)
                continue
            self._apply_intervention(u, max(u.t, t0))
        return remaining

    def _apply_intervention(self, u: Intervention, t: float) -> None:
        node = self._nodes.get(u.merchant_id)
        if node is None:
            raise SimulationError(f"intervention targets unknown merchant {u.merchant_id!r}")

        detail: dict[str, object] = {"type": str(u.type), "cost": u.cost}
        match u.type:
            case InterventionType.LIQUIDITY_INJECTION:
                node.receive(u.amount)
            case InterventionType.CREDIT_LINE_INCREASE:
                node.credit_limit += u.amount
            case InterventionType.RECEIVABLE_ACCELERATION:
                obligation = self._obligations.get(u.target_obligation_id or "")
                if obligation is None or not obligation.is_open:
                    detail["skipped"] = "obligation closed or unknown"
                else:
                    new_due = max(t, obligation.due_t - u.shift_hours)
                    self._replace_obligation(obligation.with_deadline(new_due))
                    self._release_t.pop(obligation.obligation_id, None)
                    detail |= {"old_due_t": obligation.due_t, "new_due_t": new_due}
            case InterventionType.SUPPLIER_TERM_EXTENSION:
                obligation = self._obligations.get(u.target_obligation_id or "")
                if obligation is None or not obligation.is_open:
                    detail["skipped"] = "obligation closed or unknown"
                else:
                    new_due = obligation.due_t + u.shift_hours
                    self._replace_obligation(obligation.with_deadline(new_due))
                    self._release_t.pop(obligation.obligation_id, None)
                    detail |= {"old_due_t": obligation.due_t, "new_due_t": new_due}
            case InterventionType.REPAYMENT_RESTRUCTURE:
                detail |= self._restructure(u, t)

        self._emit(
            t,
            PropagationEventType.INTERVENTION_APPLIED,
            u.merchant_id,
            obligation_id=u.target_obligation_id,
            amount=u.amount,
            detail=detail,
        )

    def _restructure(self, u: Intervention, t: float) -> dict[str, object]:
        """Split an obligation into ``u.tranches`` instalments.

        The parent is marked ``RESTRUCTURED`` and replaced by children whose
        amounts sum exactly to the outstanding principal - restructuring changes
        *when* cash is owed, never *how much*.
        """
        parent = self._obligations.get(u.target_obligation_id or "")
        if parent is None or not parent.is_open:
            return {"skipped": "obligation closed or unknown"}

        outstanding = parent.outstanding
        n = max(1, u.tranches)
        per = outstanding / n
        start = max(t, parent.due_t)

        self._replace_obligation(parent.model_copy(update={"status": ObligationStatus.RESTRUCTURED}))
        self._open_by_debtor[parent.debtor_id].discard(parent.obligation_id)

        children: list[str] = []
        for k in range(n):
            amount = outstanding - per * (n - 1) if k == n - 1 else per
            child = Obligation(
                debtor_id=parent.debtor_id,
                creditor_id=parent.creditor_id,
                amount=amount,
                issued_t=t,
                due_t=start + k * u.tranche_spacing_hours,
                kind=parent.kind,
                priority=parent.priority,
                parent_obligation_id=parent.obligation_id,
                metadata={"restructured_from": parent.obligation_id},
            )
            self._obligations[child.obligation_id] = child
            self._open_by_debtor[child.debtor_id].add(child.obligation_id)
            children.append(child.obligation_id)

        return {"tranches": n, "children": children, "principal": outstanding}

    def _replace_obligation(self, obligation: Obligation) -> None:
        self._obligations[obligation.obligation_id] = obligation
        if obligation.is_open:
            self._open_by_debtor[obligation.debtor_id].add(obligation.obligation_id)
        else:
            self._open_by_debtor[obligation.debtor_id].discard(obligation.obligation_id)

    def _deliver_inflows(self, t0: float, t1: float) -> None:
        """Credit in-flight payments whose funds become available this tick."""
        if not self._pending:
            return
        still_pending: list[PendingInflow] = []
        for inflow in self._pending:
            if inflow.available_t < t1:
                node = self._nodes.get(inflow.payee_id)
                if node is not None:
                    node.receive(inflow.amount)
            else:
                still_pending.append(inflow)
        self._pending = still_pending

    def _accrue(self, dt: float, t: float) -> None:
        for node in self._nodes.values():
            node.accrue(dt)

    def _apply_shock(self, shock: Shock, t0: float, t1: float) -> None:
        for component in shock.components:
            node = self._nodes[component.merchant_id]
            match component.kind:
                case ShockKind.CREDIT_LINE_CUT:
                    if t0 <= component.t < t1:
                        node.credit_limit = max(
                            node.credit_drawn, node.credit_limit - component.magnitude
                        )
                        self._emit(
                            component.t,
                            PropagationEventType.SHOCK_APPLIED,
                            node.merchant_id,
                            amount=component.magnitude,
                            hop=0,
                            detail={"kind": str(component.kind)},
                        )
                case ShockKind.MISSED_INBOUND | ShockKind.COUNTERPARTY_DEFAULT:
                    if not (t0 <= component.t < t1):
                        continue
                    self._apply_inbound_failure(component, node, t0)
                case _:
                    mass = component.magnitude_in(t0, t1)
                    if mass > 0:
                        node.drain(mass)
                        event_id = self._emit(
                            max(component.t, t0),
                            PropagationEventType.SHOCK_APPLIED,
                            node.merchant_id,
                            amount=mass,
                            hop=0,
                            detail={"kind": str(component.kind)},
                        )
                        node.last_impact_event_id = event_id

    def _apply_inbound_failure(self, component, node: NodeState, t: float) -> None:
        """An expected receivable never arrives.

        With a named obligation, the receivable is written off - the node loses
        both the cash *and* the asset. Without one, the magnitude is drained
        directly, which is the same liquidity hit expressed on the balance.

        The write-off is booked as a **default by the debtor**, not a
        cancellation. Cancelling would quietly release the debtor from a
        liability it in fact failed to honour, and that relief shows up in the
        objective as a *reduction* in disruption which offsets the creditor's
        loss - leaving a severe missed payment scoring as though nothing had
        happened.
        """
        detail: dict[str, object] = {"kind": str(component.kind)}
        amount = component.magnitude
        target_id = component.target_obligation_id

        if target_id:
            obligation = self._obligations.get(target_id)
            if obligation is None:
                raise SimulationError(f"shock references unknown obligation {target_id!r}")
            amount = obligation.outstanding
            self._replace_obligation(
                obligation.model_copy(update={"status": ObligationStatus.DEFAULTED})
            )
            self._open_by_debtor[obligation.debtor_id].discard(target_id)

            # Charge the debtor for the default, and record it as counted so the
            # end-of-horizon sweep does not charge it a second time.
            if target_id not in self._default_counted:
                self._default_counted.add(target_id)
                debtor = self._nodes.get(obligation.debtor_id)
                if debtor is not None:
                    # Only the default count here; the value-weighted delay is
                    # charged once, uniformly, by _finalise.
                    debtor.defaults_caused += 1
                    if debtor.mark_defaulted(component.t):
                        self._emit(
                            component.t,
                            PropagationEventType.NODE_DEFAULTED,
                            obligation.debtor_id,
                            counterparty_id=obligation.creditor_id,
                            obligation_id=target_id,
                            amount=obligation.outstanding,
                            hop=0,
                            detail={"cause": "shock_write_off"},
                        )
            detail |= {"defaulted_obligation": target_id, "debtor": obligation.debtor_id}
        else:
            node.drain(amount)

        event_id = self._emit(
            component.t,
            PropagationEventType.SHOCK_APPLIED,
            node.merchant_id,
            obligation_id=target_id,
            amount=amount,
            hop=0,
            detail=detail,
        )
        node.last_impact_event_id = event_id

    def _settle(self, t0: float, t1: float) -> None:
        """Attempt every obligation whose deadline has arrived, in priority order."""
        for debtor_id in sorted(self._open_by_debtor):
            open_ids = self._open_by_debtor[debtor_id]
            if not open_ids:
                continue
            node = self._nodes.get(debtor_id)
            if node is None:
                continue

            due = [
                self._obligations[oid]
                for oid in open_ids
                if self._obligations[oid].is_open
                and self._obligations[oid].due_t < t1
                and self._release_time(self._obligations[oid]) < t1
            ]
            if not due:
                continue
            # Highest priority first, then earliest deadline, then largest.
            due.sort(key=lambda o: (-o.priority, o.due_t, -o.amount, o.obligation_id))

            for obligation in due:
                self._attempt_settlement(node, obligation, t0, t1)

    def _attempt_settlement(
        self, node: NodeState, obligation: Obligation, t0: float, t1: float
    ) -> None:
        outstanding = obligation.outstanding
        if outstanding <= AMOUNT_TOL:
            self._replace_obligation(obligation.touched(t1, self.config.grace_period_hours))
            return

        pay_t = max(t0, min(obligation.due_t, t1 - 1e-9), self._release_time(obligation))
        pay_t = min(max(pay_t, t0), t1 - 1e-9) if t1 > t0 else t0

        if node.can_pay(outstanding):
            self._execute_payment(node, obligation, outstanding, pay_t, partial=False)
            return

        capacity = node.max_payable()
        min_slice = self.config.min_partial_fraction * outstanding
        if (
            self.config.partial_payment_enabled
            and capacity > 0
            and capacity >= min_slice
        ):
            self._execute_payment(node, obligation, capacity, pay_t, partial=True)
            return

        self._register_miss(node, obligation, outstanding, pay_t)

    def _execute_payment(
        self,
        node: NodeState,
        obligation: Obligation,
        amount: float,
        t: float,
        *,
        partial: bool,
    ) -> None:
        paid = node.disburse(amount)
        if paid <= AMOUNT_TOL:
            self._register_miss(node, obligation, obligation.outstanding, t)
            return

        settlement_lag = self.config.settlement_lag_hours
        payment = PaymentEvent(
            payer_id=obligation.debtor_id,
            payee_id=obligation.creditor_id,
            amount=paid,
            t=t,
            obligation_id=obligation.obligation_id,
            settlement_lag_hours=settlement_lag,
        )
        self._payments.append(payment)

        if obligation.creditor_id in self._nodes:
            self._pending.append(
                PendingInflow(
                    available_t=t + settlement_lag,
                    payee_id=obligation.creditor_id,
                    amount=paid,
                    source_id=obligation.debtor_id,
                    source_event_id=payment.event_id,
                    obligation_id=obligation.obligation_id,
                )
            )

        updated = obligation.with_payment(paid, t, self.config.grace_period_hours)
        self._replace_obligation(updated)

        is_late = t > obligation.due_t + AMOUNT_TOL
        if is_late:
            # Charge the delay penalty on *this slice*, at the moment it is paid.
            # Charging only on full settlement would let a part-payer escape the
            # penalty on everything it did pay, which makes the objective
            # non-monotone: starving a node into paying 90% late instead of 100%
            # late would score as an improvement.
            delay = max(0.0, t - obligation.due_t)
            node.weighted_delay += paid * phi(delay, self.objective.delay_unit_hours)
            node.value_delayed += paid
        if is_late and updated.outstanding <= AMOUNT_TOL:
            node.obligations_settled_late += 1

        event_type = (
            PropagationEventType.PAYMENT_PARTIAL if partial else PropagationEventType.PAYMENT_MADE
        )
        event_id = self._emit(
            t,
            event_type,
            node.merchant_id,
            counterparty_id=obligation.creditor_id,
            obligation_id=obligation.obligation_id,
            amount=paid,
            caused_by=node.last_impact_event_id if partial else None,
            hop=node.hop_distance or 0,
            detail={"late": is_late, "outstanding_after": updated.outstanding},
        )

        if partial:
            # A part-payment still starves the creditor of the remainder.
            self._register_shortfall(
                node, obligation, updated.outstanding, t, event_id, constrained=True
            )

    def _register_miss(
        self, node: NodeState, obligation: Obligation, amount: float, t: float
    ) -> None:
        """The debtor cannot pay at all: record the miss and starve the creditor."""
        first_miss = obligation.obligation_id not in self._missed_counted
        if first_miss:
            self._missed_counted.add(obligation.obligation_id)
            node.obligations_missed += 1

        event_id = self._emit(
            t,
            PropagationEventType.PAYMENT_MISSED,
            node.merchant_id,
            counterparty_id=obligation.creditor_id,
            obligation_id=obligation.obligation_id,
            amount=amount,
            caused_by=node.last_impact_event_id,
            hop=node.hop_distance or 0,
            detail={"buffer": node.buffer, "required": amount},
        )
        self._register_shortfall(node, obligation, amount, t, event_id, constrained=True)

    def _register_shortfall(
        self,
        node: NodeState,
        obligation: Obligation,
        amount: float,
        t: float,
        event_id: str,
        *,
        constrained: bool,
    ) -> None:
        """Mark the debtor constrained and propagate starvation to the creditor."""
        if constrained and node.mark_constrained(t, hop=self._hop_for(node)):
            self._emit(
                t,
                PropagationEventType.NODE_CONSTRAINED,
                node.merchant_id,
                obligation_id=obligation.obligation_id,
                amount=amount,
                caused_by=self._cause_for(node),
                hop=node.hop_distance or 0,
                detail={"buffer": node.buffer},
            )
        node.last_impact_event_id = event_id

        creditor_id = obligation.creditor_id
        if creditor_id == EXTERNAL_SINK or creditor_id not in self._nodes or amount <= AMOUNT_TOL:
            return

        hop = (node.hop_distance or 0) + 1
        existing = self._starvation.get(creditor_id)
        if existing is None or hop < existing.hop or (hop == existing.hop and t < existing.t):
            self._starvation[creditor_id] = _Starvation(
                hop=hop, event_id=event_id, t=t, amount=amount
            )

    def _hop_for(self, node: NodeState) -> int:
        if node.hop_distance is not None:
            return node.hop_distance
        starve = self._starvation.get(node.merchant_id)
        return starve.hop if starve is not None else 0

    def _cause_for(self, node: NodeState) -> str | None:
        if node.last_impact_event_id is not None:
            return node.last_impact_event_id
        starve = self._starvation.get(node.merchant_id)
        return starve.event_id if starve is not None else None

    def _sweep_defaults(self, t: float) -> None:
        """Write off obligations that have passed the grace period."""
        grace = self.config.grace_period_hours
        for debtor_id in sorted(self._open_by_debtor):
            for oid in list(self._open_by_debtor[debtor_id]):
                obligation = self._obligations[oid]
                if oid in self._default_counted or not obligation.is_defaulted_at(t, grace):
                    continue
                self._default_counted.add(oid)
                node = self._nodes.get(debtor_id)
                if node is None:
                    continue
                node.defaults_caused += 1
                self._replace_obligation(
                    obligation.model_copy(update={"status": ObligationStatus.DEFAULTED})
                )
                if node.mark_defaulted(t):
                    self._emit(
                        t,
                        PropagationEventType.NODE_DEFAULTED,
                        debtor_id,
                        counterparty_id=obligation.creditor_id,
                        obligation_id=oid,
                        amount=obligation.outstanding,
                        caused_by=self._cause_for(node),
                        hop=node.hop_distance or 0,
                    )

    def _observe(self, t: float, dt: float) -> None:
        for node in self._nodes.values():
            node.observe(t, dt)
            previous = node.status
            node.refresh_status()
            if previous is NodeStatus.HEALTHY and node.status is NodeStatus.STRESSED:
                self._emit(
                    t + dt,
                    PropagationEventType.NODE_STRESSED,
                    node.merchant_id,
                    hop=node.hop_distance or 0,
                    caused_by=self._cause_for(node),
                )

    # ------------------------------------------------------------- finalise

    def _finalise(
        self,
        shock: Shock | None,
        plan: InterventionPlan | None,
        *,
        run_id: str | None,
        config_hash: str | None,
    ) -> CascadeResult:
        """Charge end-of-horizon penalties and assemble the result."""
        horizon = self.config.horizon_hours
        for obligation in self._obligations.values():
            if obligation.status not in _UNPAID_AT_HORIZON or obligation.due_t >= horizon:
                continue
            node = self._nodes.get(obligation.debtor_id)
            if node is None:
                continue
            # Still unpaid at T: charge the residual for the whole overdue
            # window. Together with the per-slice charge above this makes the
            # total penalty sum_k (slice_k * lateness_k) - monotone in both how
            # much is unpaid and how long it stays unpaid.
            #
            # DEFAULTED obligations are charged here too, on top of their flat
            # default fee. Excluding them (as `is_open` would) makes writing a
            # debt off *cheaper* than paying it late, because the value-weighted
            # delay - which scales with the amount and the overdue window -
            # simply vanishes and is replaced by a fixed fee. The objective then
            # falls when a shock turns late payments into defaults, and every
            # monotonicity guarantee built on it breaks.
            delay = horizon - obligation.due_t
            node.weighted_delay += obligation.outstanding * phi(
                delay, self.objective.delay_unit_hours
            )
            node.value_delayed += obligation.outstanding

        outcomes: dict[str, NodeOutcome] = {}
        for mid, node in self._nodes.items():
            starve = self._starvation.get(mid)
            hop = node.hop_distance
            if hop is None and starve is not None and node.first_constrained_t is not None:
                hop = starve.hop
            outcomes[mid] = NodeOutcome(
                merchant_id=mid,
                systemic_weight=node.profile.systemic_weight,
                final_status=node.status,
                was_shocked=node.was_shocked,
                became_constrained=node.first_constrained_t is not None,
                became_defaulted=node.first_defaulted_t is not None,
                first_constrained_t=node.first_constrained_t,
                first_defaulted_t=node.first_defaulted_t,
                hop_distance=hop,
                value_delayed=node.value_delayed,
                weighted_delay=node.weighted_delay,
                defaults_caused=node.defaults_caused,
                deficit_integral=node.deficit_integral,
                min_buffer=node.min_buffer if math.isfinite(node.min_buffer) else 0.0,
                final_balance=node.cash,
                obligations_missed=node.obligations_missed,
                obligations_settled_late=node.obligations_settled_late,
            )

        result = CascadeResult(
            run_id=run_id or new_id("run"),
            shock_id=shock.shock_id if shock else None,
            plan_id=plan.plan_id if plan else None,
            horizon_hours=horizon,
            events=self._events,
            outcomes=outcomes,
            seed=self.config.seed,
            config_hash=config_hash,
            metadata={
                "n_payments": len(self._payments),
                "events_truncated": self._truncated,
                "config": self.config.to_dict(),
            },
        )
        breakdown = compute_disruption(result, self.objective)
        return result.model_copy(
            update={"disruption": breakdown.total, "disruption_breakdown": breakdown.to_dict()}
        )

    # ------------------------------------------------------------- accessors

    @property
    def emitted_payments(self) -> list[PaymentEvent]:
        """Payments generated by the last run - the observable event stream."""
        return list(self._payments)

    def obligation_book(self) -> list[Obligation]:
        """Final state of every obligation after the last run."""
        return sorted(self._obligations.values(), key=lambda o: (o.due_t, o.obligation_id))
