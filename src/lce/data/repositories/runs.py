"""Repositories for scenarios, runs and cascade results."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert as sa_insert
from sqlalchemy import select

from lce.data import mappers
from lce.data.orm import (
    InterventionPlanRow,
    NodeOutcomeRow,
    PropagationEventRow,
    RunRow,
    ShockRow,
    to_minor,
)
from lce.data.repositories.base import Repository
from lce.domain.enums import RunKind, RunStatus
from lce.domain.intervention import InterventionPlan
from lce.domain.propagation import CascadeResult, NodeOutcome, PropagationEvent
from lce.domain.shock import Shock
from lce.errors import NotFoundError


class ShockRepository(Repository[ShockRow]):
    model = ShockRow

    def save(self, shock: Shock, dataset_id: str) -> ShockRow:
        return self.add(mappers.shock_to_row(shock, dataset_id))

    def get(self, shock_id: str) -> Shock | None:
        row = self.one_or_none(select(ShockRow).where(ShockRow.shock_id == shock_id))
        return mappers.shock_from_row(row) if row else None

    def require(self, shock_id: str) -> Shock:
        shock = self.get(shock_id)
        if shock is None:
            raise NotFoundError(f"unknown shock {shock_id!r}", shock_id=shock_id)
        return shock

    def list_for_dataset(self, dataset_id: str, limit: int = 100) -> list[Shock]:
        stmt = (
            select(ShockRow)
            .where(ShockRow.dataset_id == dataset_id)
            .order_by(ShockRow.created_at.desc())
            .limit(limit)
        )
        return [mappers.shock_from_row(r) for r in self.scalars(stmt)]


class InterventionPlanRepository(Repository[InterventionPlanRow]):
    model = InterventionPlanRow

    def save(
        self,
        plan: InterventionPlan,
        dataset_id: str,
        *,
        shock_id: str | None = None,
        run_id: str | None = None,
    ) -> InterventionPlanRow:
        return self.add(mappers.plan_to_row(plan, dataset_id, shock_id, run_id))

    def get(self, plan_id: str) -> InterventionPlan | None:
        row = self.one_or_none(
            select(InterventionPlanRow).where(InterventionPlanRow.plan_id == plan_id)
        )
        return mappers.plan_from_row(row) if row else None

    def require(self, plan_id: str) -> InterventionPlan:
        plan = self.get(plan_id)
        if plan is None:
            raise NotFoundError(f"unknown plan {plan_id!r}", plan_id=plan_id)
        return plan

    def list_for_shock(self, shock_id: str, limit: int = 50) -> list[InterventionPlan]:
        stmt = (
            select(InterventionPlanRow)
            .where(InterventionPlanRow.shock_id == shock_id)
            .order_by(InterventionPlanRow.created_at.desc())
            .limit(limit)
        )
        return [mappers.plan_from_row(r) for r in self.scalars(stmt)]


class RunRepository(Repository[RunRow]):
    """The reproducibility ledger.

    A run row is written *before* the work starts (status ``running``) and
    completed afterwards, so a crashed run leaves evidence rather than vanishing.
    """

    model = RunRow

    def start(
        self,
        run_id: str,
        kind: RunKind,
        *,
        name: str = "",
        experiment_id: str | None = None,
        parent_run_id: str | None = None,
        dataset_version: str | None = None,
        model_version: str | None = None,
        shock_id: str | None = None,
        plan_id: str | None = None,
        seed: int = 0,
        config: dict[str, Any] | None = None,
        config_hash: str | None = None,
        seeds: dict[str, Any] | None = None,
        git_sha: str | None = None,
        code_version: str | None = None,
    ) -> RunRow:
        return self.add(
            RunRow(
                run_id=run_id,
                kind=str(kind),
                status=str(RunStatus.RUNNING),
                name=name,
                experiment_id=experiment_id,
                parent_run_id=parent_run_id,
                dataset_version=dataset_version,
                model_version=model_version,
                shock_id=shock_id,
                plan_id=plan_id,
                seed=seed,
                config=config or {},
                config_hash=config_hash,
                seeds=seeds or {},
                git_sha=git_sha,
                code_version=code_version,
                started_at=datetime.now(tz=UTC),
            )
        )

    def complete(
        self,
        run_id: str,
        *,
        metrics: dict[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> RunRow:
        row = self.require_row(run_id)
        row.status = str(RunStatus.COMPLETED)
        row.finished_at = datetime.now(tz=UTC)
        row.duration_ms = duration_ms
        if metrics:
            row.metrics = metrics
        return row

    def fail(self, run_id: str, error: str) -> RunRow:
        row = self.require_row(run_id)
        row.status = str(RunStatus.FAILED)
        row.finished_at = datetime.now(tz=UTC)
        row.error = error[:4000]
        return row

    def get(self, run_id: str) -> RunRow | None:
        return self.one_or_none(select(RunRow).where(RunRow.run_id == run_id))

    def require_row(self, run_id: str) -> RunRow:
        row = self.get(run_id)
        if row is None:
            raise NotFoundError(f"unknown run {run_id!r}", run_id=run_id)
        return row

    def list_runs(
        self,
        *,
        kind: RunKind | None = None,
        status: RunStatus | None = None,
        experiment_id: str | None = None,
        dataset_version: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunRow]:
        stmt = select(RunRow)
        if kind is not None:
            stmt = stmt.where(RunRow.kind == str(kind))
        if status is not None:
            stmt = stmt.where(RunRow.status == str(status))
        if experiment_id is not None:
            stmt = stmt.where(RunRow.experiment_id == experiment_id)
        if dataset_version is not None:
            stmt = stmt.where(RunRow.dataset_version == dataset_version)
        stmt = stmt.order_by(RunRow.created_at.desc()).offset(offset).limit(limit)
        return self.scalars(stmt)


class CascadeResultRepository(Repository[NodeOutcomeRow]):
    """Persists the two halves of a cascade: its events and its outcomes."""

    model = NodeOutcomeRow

    def save_result(
        self, result: CascadeResult, *, store_events: bool = True
    ) -> tuple[int, int]:
        """Write outcomes and (optionally) the full event trace."""
        outcome_rows = [
            mappers.outcome_to_row(o, result.run_id) for o in result.outcomes.values()
        ]
        self.add_all(outcome_rows)

        n_events = 0
        if store_events and result.events:
            n_events = self._insert_events(result.events, result.run_id)
        return len(outcome_rows), n_events

    def _insert_events(self, events: Sequence[PropagationEvent], run_id: str) -> int:
        payload = [
            {
                "event_id": e.event_id,
                "run_id": run_id,
                "sequence": e.sequence,
                "t": e.t,
                "type": str(e.type),
                "merchant_id": e.merchant_id,
                "counterparty_id": e.counterparty_id,
                "obligation_id": e.obligation_id,
                "amount_minor": to_minor(e.amount),
                "caused_by": e.caused_by,
                "hop": e.hop,
                "balance_after_minor": (
                    to_minor(e.balance_after) if e.balance_after is not None else None
                ),
                "buffer_after_minor": (
                    to_minor(e.buffer_after) if e.buffer_after is not None else None
                ),
                "status_after": str(e.status_after) if e.status_after else None,
                "detail": mappers._jsonable(e.detail),
            }
            for e in events
        ]
        if not payload:
            return 0
        self.session.execute(sa_insert(PropagationEventRow), payload)
        return len(payload)

    def outcomes_for_run(self, run_id: str) -> dict[str, NodeOutcome]:
        stmt = select(NodeOutcomeRow).where(NodeOutcomeRow.run_id == run_id)
        return {r.merchant_id: mappers.outcome_from_row(r) for r in self.scalars(stmt)}

    def events_for_run(
        self, run_id: str, *, merchant_id: str | None = None, limit: int | None = None
    ) -> list[PropagationEvent]:
        stmt = select(PropagationEventRow).where(PropagationEventRow.run_id == run_id)
        if merchant_id is not None:
            stmt = stmt.where(PropagationEventRow.merchant_id == merchant_id)
        stmt = stmt.order_by(PropagationEventRow.sequence)
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = list(self.session.execute(stmt).scalars().all())
        return [mappers.propagation_from_row(r) for r in rows]

    def affected_ids(self, run_id: str) -> list[str]:
        stmt = (
            select(NodeOutcomeRow.merchant_id)
            .where(NodeOutcomeRow.run_id == run_id, NodeOutcomeRow.is_affected.is_(True))
            .order_by(NodeOutcomeRow.merchant_id)
        )
        return list(self.session.execute(stmt).scalars().all())
