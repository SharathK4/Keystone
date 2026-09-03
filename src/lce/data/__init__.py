"""Persistence layer: ORM schema, mappers, repositories, unit of work.

Imports are lazy
----------------
Public names resolve through :func:`__getattr__` rather than being imported when
the package loads. The reason is the same one that made
:mod:`lce.learning` lazy: ``generator`` is a *build-time* dependency, and a
process that only serves precomputed analytics must be able to reach the
database helpers without dragging the dataset generator into memory behind them.
Re-exporting the generator eagerly here made every importer of any
``lce.data.*`` module a transitive importer of it, which is how it reached the
API process. ``src/lce/scripts/audit_backend.py`` fails the build if it gets
back in.
"""

from __future__ import annotations

from typing import Any

#: Public name -> module it lives in. The single source of truth for both
#: ``__all__`` and the lazy resolver, so the two cannot drift apart.
_EXPORTS: dict[str, str] = {
    # database
    "build_engine": "database",
    "create_all": "database",
    "drop_all": "database",
    "get_engine": "database",
    "get_session_factory": "database",
    "healthcheck": "database",
    "reset_engine_cache": "database",
    "session_scope": "database",
    # generator (build-time only)
    "GeneratorConfig": "generator",
    "NetworkGenerator": "generator",
    "SyntheticNetwork": "generator",
    "generate_network": "generator",
    # orm
    "Base": "orm",
    "from_minor": "orm",
    "to_minor": "orm",
    # unit of work
    "UnitOfWork": "unit_of_work",
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
