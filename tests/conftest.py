"""Shared fixtures.

The suite runs against an on-disk SQLite database in a temp directory rather
than PostgreSQL, so it needs no external service. The ORM is written to be
dialect-portable for exactly this reason; the ``requires_postgres`` marker is
reserved for anything that genuinely needs server-side behaviour.

Networks are expensive to generate (the history is a cascading point process),
so the shared ones are session-scoped and treated as read-only. Tests that
mutate a graph take a ``copy()``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

# Environment must be set before lce.config is first imported, since settings
# are cached on a module-level singleton.
_TEST_DB = Path(os.environ.get("PYTEST_TMP_DB", "")) if os.environ.get("PYTEST_TMP_DB") else None


@pytest.fixture(scope="session", autouse=True)
def _test_environment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Point the app at a throwaway database and artifact directory."""
    root = tmp_path_factory.mktemp("lce")
    db_path = root / "test.sqlite"
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    previous = {
        key: os.environ.get(key)
        for key in (
            "DATABASE_URL",
            "MODEL_ARTIFACT_DIR",
            "LCE_ENV",
            "LCE_LOG_LEVEL",
            "RANDOM_SEED",
            "RAZORPAY_MODE",
        )
    }
    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{db_path.as_posix()}"
    os.environ["MODEL_ARTIFACT_DIR"] = str(artifacts)
    os.environ["LCE_ENV"] = "test"
    os.environ["LCE_LOG_LEVEL"] = "WARNING"
    os.environ["RANDOM_SEED"] = "1234"
    os.environ["RAZORPAY_MODE"] = "test"

    from lce.config import reset_settings_cache
    from lce.data.database import create_all, reset_engine_cache

    reset_settings_cache()
    reset_engine_cache()
    create_all()

    yield

    reset_engine_cache()
    reset_settings_cache()
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def uow() -> Iterator["object"]:
    """A unit of work on a clean transaction, rolled back after the test."""
    from lce.data.unit_of_work import UnitOfWork

    with UnitOfWork() as unit:
        yield unit
        unit.rollback()


@pytest.fixture(scope="session")
def small_network():
    """A tiny generated network. Session-scoped: treat as read-only."""
    from lce.data.generator import GeneratorConfig, generate_network

    return generate_network(
        replace(
            GeneratorConfig(),
            n_merchants=24,
            n_layers=3,
            seed=99,
            history_hours=20 * 24.0,
            horizon_hours=168.0,
        )
    )


@pytest.fixture(scope="session")
def medium_network():
    """A network large enough to produce a multi-hop cascade."""
    from lce.data.generator import GeneratorConfig, generate_network

    return generate_network(
        replace(
            GeneratorConfig(),
            n_merchants=45,
            seed=7,
            history_hours=30 * 24.0,
            horizon_hours=168.0,
            coverage_low=0.25,
            coverage_high=0.55,
        )
    )


@pytest.fixture
def graph(small_network):
    """A mutable copy of the small network's graph."""
    return small_network.graph.copy()


@pytest.fixture(scope="session")
def sim_config():
    from lce.simulation.engine import SimulationConfig

    return SimulationConfig(horizon_hours=168.0, tick_hours=1.0, seed=99)


@pytest.fixture
def api_client() -> Iterator["object"]:
    """FastAPI test client sharing the test database."""
    from fastapi.testclient import TestClient

    from lce.api.app import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture(autouse=True)
def _clear_graph_cache() -> Iterator[None]:
    """Stop the service-layer graph cache leaking between tests."""
    from lce.services.network_service import NetworkService

    NetworkService.invalidate_cache()
    yield
    NetworkService.invalidate_cache()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: long-running tests")
    config.addinivalue_line("markers", "requires_torch: needs the optional ml extra")
    config.addinivalue_line("markers", "requires_ortools: needs the optional opt extra")
    config.addinivalue_line("markers", "requires_postgres: needs a live PostgreSQL")


def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


HAS_TORCH = _module_available("torch") and _module_available("torch_geometric")
HAS_ORTOOLS = _module_available("ortools")
