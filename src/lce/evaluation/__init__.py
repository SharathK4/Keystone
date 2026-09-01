"""Evaluation: metrics and the end-to-end scoring harness."""

from __future__ import annotations

from lce.evaluation.harness import (
    ComparisonReport,
    GroundTruth,
    build_ground_truth,
    compare_predictors,
    compare_searches,
    evaluate_prediction,
    evaluate_search,
)
from lce.evaluation.metrics import (
    attributable_affected,
    average_precision,
    classification_metrics,
    confusion,
    intervention_metrics,
    roc_auc,
    timing_metrics,
)

__all__ = [
    "ComparisonReport",
    "GroundTruth",
    "attributable_affected",
    "average_precision",
    "build_ground_truth",
    "classification_metrics",
    "compare_predictors",
    "compare_searches",
    "confusion",
    "evaluate_prediction",
    "evaluate_search",
    "intervention_metrics",
    "roc_auc",
    "timing_metrics",
]
