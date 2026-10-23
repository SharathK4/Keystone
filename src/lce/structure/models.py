"""Contract models for the value-of-speed artifact.

Same rules as :mod:`lce.snapshot.models`: immutable, ``extra="forbid"``, and an
unknown is ``null`` rather than a guess. Rupee amounts are analytic rupees from
the synthetic network; the disruption index is an index and never carries a
currency sign.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

LABEL = "Replayed offline, inside the synthetic generator"
DELAY_NOTE = (
    "Delays are scenario parameters: the hours between the moment the shock is "
    "known and the moment the money lands. They are not production timings."
)


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())


class ObjectiveTerms(_Contract):
    """The three terms of the disruption index. ``total`` is their sum."""

    delay: float
    default: float
    deficit: float
    total: float


class SpeedAction(_Contract):
    action_type: str
    merchant_id: str
    lands_at_hours: float
    amount_inr: float
    shift_hours: float
    cost_inr: float


class ArmResult(_Contract):
    """One plan, replayed with the shock, compared against no plan at all."""

    actions: list[SpeedAction]
    cost_inr: float
    disruption_index: float
    disruption_cut_pct: float
    payments_kept_on_time_inr: float
    merchants_protected: int
    defaults_avoided: int
    reduction: ObjectiveTerms


class DelayResult(_Contract):
    delay_hours: float
    frozen: ArmResult
    replanned: ArmResult


class Monotonicity(_Contract):
    """Reported, never assumed: does the cut only fall as the delay grows?"""

    frozen_non_increasing: bool
    replanned_non_increasing: bool
    rises: list[str]


class ScenarioSpeed(_Contract):
    scenario_id: str
    family: str
    seed: int
    dataset_version: str
    shock_onset_hours: float
    in_snapshot: bool
    has_plan: bool
    unplanned: ObjectiveTerms
    payments_late_inr: float
    merchants_affected: int
    defaults: int
    delays: list[DelayResult]
    monotonicity: Monotonicity


class Interval(_Contract):
    """Mean over scenarios, with a percentile bootstrap interval over scenarios."""

    mean: float
    low: float
    high: float


class SpeedPoint(_Contract):
    delay_hours: float
    n_scenarios: int
    frozen_cut_pct: Interval
    replanned_cut_pct: Interval
    replanning_gain_pp: Interval
    frozen_kept_inr: Interval
    replanned_kept_inr: Interval
    replanned_mix: dict[str, int]
    replanned_cost_inr: Interval


class BootstrapSpec(_Contract):
    resamples: int
    seed: int
    level: float
    unit: Literal["scenario"]


class SpeedProvenance(_Contract):
    kind: Literal["replayed_offline"]
    label: str
    delay_note: str
    replay_note: str
    snapshot_id: str
    seeds: list[int]
    scale: str
    dataset_versions: list[str]
    delays_hours: list[float]
    optimiser: str
    objective: dict[str, float]
    replay_config_hash: str
    budget: dict[str, object]
    bootstrap: BootstrapSpec
    code_version: str


class SpeedScenarioRef(_Contract):
    scenario_id: str
    family: str
    seed: int
    in_snapshot: bool
    has_plan: bool


class SpeedSummary(_Contract):
    """The headline: value of speed aggregated over every scenario with a plan."""

    artifact_id: str
    content_hash: str
    label: str
    provenance: SpeedProvenance
    n_scenarios: int
    n_with_plan: int
    points: list[SpeedPoint]
    monotone_scenarios: int
    scenarios: list[SpeedScenarioRef]


class SpeedScenarioView(_Contract):
    artifact_id: str
    content_hash: str
    label: str
    scenario: ScenarioSpeed


# ------------------------------------------------------------- pulled stones


class PullAffected(_Contract):
    merchant_id: str
    cascade_depth: int | None
    hours_to_constraint: float | None
    disrupted_value_inr: float


class PullRange(_Contract):
    """What each figure moves over when the shock is 1.5x or 2.5x instead of 2x."""

    multiples: list[float]
    n_affected: list[int]
    first_constraint_hours: list[float] | None
    disrupted_value_inr: list[float]


class PullPlan(_Contract):
    action_type: str
    merchant_id: str
    amount_inr: float
    disruption_cut_pct: float
    payments_kept_on_time_inr: float


class PullRecord(_Contract):
    merchant_id: str
    tier: str
    sector: str
    magnitude_multiple: float
    magnitude_inr: float
    onset_hours: float
    n_affected: int
    n_defaulted: int
    max_depth: int
    disrupted_value_inr: float
    first_constraint_hours: float | None
    affected: list[PullAffected]
    range: PullRange
    plan: PullPlan | None


class PullsProvenance(_Contract):
    kind: Literal["replayed_offline"]
    label: str
    snapshot_id: str
    shock: str
    multiples: list[float]
    central_multiple: float
    limits: dict[str, int]
    code_version: str


class PullsView(_Contract):
    artifact_id: str
    content_hash: str
    label: str
    provenance: PullsProvenance
    records: list[PullRecord]


# --------------------------------------------------------------------- proof


class LeakageRow(_Contract):
    """The barrier for one scenario: history used to estimate ends before the shock."""

    scenario_id: str
    family: str
    onset_hours: float
    first_observed_hours: float | None
    last_observed_hours: float | None
    n_observed_events: int
    holds: bool


class Rate(_Contract):
    value: float
    low: float
    high: float
    numerator: int
    denominator: int


class ParameterRecovery(_Contract):
    """How well one estimated strength tracks the generator's true value, link by link."""

    name: str
    label: str
    n_edges: int
    rank_correlation: float
    low: float
    high: float
    mean_abs_error: float
    unit: str


class RecoveryView(_Contract):
    """Which links exist is read from observed payments; their strengths are estimated."""

    n_true: int
    n_estimated: int
    n_matched: int
    precision: Rate
    recall: Rate
    parameters: list[ParameterRecovery]


class AuditLine(_Contract):
    name: str
    passed: bool


class MeasuredResult(_Contract):
    label: str
    value: float
    low: float | None
    high: float | None
    unit: str
    note: str


class FailedItem(_Contract):
    title: str
    detail: str


class ProofProvenance(_Contract):
    kind: Literal["replayed_offline"]
    label: str
    snapshot_id: str
    speed_artifact_id: str
    code_version: str


class ProofView(_Contract):
    artifact_id: str
    content_hash: str
    label: str
    provenance: ProofProvenance
    leakage: list[LeakageRow]
    recovery: RecoveryView
    audit: list[AuditLine]
    results: list[MeasuredResult]
    failed: list[FailedItem]
