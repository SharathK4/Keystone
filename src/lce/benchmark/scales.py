"""Standard benchmark scales.

Three sizes, chosen so results are comparable across runs and machines:

``SMALL``   100 merchants   - fast enough for exhaustive intervention search,
                              which is what makes a *measured* optimality gap
                              possible rather than a comparison of heuristics.
``MEDIUM``  1,000 merchants - the default reporting scale.
``LARGE``   10,000 merchants - scaling behaviour; generated in streaming mode.

Event budget scales with the network
------------------------------------
``max_history_events`` is not a constant. A fixed cap that is generous at 100
merchants starves the dependency learner at 10,000: the same budget spread over
100x more edges leaves under one observation per edge, and every parameter it
reports becomes noise. The budget is therefore set from a target number of
*events per edge*, so statistical power per link is held roughly constant as the
network grows.

Network tightness
-----------------
Benchmark networks are deliberately tighter than the generator's default:
buffers cover only 20-45% of a horizon's payables. A benchmark exists to
exercise contagion, and on a comfortably capitalised network the cascade
stops at the first ring on most seeds - every family reports one victim and
depth zero, which is a valid economy but a useless benchmark.

History length is trimmed at LARGE because the cascading point process is
super-linear in both merchants and window length; the trade is fewer weeks of
history rather than fewer observations per edge, which is the axis that actually
matters for identifiability.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from lce.data.generator import HOURS_PER_DAY, GeneratorConfig

# Observations per directed edge the learner needs for a usable fit. Below ~20
# the marked-Hawkes EM has too little evidence to separate excitation from the
# baseline stream and reports low-confidence estimates.
TARGET_EVENTS_PER_EDGE = 45


class BenchmarkScale(StrEnum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


@dataclass(frozen=True, slots=True)
class ScaleProfile:
    """Fixed parameters for one benchmark scale."""

    scale: BenchmarkScale
    n_merchants: int
    n_layers: int
    mean_out_degree: float
    history_days: float
    horizon_hours: float
    streaming: bool
    exhaustive_optimum: bool
    coverage_low: float = 0.20
    coverage_high: float = 0.45

    @property
    def approx_edges(self) -> int:
        return max(1, int(self.n_merchants * self.mean_out_degree))

    @property
    def event_budget(self) -> int:
        return int(self.approx_edges * TARGET_EVENTS_PER_EDGE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scale": str(self.scale),
            "n_merchants": self.n_merchants,
            "n_layers": self.n_layers,
            "mean_out_degree": self.mean_out_degree,
            "history_days": self.history_days,
            "horizon_hours": self.horizon_hours,
            "streaming": self.streaming,
            "exhaustive_optimum": self.exhaustive_optimum,
            "coverage_low": self.coverage_low,
            "coverage_high": self.coverage_high,
            "event_budget": self.event_budget,
        }


SCALE_PROFILES: dict[BenchmarkScale, ScaleProfile] = {
    BenchmarkScale.SMALL: ScaleProfile(
        scale=BenchmarkScale.SMALL,
        n_merchants=100,
        n_layers=4,
        mean_out_degree=2.4,
        history_days=60.0,
        horizon_hours=168.0,
        streaming=False,
        # Small enough that brute-force search over the candidate set is
        # affordable, which is the only way to report a true optimality gap.
        exhaustive_optimum=True,
    ),
    BenchmarkScale.MEDIUM: ScaleProfile(
        scale=BenchmarkScale.MEDIUM,
        n_merchants=1_000,
        n_layers=5,
        mean_out_degree=2.6,
        history_days=45.0,
        horizon_hours=168.0,
        streaming=False,
        exhaustive_optimum=False,
    ),
    BenchmarkScale.LARGE: ScaleProfile(
        scale=BenchmarkScale.LARGE,
        n_merchants=10_000,
        n_layers=6,
        mean_out_degree=2.8,
        history_days=21.0,
        horizon_hours=168.0,
        streaming=True,
        exhaustive_optimum=False,
    ),
}


def scale_config(
    scale: BenchmarkScale | str,
    *,
    seed: int = 20250101,
    overrides: dict[str, Any] | None = None,
) -> GeneratorConfig:
    """Generator config for a named benchmark scale."""
    profile = SCALE_PROFILES[BenchmarkScale(scale)]
    config = replace(
        GeneratorConfig(),
        n_merchants=profile.n_merchants,
        n_layers=profile.n_layers,
        mean_out_degree=profile.mean_out_degree,
        history_hours=profile.history_days * HOURS_PER_DAY,
        horizon_hours=profile.horizon_hours,
        max_history_events=profile.event_budget,
        coverage_low=profile.coverage_low,
        coverage_high=profile.coverage_high,
        seed=seed,
    )
    if overrides:
        allowed = set(GeneratorConfig.__dataclass_fields__)
        unknown = set(overrides) - allowed
        if unknown:
            raise ValueError(f"unknown generator parameters: {sorted(unknown)}")
        config = replace(config, **overrides)
    return config


def profile_for(scale: BenchmarkScale | str) -> ScaleProfile:
    return SCALE_PROFILES[BenchmarkScale(scale)]


def estimate_cost(scale: BenchmarkScale | str) -> dict[str, float]:
    """Rough generation cost, so a caller can pick a scale knowingly.

    Calibrated from measured runs on a single core; the point is the *shape*
    (event generation dominates and grows faster than merchant count), not the
    absolute numbers, which vary by machine.
    """
    profile = profile_for(scale)
    events = profile.event_budget
    seconds = 0.6 + events / 45_000.0 + profile.n_merchants / 900.0
    return {
        "approx_edges": float(profile.approx_edges),
        "event_budget": float(events),
        "estimated_seconds": round(seconds, 1),
        "estimated_peak_mb": round(60.0 + events / 1400.0 + profile.n_merchants / 12.0, 1),
        "streaming_recommended": float(profile.streaming),
    }


def events_per_edge(n_events: int, n_edges: int) -> float:
    """Observations per directed edge - the learner's statistical power."""
    return n_events / max(1, n_edges)


def sufficient_power(n_events: int, n_edges: int, threshold: int = 20) -> bool:
    """Whether a dataset carries enough per-edge evidence to fit dependencies."""
    return events_per_edge(n_events, n_edges) >= threshold


def recommended_shock_count(scale: BenchmarkScale | str) -> int:
    """How many scenarios to sample so results are not a single-draw accident."""
    return {
        BenchmarkScale.SMALL: 20,
        BenchmarkScale.MEDIUM: 12,
        BenchmarkScale.LARGE: 6,
    }[BenchmarkScale(scale)]
