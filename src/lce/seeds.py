"""Deterministic random-seed control.

Reproducibility rule for this project: *no component ever reads a global RNG.*
Every stochastic component receives an explicit :class:`numpy.random.Generator`
derived from a run seed, and that run seed is itself derived deterministically
from ``(RANDOM_SEED, config_hash, stream_name)``. Two runs with the same config
and the same base seed produce bit-identical output.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from dataclasses import dataclass
from typing import Any

import numpy as np

MAX_SEED = 2**32 - 1


def canonical_json(payload: Any) -> str:
    """Stable JSON encoding used for every config hash in the system."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def config_hash(payload: Any, length: int = 16) -> str:
    """Deterministic short hash of an arbitrary JSON-serialisable config."""
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return digest[:length]


def derive_seed(base_seed: int, *parts: Any) -> int:
    """Derive a reproducible child seed from a base seed and arbitrary parts.

    Uses SHA-256 rather than Python's ``hash`` because the latter is salted per
    process (PYTHONHASHSEED) and would break reproducibility across runs.
    """
    material = canonical_json([base_seed, *parts]).encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return int.from_bytes(digest[:8], "big") % (MAX_SEED + 1)


@dataclass(frozen=True, slots=True)
class SeedBundle:
    """The complete set of seeds used by one run.

    Persisted alongside every run so an experiment can be replayed exactly.
    """

    base_seed: int
    run_seed: int
    topology_seed: int
    behaviour_seed: int
    event_seed: int
    shock_seed: int
    model_seed: int

    def to_dict(self) -> dict[str, int]:
        return {
            "base_seed": self.base_seed,
            "run_seed": self.run_seed,
            "topology_seed": self.topology_seed,
            "behaviour_seed": self.behaviour_seed,
            "event_seed": self.event_seed,
            "shock_seed": self.shock_seed,
            "model_seed": self.model_seed,
        }

    def generator(self, stream: str) -> np.random.Generator:
        """A fresh independent generator for a named stream of this run."""
        return np.random.default_rng(derive_seed(self.run_seed, "stream", stream))


def build_seed_bundle(base_seed: int, cfg_hash: str) -> SeedBundle:
    """Derive the full seed bundle for a run from its base seed + config hash."""
    run_seed = derive_seed(base_seed, "run", cfg_hash)
    return SeedBundle(
        base_seed=base_seed,
        run_seed=run_seed,
        topology_seed=derive_seed(run_seed, "topology"),
        behaviour_seed=derive_seed(run_seed, "behaviour"),
        event_seed=derive_seed(run_seed, "events"),
        shock_seed=derive_seed(run_seed, "shocks"),
        model_seed=derive_seed(run_seed, "model"),
    )


def rng(seed: int) -> np.random.Generator:
    """Construct a NumPy generator from an integer seed."""
    return np.random.default_rng(seed)


def seed_everything(seed: int, *, deterministic_torch: bool = True) -> None:
    """Seed the *global* RNGs.

    Only used for library code that we do not control (notably torch layer
    initialisation). Project code should still take explicit generators.
    """
    seed = int(seed) % (MAX_SEED + 1)
    os.environ["PYTHONHASHSEED"] = str(seed)
    random.seed(seed)
    np.random.seed(seed)

    try:  # pragma: no cover - exercised only when torch is installed
        import torch
    except ImportError:
        return

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    if deterministic_torch:
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
