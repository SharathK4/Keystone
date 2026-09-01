"""Run ledger endpoints - the reproducibility record, exposed."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from lce.api.deps import UoW
from lce.api.schemas import PagedRuns, RunView
from lce.domain.enums import RunKind, RunStatus
from lce.errors import NotFoundError

router = APIRouter(prefix="/runs", tags=["runs"])


def _to_view(row: Any) -> RunView:
    return RunView(
        run_id=row.run_id,
        kind=row.kind,
        status=row.status,
        name=row.name,
        dataset_version=row.dataset_version,
        model_version=row.model_version,
        shock_id=row.shock_id,
        plan_id=row.plan_id,
        seed=row.seed,
        config_hash=row.config_hash,
        metrics=row.metrics or {},
        duration_ms=row.duration_ms,
        created_at=row.created_at.isoformat() if row.created_at else None,
        git_sha=row.git_sha,
    )


@router.get("", response_model=PagedRuns, summary="List tracked runs")
def list_runs(
    uow: UoW,
    kind: RunKind | None = None,
    status: RunStatus | None = None,
    dataset_version: str | None = None,
    experiment_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> PagedRuns:
    rows = uow.runs.list_runs(
        kind=kind,
        status=status,
        dataset_version=dataset_version,
        experiment_id=experiment_id,
        limit=limit,
        offset=offset,
    )
    return PagedRuns(items=[_to_view(r) for r in rows], limit=limit, offset=offset)


@router.get("/{run_id}", response_model=RunView, summary="Run detail")
def get_run(run_id: str, uow: UoW) -> RunView:
    row = uow.runs.get(run_id)
    if row is None:
        raise NotFoundError(f"unknown run {run_id!r}", run_id=run_id)
    return _to_view(row)


@router.get(
    "/{run_id}/manifest",
    summary="Full reproducibility manifest for a run",
)
def get_manifest(run_id: str, uow: UoW) -> dict[str, Any]:
    """Everything needed to replay this run: config, seeds, versions, git SHA."""
    row = uow.runs.get(run_id)
    if row is None:
        raise NotFoundError(f"unknown run {run_id!r}", run_id=run_id)
    return {
        "run_id": row.run_id,
        "kind": row.kind,
        "status": row.status,
        "name": row.name,
        "experiment_id": row.experiment_id,
        "parent_run_id": row.parent_run_id,
        "dataset_version": row.dataset_version,
        "model_version": row.model_version,
        "seed": row.seed,
        "seeds": row.seeds or {},
        "config": row.config or {},
        "config_hash": row.config_hash,
        "metrics": row.metrics or {},
        "code_version": row.code_version,
        "git_sha": row.git_sha,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "duration_ms": row.duration_ms,
        "error": row.error,
    }
