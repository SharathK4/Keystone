"""Evaluation results.

Two things get scored in this system, and they are scored differently:

**Contagion prediction** - set prediction against the simulator's ground-truth
affected set :math:`\\mathcal{A}(G,S)`: precision, recall, F1, PR-AUC on the raw
exposure scores, plus MAE on the predicted time-to-impact over the nodes both
sets agree are affected.

**Intervention search** - the optimality gap against the true optimum
:math:`U^*`, obtained by exhaustive counterfactual search on small instances:

.. math::

    \\mathrm{gap}(U) =
    \\frac{D(U) - D(U^*)}{D(\\emptyset) - D(U^*)} \\in [0, 1]

0 means the algorithm found an optimal plan; 1 means it prevented nothing.
The denominator is the *achievable* improvement, which keeps the gap
comparable across scenarios of very different severity.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, computed_field

from lce.domain.base import DomainModel, new_id, utcnow


class ClassificationMetrics(DomainModel):
    """Set-prediction quality for the affected set."""

    true_positives: int = Field(default=0, ge=0)
    false_positives: int = Field(default=0, ge=0)
    false_negatives: int = Field(default=0, ge=0)
    true_negatives: int = Field(default=0, ge=0)

    pr_auc: float | None = Field(default=None, description="Average precision over score sweep.")
    roc_auc: float | None = None
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) > 0 else 0.0

    @property
    def support(self) -> int:
        return self.true_positives + self.false_negatives

    @property
    def n_scored(self) -> int:
        return (
            self.true_positives
            + self.false_positives
            + self.false_negatives
            + self.true_negatives
        )


class TimingMetrics(DomainModel):
    """Accuracy of the predicted time-to-impact, in hours."""

    mae_hours: float | None = Field(default=None, ge=0.0)
    rmse_hours: float | None = Field(default=None, ge=0.0)
    median_abs_error_hours: float | None = Field(default=None, ge=0.0)
    bias_hours: float | None = Field(
        default=None, description="Mean signed error; positive = predicted too late."
    )
    n_compared: int = Field(default=0, ge=0)
    within_6h: float | None = Field(default=None, ge=0.0, le=1.0)
    within_24h: float | None = Field(default=None, ge=0.0, le=1.0)


class InterventionMetrics(DomainModel):
    """Quality of a chosen intervention plan against the achievable optimum."""

    baseline_disruption: float
    achieved_disruption: float
    optimal_disruption: float | None = Field(
        default=None, description="D(U*) from exhaustive search. None when not computed."
    )
    cost: float = Field(default=0.0, ge=0.0)
    optimal_cost: float | None = None
    n_actions: int = Field(default=0, ge=0)
    search_ms: float | None = Field(default=None, ge=0.0)
    candidates_considered: int = Field(default=0, ge=0)
    simulations_run: int = Field(default=0, ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def disruption_prevented(self) -> float:
        return self.baseline_disruption - self.achieved_disruption

    @computed_field  # type: ignore[prop-decorator]
    @property
    def disruption_prevented_per_rupee(self) -> float:
        if self.cost <= 0:
            return float("inf") if self.disruption_prevented > 0 else 0.0
        return self.disruption_prevented / self.cost

    @computed_field  # type: ignore[prop-decorator]
    @property
    def optimality_gap(self) -> float | None:
        """(D(U) - D(U*)) / (D({}) - D(U*)); 0 is optimal, 1 prevents nothing."""
        if self.optimal_disruption is None:
            return None
        achievable = self.baseline_disruption - self.optimal_disruption
        if achievable <= 1e-9:
            # Nothing was preventable; any plan is trivially optimal.
            return 0.0
        return max(0.0, (self.achieved_disruption - self.optimal_disruption) / achievable)

    @property
    def is_optimal(self) -> bool:
        gap = self.optimality_gap
        return gap is not None and gap <= 1e-9


class EvaluationResult(DomainModel):
    """One scored comparison, persisted for the experiment record."""

    evaluation_id: str = Field(default_factory=lambda: new_id("evl"))
    run_id: str | None = None
    prediction_id: str | None = None
    plan_id: str | None = None
    shock_id: str | None = None

    name: str = ""
    predictor: str | None = None
    optimizer: str | None = None
    model_version: str | None = None
    dataset_version: str | None = None
    horizon_hours: float | None = None

    classification: ClassificationMetrics | None = None
    timing: TimingMetrics | None = None
    intervention: InterventionMetrics | None = None

    # Metrics sliced by prediction horizon (the 6h / 24h / 48h / 72h view).
    by_horizon: dict[str, ClassificationMetrics] = Field(default_factory=dict)

    seed: int | None = None
    config_hash: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    notes: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = DomainModel.model_config | {"protected_namespaces": ()}

    def headline(self) -> dict[str, Any]:
        """The numbers worth putting on a slide - and nothing that was not measured."""
        out: dict[str, Any] = {"evaluation_id": self.evaluation_id, "name": self.name}
        if self.classification is not None:
            out |= {
                "precision": round(self.classification.precision, 4),
                "recall": round(self.classification.recall, 4),
                "f1": round(self.classification.f1, 4),
                "pr_auc": self.classification.pr_auc,
                "support": self.classification.support,
            }
        if self.timing is not None and self.timing.mae_hours is not None:
            out |= {
                "hit_time_mae_hours": round(self.timing.mae_hours, 3),
                "n_timed": self.timing.n_compared,
            }
        if self.intervention is not None:
            out |= {
                "disruption_prevented": self.intervention.disruption_prevented,
                "cost": self.intervention.cost,
                "dpr": self.intervention.disruption_prevented_per_rupee,
                "optimality_gap": self.intervention.optimality_gap,
            }
        return out
