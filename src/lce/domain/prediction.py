"""Model predictions about how a shock will propagate.

A predictor answers three questions about a shock ``S`` on network ``G``:

1. **Who** is exposed - a per-merchant exposure score in ``[0, 1]``.
2. **When** it hits them - an expected time-to-impact in hours.
3. **How hard** - the expected liquidity shortfall in INR.

Predictions are kept separate from simulation outcomes so the two can be scored
against each other. The simulator's own result, wrapped as a prediction with
``PredictorKind.SIMULATION_ORACLE``, is the ground truth.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, computed_field

from lce.domain.base import DomainModel, MerchantId, new_id, utcnow
from lce.domain.enums import PredictorKind


class NodeExposure(DomainModel):
    """Predicted exposure of a single merchant to a given shock."""

    merchant_id: MerchantId
    exposure_score: float = Field(
        ge=0.0, le=1.0, description="P(node becomes constrained within the horizon)."
    )
    expected_shortfall: float = Field(
        default=0.0, ge=0.0, description="Expected INR of liquidity the node will be short."
    )
    expected_hit_t: float | None = Field(
        default=None, description="Expected first-constrained time, simulation hours."
    )
    hit_t_lower: float | None = Field(default=None, description="10th-percentile hit time.")
    hit_t_upper: float | None = Field(default=None, description="90th-percentile hit time.")
    hop_distance: int | None = Field(default=None, description="Predicted cascade depth.")
    contributing_sources: list[MerchantId] = Field(
        default_factory=list, description="Upstream nodes driving this exposure, ranked."
    )

    def is_predicted_affected(self, threshold: float = 0.5) -> bool:
        return self.exposure_score >= threshold


class ModelPrediction(DomainModel):
    """A predictor's full answer for one (network, shock) pair."""

    prediction_id: str = Field(default_factory=lambda: new_id("prd"))
    run_id: str | None = None
    shock_id: str | None = None

    predictor: PredictorKind
    model_version: str = Field(
        default="v0", description="Artifact version of the predictor that produced this."
    )
    horizon_hours: float = Field(gt=0.0)

    exposures: dict[MerchantId, NodeExposure] = Field(default_factory=dict)
    threshold: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Score cut-off used to form the predicted set."
    )

    inference_ms: float | None = Field(default=None, ge=0.0)
    seed: int | None = None
    config_hash: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Pydantic reserves the `model_` prefix; `model_version` is deliberate and
    # allowed by disabling the protected-namespace check for this model only.
    model_config = DomainModel.model_config | {"protected_namespaces": ()}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def predicted_affected_ids(self) -> list[MerchantId]:
        """The predicted affected set at the configured threshold."""
        return sorted(
            m for m, e in self.exposures.items() if e.exposure_score >= self.threshold
        )

    def predicted_affected_at(self, threshold: float) -> list[MerchantId]:
        return sorted(m for m, e in self.exposures.items() if e.exposure_score >= threshold)

    def predicted_by_time(self, t: float, threshold: float | None = None) -> list[MerchantId]:
        """Predicted-affected restricted to nodes expected to be hit by ``t``."""
        cut = self.threshold if threshold is None else threshold
        return sorted(
            m
            for m, e in self.exposures.items()
            if e.exposure_score >= cut and e.expected_hit_t is not None and e.expected_hit_t <= t
        )

    def scores(self) -> dict[MerchantId, float]:
        """Raw scores for every node - the input to threshold-free metrics (PR-AUC)."""
        return {m: e.exposure_score for m, e in self.exposures.items()}

    def hit_times(self) -> dict[MerchantId, float]:
        return {
            m: e.expected_hit_t for m, e in self.exposures.items() if e.expected_hit_t is not None
        }

    def ranked(self, limit: int | None = None) -> list[NodeExposure]:
        """Exposures sorted by score descending, then by earliest predicted hit."""
        ordered = sorted(
            self.exposures.values(),
            key=lambda e: (-e.exposure_score, e.expected_hit_t or float("inf"), e.merchant_id),
        )
        return ordered[:limit] if limit else ordered

    def top_exposed(self, k: int) -> list[MerchantId]:
        return [e.merchant_id for e in self.ranked(limit=k)]
