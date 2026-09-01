"""Candidate intervention generation.

The search space is combinatorial and the objective is a simulation, so the
candidate set decides how expensive the whole search is. It is built by pointing
the four intervention types at the nodes a *predictor* says are exposed, rather
than at all nodes: that is what keeps the search tractable, and it is also the
honest coupling between the two halves of the system - the optimiser is only as
good as the contagion model that feeds it, and the evaluation should reflect
that.

Sizing is relative, not absolute. A liquidity injection is sized as a multiple
of the node's *predicted shortfall* (falling back to its buffer), so the same
generator works on a micro merchant and on an anchor without a hand-tuned rupee
grid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lce.domain.enums import InterventionType
from lce.domain.events import EXTERNAL_SINK
from lce.domain.intervention import Intervention
from lce.domain.prediction import ModelPrediction
from lce.domain.shock import Shock
from lce.graph.temporal_graph import TemporalPaymentGraph

HOURS_PER_DAY = 24.0


@dataclass(frozen=True, slots=True)
class CandidateConfig:
    """Controls the size and shape of the search space."""

    top_k_nodes: int = 8
    injection_multiples: tuple[float, ...] = (0.6, 1.0, 1.5)
    credit_multiples: tuple[float, ...] = (1.0,)
    acceleration_shift_hours: tuple[float, ...] = (2 * HOURS_PER_DAY, 4 * HOURS_PER_DAY)
    extension_shift_hours: tuple[float, ...] = (2 * HOURS_PER_DAY, 5 * HOURS_PER_DAY)
    restructure_tranches: tuple[int, ...] = (3,)
    include_types: tuple[InterventionType, ...] = (
        InterventionType.LIQUIDITY_INJECTION,
        InterventionType.CREDIT_LINE_INCREASE,
        InterventionType.RECEIVABLE_ACCELERATION,
        InterventionType.SUPPLIER_TERM_EXTENSION,
        InterventionType.REPAYMENT_RESTRUCTURE,
    )
    min_amount: float = 1000.0
    max_candidates: int = 120
    apply_at_shock_onset: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "top_k_nodes": self.top_k_nodes,
            "injection_multiples": list(self.injection_multiples),
            "credit_multiples": list(self.credit_multiples),
            "acceleration_shift_hours": list(self.acceleration_shift_hours),
            "extension_shift_hours": list(self.extension_shift_hours),
            "restructure_tranches": list(self.restructure_tranches),
            "include_types": [str(t) for t in self.include_types],
            "min_amount": self.min_amount,
            "max_candidates": self.max_candidates,
            "apply_at_shock_onset": self.apply_at_shock_onset,
        }


@dataclass(slots=True)
class CandidateSet:
    """Generated candidates plus the reasoning that produced them."""

    interventions: list[Intervention] = field(default_factory=list)
    targeted_nodes: list[str] = field(default_factory=list)
    config: CandidateConfig = field(default_factory=CandidateConfig)

    def __len__(self) -> int:
        return len(self.interventions)

    def by_type(self, kind: InterventionType) -> list[Intervention]:
        return [u for u in self.interventions if u.type is kind]

    def affordable(self, budget: float) -> list[Intervention]:
        return [u for u in self.interventions if u.cost <= budget]


def generate_candidates(
    graph: TemporalPaymentGraph,
    shock: Shock,
    prediction: ModelPrediction,
    config: CandidateConfig | None = None,
    *,
    horizon_hours: float = 168.0,
) -> CandidateSet:
    """Build the candidate action set for a (network, shock, prediction) triple."""
    cfg = config or CandidateConfig()
    t0 = shock.onset_t if cfg.apply_at_shock_onset else 0.0

    ranked = [
        exposure
        for exposure in prediction.ranked()
        if exposure.exposure_score > 0.0 and graph.has_merchant(exposure.merchant_id)
    ][: cfg.top_k_nodes]

    # If the predictor flagged nothing, fall back to the shock origins so the
    # optimiser still has something to work with rather than returning empty.
    if not ranked:
        targets = [m for m in shock.origin_ids if graph.has_merchant(m)]
        shortfalls = {m: graph.merchant(m).initial_buffer for m in targets}
    else:
        targets = [e.merchant_id for e in ranked]
        shortfalls = {
            e.merchant_id: (
                e.expected_shortfall
                if e.expected_shortfall > 0
                else graph.merchant(e.merchant_id).initial_buffer * 0.5
            )
            for e in ranked
        }

    interventions: list[Intervention] = []
    for merchant_id in targets:
        base = max(shortfalls.get(merchant_id, 0.0), cfg.min_amount)
        interventions.extend(
            _cash_candidates(merchant_id, base, t0, cfg)
        )
        interventions.extend(
            _obligation_candidates(graph, merchant_id, t0, cfg, horizon_hours)
        )

    # Deterministic order, then cap. Sorting by cost keeps the cheapest options
    # in the set when the cap bites, which is what a budgeted search wants.
    interventions.sort(key=lambda u: (u.cost, u.merchant_id, str(u.type)))
    return CandidateSet(
        interventions=interventions[: cfg.max_candidates],
        targeted_nodes=targets,
        config=cfg,
    )


def _cash_candidates(
    merchant_id: str, base: float, t0: float, cfg: CandidateConfig
) -> list[Intervention]:
    out: list[Intervention] = []
    if InterventionType.LIQUIDITY_INJECTION in cfg.include_types:
        for multiple in cfg.injection_multiples:
            amount = base * multiple
            if amount >= cfg.min_amount:
                out.append(
                    Intervention(
                        type=InterventionType.LIQUIDITY_INJECTION,
                        merchant_id=merchant_id,
                        t=t0,
                        amount=amount,
                        label=f"Inject {multiple:g}x shortfall into {merchant_id}",
                    )
                )
    if InterventionType.CREDIT_LINE_INCREASE in cfg.include_types:
        for multiple in cfg.credit_multiples:
            amount = base * multiple
            if amount >= cfg.min_amount:
                out.append(
                    Intervention(
                        type=InterventionType.CREDIT_LINE_INCREASE,
                        merchant_id=merchant_id,
                        t=t0,
                        amount=amount,
                        label=f"Raise {merchant_id} credit line by {multiple:g}x shortfall",
                    )
                )
    return out


def _obligation_candidates(
    graph: TemporalPaymentGraph,
    merchant_id: str,
    t0: float,
    cfg: CandidateConfig,
    horizon: float,
) -> list[Intervention]:
    """Term-structure interventions, aimed at the node's largest live items."""
    out: list[Intervention] = []

    # Accelerating a receivable only helps if it was going to arrive *later*.
    receivables = [
        o
        for o in graph.receivables_of(merchant_id)
        if o.is_open and t0 < o.due_t <= horizon and o.creditor_id != EXTERNAL_SINK
    ]
    if receivables and InterventionType.RECEIVABLE_ACCELERATION in cfg.include_types:
        target = max(receivables, key=lambda o: o.outstanding)
        for shift in cfg.acceleration_shift_hours:
            # Never "accelerate" past the point the shock is applied.
            effective = min(shift, max(0.0, target.due_t - t0))
            if effective <= 0:
                continue
            out.append(
                Intervention(
                    type=InterventionType.RECEIVABLE_ACCELERATION,
                    merchant_id=merchant_id,
                    t=t0,
                    amount=target.outstanding,
                    shift_hours=effective,
                    target_obligation_id=target.obligation_id,
                    label=(
                        f"Pull forward {merchant_id}'s receivable by "
                        f"{effective / HOURS_PER_DAY:.1f}d"
                    ),
                )
            )

    payables = [
        o for o in graph.payables_of(merchant_id) if o.is_open and o.due_t <= horizon
    ]
    if payables:
        target = max(payables, key=lambda o: o.outstanding)
        if InterventionType.SUPPLIER_TERM_EXTENSION in cfg.include_types:
            for shift in cfg.extension_shift_hours:
                out.append(
                    Intervention(
                        type=InterventionType.SUPPLIER_TERM_EXTENSION,
                        merchant_id=merchant_id,
                        t=t0,
                        amount=target.outstanding,
                        shift_hours=shift,
                        target_obligation_id=target.obligation_id,
                        label=(
                            f"Extend {merchant_id}'s payable by "
                            f"{shift / HOURS_PER_DAY:.1f}d"
                        ),
                    )
                )
        if InterventionType.REPAYMENT_RESTRUCTURE in cfg.include_types:
            for tranches in cfg.restructure_tranches:
                out.append(
                    Intervention(
                        type=InterventionType.REPAYMENT_RESTRUCTURE,
                        merchant_id=merchant_id,
                        t=t0,
                        amount=target.outstanding,
                        tranches=tranches,
                        target_obligation_id=target.obligation_id,
                        label=(
                            f"Restructure {merchant_id}'s payable into {tranches} tranches"
                        ),
                    )
                )
    return out
