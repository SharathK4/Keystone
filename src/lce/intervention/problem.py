"""The intervention problem: objective, constraints, feasibility.

This module is the executable half of sections 2 and 3 of
``docs/PHASE4_INTERVENTION.md``. It defines what is being minimised, what counts
as a feasible action, and how a violation is reported - and it does all of that
against quantities that already exist. No new score is invented here: ``D`` is
the Phase-1 disruption objective computed by the simulator, and ``Cost`` is the
per-action cost model already carried on
:class:`~lce.domain.intervention.Intervention`.

Two equivalent formulations
---------------------------
**Penalised.** The default. Rupees of deployed capital are converted into units
of disruption by a declared preference parameter :math:`\\lambda`:

.. math::

    \\min_{a \\in A} \\; J(a) = D(a) + \\lambda \\, \\mathrm{Cost}(a)

**Constrained.** The same feasible set and the same simulated ``D``, asking the
dual question - the cheapest action that gets disruption under a ceiling:

.. math::

    \\min_{a \\in A} \\; \\mathrm{Cost}(a)
    \\quad \\text{s.t.} \\quad D(a) \\le \\varepsilon

Both are solved over the *same* simulated objective, so their answers are
directly comparable. :math:`\\lambda` and :math:`\\varepsilon` are declared
preferences recorded in every run, never fitted to make a number look better.

Feasibility is checked, not assumed
-----------------------------------
:func:`check_action` returns every violation it finds rather than the first, and
:class:`FeasibilityReport` is attached to results so an infeasible
recommendation is visible instead of silently dropped. The invariant with the
most teeth is **no money creation**: every action type except an injection or a
credit line must leave the total obligated principal unchanged. Restructuring
moves *when* cash is owed and never *how much*, which the simulator's
``_restructure`` already guarantees by construction; the check pins it so a
future change cannot quietly break it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from lce.domain.base import AMOUNT_TOL
from lce.domain.enums import InterventionType
from lce.domain.intervention import Intervention
from lce.graph.temporal_graph import TemporalPaymentGraph

HOURS_PER_DAY = 24.0

#: Action types that add cash or headroom to the system from outside it. Every
#: other type only rearranges existing obligations, and is held to the
#: conservation check.
CAPITAL_TYPES = frozenset(
    {InterventionType.LIQUIDITY_INJECTION, InterventionType.CREDIT_LINE_INCREASE}
)


@dataclass(frozen=True, slots=True)
class InterventionConstraints:
    """The feasible set ``A``, as bounds rather than prose.

    Defaults are deliberately conservative: a five-day supplier extension and a
    four-tranche restructure are ordinary commercial terms, and anything beyond
    that stops being an intervention and starts being a renegotiation the model
    has no standing to assume.
    """

    budget: float | None = None
    """``B``: total deployable cost. ``None`` means unbounded, which is only
    sensible when the penalised form is doing the limiting instead."""
    max_actions: int = 3
    max_per_merchant: int = 1
    max_extension_hours: float = 5 * HOURS_PER_DAY
    max_acceleration_hours: float = 7 * HOURS_PER_DAY
    max_tranches: int = 4
    max_restructure_span_hours: float = 28 * HOURS_PER_DAY
    min_amount: float = 1000.0
    max_injection_per_merchant: float | None = None
    """Cap on a single injection. ``None`` derives it from the merchant's own
    horizon payables, so the cap scales with the merchant rather than the
    currency."""
    decision_time: float = 0.0
    """Actions may not be scheduled before this. An optimiser that schedules an
    action *before* the moment it was decided is time travel, not policy."""
    horizon_hours: float = 168.0
    enforce_liquidity_floor: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget,
            "max_actions": self.max_actions,
            "max_per_merchant": self.max_per_merchant,
            "max_extension_hours": self.max_extension_hours,
            "max_acceleration_hours": self.max_acceleration_hours,
            "max_tranches": self.max_tranches,
            "max_restructure_span_hours": self.max_restructure_span_hours,
            "min_amount": self.min_amount,
            "max_injection_per_merchant": self.max_injection_per_merchant,
            "decision_time": self.decision_time,
            "horizon_hours": self.horizon_hours,
            "enforce_liquidity_floor": self.enforce_liquidity_floor,
        }


@dataclass(frozen=True, slots=True)
class ObjectiveSpec:
    """Which form of the problem is being solved, and with what preferences."""

    form: str = "penalised"
    """``penalised`` minimises ``D + lambda * Cost``; ``constrained`` minimises
    ``Cost`` subject to ``D <= epsilon``."""
    lam: float = 1.0
    """``lambda``: units of disruption per rupee of deployed cost. A declared
    preference. At 0 the optimiser spends freely; the benchmark reports capital
    efficiency separately so the choice is visible rather than buried."""
    epsilon: float | None = None
    """Absolute disruption ceiling for the constrained form."""
    epsilon_fraction: float = 0.5
    """Used when ``epsilon`` is not given: the ceiling becomes this fraction of
    the do-nothing disruption, which makes the target comparable across networks
    that differ by orders of magnitude in absolute rupees."""

    def __post_init__(self) -> None:
        if self.form not in ("penalised", "constrained"):
            raise ValueError(f"unknown objective form {self.form!r}")

    def resolve_epsilon(self, baseline_disruption: float) -> float:
        if self.epsilon is not None:
            return float(self.epsilon)
        return float(self.epsilon_fraction * max(baseline_disruption, 0.0))

    def value(self, disruption: float, cost: float) -> float:
        """``J(a)`` for the penalised form; the cost for the constrained one.

        The constrained form's ranking key is the cost alone - the disruption
        ceiling is a feasibility test, not part of the objective, and folding it
        in would quietly turn the constrained problem back into the penalised
        one.
        """
        if self.form == "penalised":
            return disruption + self.lam * cost
        return cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "form": self.form,
            "lambda": self.lam,
            "epsilon": self.epsilon,
            "epsilon_fraction": self.epsilon_fraction,
        }


@dataclass(slots=True)
class Violation:
    """One broken constraint, named and quantified."""

    constraint: str
    detail: str
    intervention_id: str | None = None
    observed: float | None = None
    limit: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint": self.constraint,
            "detail": self.detail,
            "intervention_id": self.intervention_id,
            "observed": self.observed,
            "limit": self.limit,
        }


@dataclass(slots=True)
class FeasibilityReport:
    """Every violation an action carries. Empty means feasible."""

    violations: list[Violation] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return not self.violations

    def names(self) -> list[str]:
        return sorted({v.constraint for v in self.violations})

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "n_violations": len(self.violations),
            "constraints": self.names(),
            "violations": [v.to_dict() for v in self.violations],
        }


def obligated_principal(graph: TemporalPaymentGraph) -> float:
    """Total outstanding principal across the whole obligation book.

    The conserved quantity. Any action that is not an injection or a credit line
    must leave this unchanged: rescheduling debt is not the same as retiring it,
    and an optimiser that could quietly forgive principal would find a very
    cheap way to make disruption disappear.
    """
    return sum(o.outstanding for o in graph.obligations if o.is_open)


def check_action(
    interventions: Sequence[Intervention],
    graph: TemporalPaymentGraph,
    constraints: InterventionConstraints,
) -> FeasibilityReport:
    """Structural feasibility of an action, checked term by term.

    Structural only: this does not run the simulator. The liquidity-floor
    constraint depends on the realised trajectory and is therefore checked in
    :func:`check_realised_floor` after evaluation, which is the only place the
    answer actually exists.
    """
    report = FeasibilityReport()
    per_merchant: dict[str, int] = {}
    total_cost = 0.0

    if len(interventions) > constraints.max_actions:
        report.violations.append(
            Violation(
                "cardinality",
                f"{len(interventions)} actions exceeds the cap",
                observed=float(len(interventions)),
                limit=float(constraints.max_actions),
            )
        )

    for u in interventions:
        total_cost += u.cost
        per_merchant[u.merchant_id] = per_merchant.get(u.merchant_id, 0) + 1

        if not graph.has_merchant(u.merchant_id):
            report.violations.append(
                Violation(
                    "unknown_merchant",
                    f"{u.merchant_id!r} is not in the network",
                    u.intervention_id,
                )
            )
            continue

        _check_timing(u, constraints, report)
        _check_type_bounds(u, graph, constraints, report)

    if constraints.budget is not None and total_cost > constraints.budget + AMOUNT_TOL:
        report.violations.append(
            Violation(
                "budget",
                f"cost {total_cost:,.0f} exceeds budget",
                observed=total_cost,
                limit=constraints.budget,
            )
        )

    for merchant_id, count in sorted(per_merchant.items()):
        if count > constraints.max_per_merchant:
            report.violations.append(
                Violation(
                    "capacity",
                    f"{count} actions on {merchant_id}",
                    observed=float(count),
                    limit=float(constraints.max_per_merchant),
                )
            )
    return report


def _check_timing(
    u: Intervention, constraints: InterventionConstraints, report: FeasibilityReport
) -> None:
    if u.t < constraints.decision_time - 1e-9:
        report.violations.append(
            Violation(
                "timing",
                f"scheduled at t={u.t:.1f}h, before the decision time",
                u.intervention_id,
                observed=u.t,
                limit=constraints.decision_time,
            )
        )
    if u.t >= constraints.horizon_hours:
        report.violations.append(
            Violation(
                "timing",
                f"scheduled at t={u.t:.1f}h, at or past the horizon",
                u.intervention_id,
                observed=u.t,
                limit=constraints.horizon_hours,
            )
        )


def _check_type_bounds(
    u: Intervention,
    graph: TemporalPaymentGraph,
    constraints: InterventionConstraints,
    report: FeasibilityReport,
) -> None:
    match u.type:
        case InterventionType.LIQUIDITY_INJECTION | InterventionType.CREDIT_LINE_INCREASE:
            if u.amount < constraints.min_amount:
                report.violations.append(
                    Violation(
                        "min_amount",
                        f"amount {u.amount:,.0f} below the floor",
                        u.intervention_id,
                        observed=u.amount,
                        limit=constraints.min_amount,
                    )
                )
            cap = _injection_cap(u.merchant_id, graph, constraints)
            if cap is not None and u.amount > cap + AMOUNT_TOL:
                report.violations.append(
                    Violation(
                        "injection_cap",
                        f"amount {u.amount:,.0f} exceeds the per-merchant cap",
                        u.intervention_id,
                        observed=u.amount,
                        limit=cap,
                    )
                )

        case InterventionType.SUPPLIER_TERM_EXTENSION:
            if u.shift_hours > constraints.max_extension_hours + 1e-9:
                report.violations.append(
                    Violation(
                        "max_term_extension",
                        f"shift of {u.shift_hours / HOURS_PER_DAY:.1f}d exceeds the bound",
                        u.intervention_id,
                        observed=u.shift_hours,
                        limit=constraints.max_extension_hours,
                    )
                )
            _check_deadline_inside_horizon(u, graph, constraints, report)

        case InterventionType.RECEIVABLE_ACCELERATION:
            if u.shift_hours > constraints.max_acceleration_hours + 1e-9:
                report.violations.append(
                    Violation(
                        "max_acceleration",
                        f"shift of {u.shift_hours / HOURS_PER_DAY:.1f}d exceeds the bound",
                        u.intervention_id,
                        observed=u.shift_hours,
                        limit=constraints.max_acceleration_hours,
                    )
                )
            obligation = _obligation(u, graph)
            if obligation is not None and obligation.due_t - u.shift_hours < u.t - 1e-9:
                report.violations.append(
                    Violation(
                        "deadline",
                        "acceleration would move the deadline before the action itself",
                        u.intervention_id,
                        observed=obligation.due_t - u.shift_hours,
                        limit=u.t,
                    )
                )

        case InterventionType.REPAYMENT_RESTRUCTURE:
            if u.tranches > constraints.max_tranches:
                report.violations.append(
                    Violation(
                        "max_repayment_modification",
                        f"{u.tranches} tranches exceeds the bound",
                        u.intervention_id,
                        observed=float(u.tranches),
                        limit=float(constraints.max_tranches),
                    )
                )
            span = (u.tranches - 1) * u.tranche_spacing_hours
            if span > constraints.max_restructure_span_hours + 1e-9:
                report.violations.append(
                    Violation(
                        "max_repayment_modification",
                        f"schedule spans {span / HOURS_PER_DAY:.1f}d",
                        u.intervention_id,
                        observed=span,
                        limit=constraints.max_restructure_span_hours,
                    )
                )
            _check_deadline_inside_horizon(u, graph, constraints, report)


def _obligation(u: Intervention, graph: TemporalPaymentGraph):
    if not u.target_obligation_id:
        return None
    try:
        return graph.obligation(u.target_obligation_id)
    except Exception:
        return None


def _check_deadline_inside_horizon(
    u: Intervention,
    graph: TemporalPaymentGraph,
    constraints: InterventionConstraints,
    report: FeasibilityReport,
) -> None:
    """A deadline pushed past the horizon vanishes from the accounting.

    Phase 2 hit exactly this: an obligation deferred beyond the measured window
    is never charged to anyone, so deferring it registers as *relief*. An
    optimiser handed that loophole would learn to push every deadline out of
    sight, and the reported disruption would be an artefact of the window.
    """
    obligation = _obligation(u, graph)
    if obligation is None:
        report.violations.append(
            Violation(
                "unknown_obligation",
                f"target obligation {u.target_obligation_id!r} is not in the book",
                u.intervention_id,
            )
        )
        return

    if u.type is InterventionType.SUPPLIER_TERM_EXTENSION:
        latest = obligation.due_t + u.shift_hours
    else:
        latest = max(u.t, obligation.due_t) + (u.tranches - 1) * u.tranche_spacing_hours

    if latest >= constraints.horizon_hours:
        report.violations.append(
            Violation(
                "deadline",
                (
                    f"final deadline {latest:.0f}h falls outside the horizon, where "
                    "the obligation would leave the accounting entirely"
                ),
                u.intervention_id,
                observed=latest,
                limit=constraints.horizon_hours,
            )
        )


def _injection_cap(
    merchant_id: str, graph: TemporalPaymentGraph, constraints: InterventionConstraints
) -> float | None:
    """Per-merchant injection cap, scaled to the merchant unless one is given."""
    if constraints.max_injection_per_merchant is not None:
        return constraints.max_injection_per_merchant
    payables = sum(
        o.outstanding
        for o in graph.payables_of(merchant_id)
        if o.is_open and o.due_t <= constraints.horizon_hours
    )
    buffer = max(graph.merchant(merchant_id).initial_buffer, 1.0)
    # Twice what the merchant owes inside the horizon, floored at its buffer.
    # Beyond that an injection is not an intervention, it is a bailout that
    # would make any cascade disappear and teach the optimiser nothing.
    return max(2.0 * payables, buffer)


def accounted_principal(obligations: Sequence[Any]) -> float:
    """Principal the book still accounts for: paid plus outstanding.

    Restructure *parents* are skipped - a restructure replaces a parent with
    children that carry the same principal, so counting both would double it.
    Cancelled obligations are counted at what was actually paid on them, because
    a write-off genuinely removes the rest from the economy; that is a shock
    effect, and the conservation check compares two runs that share it.
    """
    from lce.domain.enums import ObligationStatus

    total = 0.0
    for obligation in obligations:
        if obligation.status is ObligationStatus.RESTRUCTURED:
            continue
        if obligation.status is ObligationStatus.CANCELLED:
            total += obligation.amount_paid
            continue
        total += obligation.amount_paid + obligation.outstanding
    return total


def check_conservation(
    before: Sequence[Any],
    after: Sequence[Any],
    interventions: Sequence[Intervention],
    *,
    tolerance: float = 1e-6,
) -> FeasibilityReport:
    """The no-money-creation invariant, checked on two realised obligation books.

    ``before`` is the book after the *un-intervened* run and ``after`` the book
    after the intervened one. Comparing two realised runs rather than a graph
    against a run is what makes the check meaningful: both share whatever the
    shock wrote off, so any difference is attributable to the actions alone.

    Injections and credit lines legitimately add resources from outside the
    network, and their cost is exactly the resource added, so they are exempt and
    accounted separately. Everything else may only reschedule principal.
    """
    report = FeasibilityReport()
    rearranging = [u for u in interventions if u.type not in CAPITAL_TYPES]
    if not rearranging:
        return report

    start = accounted_principal(before)
    end = accounted_principal(after)
    drift = abs(end - start)
    if drift > tolerance * max(1.0, start):
        report.violations.append(
            Violation(
                "no_money_creation",
                (
                    f"accounted principal moved by {drift:,.2f} under actions that "
                    "may only reschedule it"
                ),
                observed=end,
                limit=start,
            )
        )
    return report


def check_realised_floor(
    outcomes: dict[str, Any],
    baseline_outcomes: dict[str, Any],
    *,
    tolerance: float = 1e-6,
) -> FeasibilityReport:
    """Liquidity-floor constraint, measured **incrementally** against no action.

    Checked after simulation, because a node's minimum buffer over the horizon is
    an output of the run rather than something an optimiser can bound in advance.

    Incremental, because the absolute version is vacuous here. On a shocked
    network a great many merchants dip below their operating floor with no
    intervention at all - that is what the deficit term of the objective is
    there to price - so flagging every one of them would report a property of the
    shock and attribute it to whoever acted. The constraint that actually
    constrains an action is: *do not make anyone's floor breach worse than it
    would have been if you had done nothing.*

    A term extension can genuinely do that, by moving a payable into a week where
    the debtor has less cash, and that is exactly the case worth catching.
    """
    report = FeasibilityReport()
    for merchant_id, outcome in sorted(outcomes.items()):
        after = float(getattr(outcome, "min_buffer", 0.0))
        reference = baseline_outcomes.get(merchant_id)
        before = float(getattr(reference, "min_buffer", after)) if reference else after
        # Only a *worsening* below zero counts: a node already under water stays
        # the shock's doing, not the action's.
        if after < -tolerance and after < before - tolerance * max(1.0, abs(before)):
            report.violations.append(
                Violation(
                    "liquidity_floor",
                    (
                        f"{merchant_id} ends {abs(after):,.0f} below its operating "
                        f"floor, worse than the {abs(min(before, 0.0)):,.0f} it "
                        "would have been without the action"
                    ),
                    observed=after,
                    limit=before,
                )
            )
    return report
