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
