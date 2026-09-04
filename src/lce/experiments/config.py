"""Experiment configuration.

One object holds every knob that can change a result: the generator config, the
simulator config, the learner, the predictors, the optimiser, the objective
weights and the base seed. It hashes to a ``config_hash``, and that hash plus the
base seed determines the entire seed bundle - so two runs with the same
``ExperimentConfig`` produce bit-identical output, and a differing result means a
differing config, never hidden state.

This is the object the requirement *"every simulation/model run records dataset
version, seed, parameters and model version"* resolves to: the config is stored
on the run row, its hash indexes it, and the dataset version is itself derived
from the generator half of the same config.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from pathlib import Path
from typing import Any

from lce.config import ObjectiveSettings, get_settings
from lce.data.generator import GeneratorConfig
from lce.domain.enums import OptimizerKind, PredictorKind
from lce.models.dependency import DependencyLearnerConfig
from lce.models.propagation import PropagationConfig
from lce.optimization.candidates import CandidateConfig
from lce.optimization.search import SearchConfig
from lce.seeds import SeedBundle, build_seed_bundle, config_hash
from lce.simulation.engine import SimulationConfig


def _objective_dict(objective: Any) -> dict[str, Any]:
    """Objective settings arrive as either a dataclass or a pydantic model."""
    if is_dataclass(objective) and not isinstance(objective, type):
        return asdict(objective)
    return dict(objective.model_dump())


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    """The complete, hashable specification of an experiment."""

    name: str = "default"
    description: str = ""
    seed: int = field(default_factory=lambda: get_settings().random_seed)

    generator: GeneratorConfig = field(default_factory=GeneratorConfig)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    learner: DependencyLearnerConfig = field(default_factory=DependencyLearnerConfig)
    propagation: PropagationConfig = field(default_factory=PropagationConfig)
    candidates: CandidateConfig = field(default_factory=CandidateConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    objective: ObjectiveSettings = field(default_factory=ObjectiveSettings)

    predictors: tuple[PredictorKind, ...] = (
        PredictorKind.LINEAR_THRESHOLD,
        PredictorKind.HAWKES_CASCADE,
    )
    optimizers: tuple[OptimizerKind, ...] = (
        OptimizerKind.TOP_EXPOSURE,
        OptimizerKind.GREEDY,
        OptimizerKind.CP_SAT_KNAPSACK,
    )
    reference_optimizer: OptimizerKind | None = OptimizerKind.EXHAUSTIVE

    n_shocks: int = 8
    shock_fraction_of_buffer: float = 2.0
    use_ground_truth_edges: bool = False
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # The generator and simulator must agree on the horizon, or the labels
        # and the obligation schedule describe different windows.
        if abs(self.generator.horizon_hours - self.simulation.horizon_hours) > 1e-6:
            object.__setattr__(
                self,
                "generator",
                replace(self.generator, horizon_hours=self.simulation.horizon_hours),
            )
        if abs(self.propagation.horizon_hours - self.simulation.horizon_hours) > 1e-6:
            object.__setattr__(
                self,
                "propagation",
                replace(self.propagation, horizon_hours=self.simulation.horizon_hours),
            )
        # The generator's own seed is slaved to the experiment seed so that a
        # single knob controls reproducibility end to end.
        if self.generator.seed != self.seed:
            object.__setattr__(self, "generator", replace(self.generator, seed=self.seed))
        if self.simulation.seed != self.seed:
            object.__setattr__(
                self, "simulation", replace(self.simulation, seed=self.seed)
            )

    # ------------------------------------------------------------------ views

    def to_dict(self) -> dict[str, Any]:
        """Fully-expanded, JSON-safe config - what gets persisted on the run."""
        return {
            "name": self.name,
            "description": self.description,
            "seed": self.seed,
            "generator": self.generator.to_dict(),
            "simulation": self.simulation.to_dict(),
            "learner": self.learner.to_dict(),
            "propagation": self.propagation.to_dict(),
            "candidates": self.candidates.to_dict(),
            "search": self.search.to_dict(),
            "objective": _objective_dict(self.objective),
            "predictors": [str(p) for p in self.predictors],
            "optimizers": [str(o) for o in self.optimizers],
            "reference_optimizer": (
                str(self.reference_optimizer) if self.reference_optimizer else None
            ),
            "n_shocks": self.n_shocks,
            "shock_fraction_of_buffer": self.shock_fraction_of_buffer,
            "use_ground_truth_edges": self.use_ground_truth_edges,
            "tags": list(self.tags),
        }

    @property
    def config_hash(self) -> str:
        """Content address of the whole experiment.

        Excludes ``name``/``description``/``tags``: renaming an experiment must
        not change its identity, or the cache and the reproducibility claim both
        break.
        """
        payload = self.to_dict()
        for key in ("name", "description", "tags"):
            payload.pop(key, None)
        return config_hash(payload)

    @property
    def dataset_version(self) -> str:
        return self.generator.dataset_version

    def seed_bundle(self) -> SeedBundle:
        return build_seed_bundle(self.seed, self.config_hash)

    # ------------------------------------------------------------ persistence

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, default=str)

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ExperimentConfig:
        """Rebuild a config from its persisted form.

        Unknown keys are ignored rather than raising, so a config written by an
        older version still loads; the ``config_hash`` will differ, which is the
        correct signal that it is not the same experiment.
        """
        def _sub(target: type, key: str) -> Any:
            raw = dict(payload.get(key, {}) or {})
            fields = getattr(target, "__dataclass_fields__", {})
            return target(**{k: v for k, v in raw.items() if k in fields})

        objective_payload = payload.get("objective", {}) or {}
        return cls(
            name=payload.get("name", "default"),
            description=payload.get("description", ""),
            seed=int(payload.get("seed", get_settings().random_seed)),
            generator=_sub(GeneratorConfig, "generator"),
            simulation=_sub(SimulationConfig, "simulation"),
            learner=_sub(DependencyLearnerConfig, "learner"),
            propagation=_sub(PropagationConfig, "propagation"),
            candidates=_sub(CandidateConfig, "candidates"),
            search=_sub(SearchConfig, "search"),
            objective=ObjectiveSettings(**objective_payload),
            predictors=tuple(
                PredictorKind(p) for p in payload.get("predictors", []) or []
            )
            or (PredictorKind.LINEAR_THRESHOLD,),
            optimizers=tuple(OptimizerKind(o) for o in payload.get("optimizers", []) or [])
            or (OptimizerKind.GREEDY,),
            reference_optimizer=(
                OptimizerKind(payload["reference_optimizer"])
                if payload.get("reference_optimizer")
                else None
            ),
            n_shocks=int(payload.get("n_shocks", 8)),
            shock_fraction_of_buffer=float(payload.get("shock_fraction_of_buffer", 2.0)),
            use_ground_truth_edges=bool(payload.get("use_ground_truth_edges", False)),
            tags=tuple(payload.get("tags", []) or []),
        )

    @classmethod
    def load(cls, path: Path) -> ExperimentConfig:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def quick_config(
    *,
    n_merchants: int = 40,
    seed: int = 20250101,
    horizon_hours: float = 168.0,
    n_shocks: int = 3,
    name: str = "quick",
) -> ExperimentConfig:
    """Small, fast experiment - used by the tests and the smoke CLI."""
    from dataclasses import replace as _replace

    return ExperimentConfig(
        name=name,
        seed=seed,
        generator=_replace(
            GeneratorConfig(),
            n_merchants=n_merchants,
            seed=seed,
            history_hours=30 * 24.0,
            horizon_hours=horizon_hours,
        ),
        simulation=_replace(SimulationConfig(), horizon_hours=horizon_hours, seed=seed),
        candidates=_replace(CandidateConfig(), top_k_nodes=4, max_candidates=16),
        search=_replace(SearchConfig(), max_actions=2),
        n_shocks=n_shocks,
    )
