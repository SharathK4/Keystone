"""Engine and session management.

The engine is created lazily and cached per-process. SQLite URLs are supported
so the test suite can run against an in-memory database with no external
service; PostgreSQL-specific pool options are applied only when the URL is
actually PostgreSQL.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from lce.config import Settings, get_settings, redact_dsn
from lce.data.orm import Base
from lce.logging import get_logger

logger = get_logger(__name__)


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def build_engine(settings: Settings | None = None, url: str | None = None) -> Engine:
    """Construct a SQLAlchemy engine for ``url`` (or the configured database)."""
    cfg = settings or get_settings()
    dsn = url or cfg.database_url

    if _is_sqlite(dsn):
        # A shared in-memory database needs StaticPool, or every connection
        # would see its own empty database.
        connect_args = {"check_same_thread": False}
        kwargs: dict[str, object] = {"connect_args": connect_args}
        if ":memory:" in dsn or "mode=memory" in dsn:
            kwargs["poolclass"] = StaticPool
        engine = create_engine(dsn, echo=cfg.db_echo, future=True, **kwargs)

        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_conn, _record):  # pragma: no cover - trivial
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    engine = create_engine(
        dsn,
        echo=cfg.db_echo,
        future=True,
        pool_pre_ping=True,
        pool_size=cfg.db_pool_size,
        max_overflow=cfg.db_max_overflow,
    )
    logger.debug("engine_created", url=redact_dsn(dsn))
    return engine


@functools.lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Process-wide engine for the configured database."""
    return build_engine()


@functools.lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


def reset_engine_cache() -> None:
    """Drop cached engine/session factory (tests, or after a config change)."""
    try:
        get_engine().dispose()
    except Exception:  # pragma: no cover - best effort during teardown
        pass
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@contextmanager
def session_scope(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    maker = factory or get_session_factory()
    session = maker()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine: Engine | None = None) -> None:
    """Create every table.

    Used by the tests and by ``lce db init``. Production schema changes go
    through Alembic - see ``migrations/``.
    """
    Base.metadata.create_all(engine or get_engine())


def drop_all(engine: Engine | None = None) -> None:
    Base.metadata.drop_all(engine or get_engine())


def healthcheck(engine: Engine | None = None) -> dict[str, object]:
    """Ping the database. Never raises - returns a status payload instead."""
    target = engine or get_engine()
    try:
        with target.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "dialect": target.dialect.name}
    except Exception as exc:  # pragma: no cover - depends on live DB
        logger.warning("database_healthcheck_failed", error=str(exc))
        return {"status": "error", "dialect": target.dialect.name, "error": str(exc)}
