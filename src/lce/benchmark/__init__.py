"""Benchmark layer: scales, scenarios, ground truth, validation and export.

The contract this package enforces is that a model sees the **observable**
network - merchants, payments, obligations - and never the latent parameters
that generated it. :class:`~lce.benchmark.ground_truth.ScenarioGroundTruth`
holds the hidden variables, and its ``observable_graph()`` is the only view
intended for a learner.
"""

from __future__ import annotations

from lce.benchmark.export import (
    ExportResult,
    export_dataset,
    export_scenario,
    list_scenarios,
    load_dataset,
    load_scenario_ground_truth,
    replay_scenario,
    stream_payments_to_parquet,
)
from lce.benchmark.ground_truth import (
    ScenarioGroundTruth,
    TrueLiquidityState,
    TrueOptimum,
    compute_ground_truth,
)
from lce.benchmark.manifest import DatasetManifest, make_scenario_id
from lce.benchmark.scales import (
    SCALE_PROFILES,
    BenchmarkScale,
    ScaleProfile,
    estimate_cost,
    events_per_edge,
    profile_for,
    recommended_shock_count,
    scale_config,
    sufficient_power,
)
from lce.benchmark.scenarios import (
    BuiltScenario,
    ObligationMutation,
    ScenarioFamily,
    ScenarioSpec,
    TargetStrategy,
    build_scenario,
    scenario_suite,
    select_targets,
)
from lce.benchmark.validation import (
    Check,
    ValidationReport,
    compute_diagnostics,
    validate_dataset,
)

__all__ = [
    "SCALE_PROFILES",
    "BenchmarkScale",
    "BuiltScenario",
    "Check",
    "DatasetManifest",
    "ExportResult",
    "ObligationMutation",
    "ScaleProfile",
    "ScenarioFamily",
    "ScenarioGroundTruth",
    "ScenarioSpec",
    "TargetStrategy",
    "TrueLiquidityState",
    "TrueOptimum",
    "ValidationReport",
    "build_scenario",
    "compute_diagnostics",
    "compute_ground_truth",
    "estimate_cost",
    "events_per_edge",
    "export_dataset",
    "export_scenario",
    "list_scenarios",
    "load_dataset",
    "load_scenario_ground_truth",
    "make_scenario_id",
    "profile_for",
    "recommended_shock_count",
    "replay_scenario",
    "scale_config",
    "scenario_suite",
    "select_targets",
    "stream_payments_to_parquet",
    "sufficient_power",
    "validate_dataset",
]
