"""Repository base class.

Repositories own *queries*, not transactions. They receive a live
:class:`~sqlalchemy.orm.Session` and never commit; commit/rollback is the unit
of work's job (:mod:`lce.data.unit_of_work`). That keeps a service able to span
several repositories in one atomic transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, delete, func, select
from sqlalchemy import insert as sa_insert
from sqlalchemy.orm import Session

from lce.data.orm import Base


class Repository[RowT: Base]:
    """Thin, typed query helper over one ORM table."""

    model: type[RowT]

    def __init__(self, session: Session) -> None:
        self.session = session

    # --- primitives ---------------------------------------------------------

    def add(self, row: RowT) -> RowT:
        self.session.add(row)
        return row

    def add_all(self, rows: Sequence[RowT]) -> list[RowT]:
        items = list(rows)
        if items:
            self.session.add_all(items)
        return items

    def bulk_insert(self, mappings: Sequence[dict[str, Any]]) -> int:
        """Fast path for large event batches - bypasses per-row ORM overhead."""
        payload = list(mappings)
        if not payload:
            return 0
        self.session.execute(sa_insert(self.model), payload)
        return len(payload)

    def flush(self) -> None:
        self.session.flush()

    def scalars(self, stmt: Select[tuple[RowT]]) -> list[RowT]:
        return list(self.session.execute(stmt).scalars().all())

    def one_or_none(self, stmt: Select[tuple[RowT]]) -> RowT | None:
        return self.session.execute(stmt).scalars().one_or_none()

    def count(self, stmt: Select[Any] | None = None) -> int:
        base = stmt if stmt is not None else select(self.model)
        subq = base.subquery()
        return int(self.session.execute(select(func.count()).select_from(subq)).scalar_one())

    def delete_where(self, *conditions: Any) -> int:
        result = self.session.execute(delete(self.model).where(*conditions))
        return int(result.rowcount or 0)

    def all(self, limit: int | None = None, offset: int = 0) -> list[RowT]:
        stmt = select(self.model).offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return self.scalars(stmt)
