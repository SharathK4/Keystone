"""Reproducible experiment configuration and run tracking.

Imports are lazy for the same reason :mod:`lce.data` and :mod:`lce.learning`
are: ``config`` and ``runner`` reach the dataset generator, while ``tracker`` is
plain run bookkeeping that the serving path legitimately uses. Eagerly importing
all three made every user of the tracker a transitive importer of the generator.
"""

from __future__ import annotations

from typing import Any

#: Public name -> module it lives in. The single source of truth for both
#: ``__all__`` and the lazy resolver, so the two cannot drift apart.
_EXPORTS: dict[str, str] = {
    "ExperimentConfig": "config",
    "quick_config": "config",
    "ExperimentReport": "runner",
    "ExperimentRunner": "runner",
    "RunRecord": "tracker",
    "RunTracker": "tracker",
    "git_sha": "tracker",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Resolve a public name to its module on first access."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f"{__name__}.{module_name}")
    value = getattr(module, name)
    globals()[name] = value  # cache, so repeated access is a plain lookup
    return value


def __dir__() -> list[str]:
    return __all__
