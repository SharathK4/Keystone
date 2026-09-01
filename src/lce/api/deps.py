"""FastAPI dependencies.

Each request gets its own session and unit of work, closed when the request
ends. Services are constructed per request on top of that unit of work, which
keeps the transaction boundary aligned with the request boundary.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends

from lce.config import Settings, get_settings
from lce.data.unit_of_work import UnitOfWork
from lce.experiments.tracker import RunTracker
from lce.services.analysis_service import AnalysisService
from lce.services.ingestion_service import IngestionService
from lce.services.network_service import NetworkService


def get_uow() -> Iterator[UnitOfWork]:
    """Request-scoped unit of work."""
    with UnitOfWork() as uow:
        yield uow


UoW = Annotated[UnitOfWork, Depends(get_uow)]
Config = Annotated[Settings, Depends(get_settings)]


def get_tracker(uow: UoW) -> RunTracker:
    return RunTracker(uow=uow, persist_db=True)


Tracker = Annotated[RunTracker, Depends(get_tracker)]


def get_network_service(uow: UoW, tracker: Tracker) -> NetworkService:
    return NetworkService(uow, tracker)


def get_analysis_service(uow: UoW, tracker: Tracker) -> AnalysisService:
    return AnalysisService(uow, tracker=tracker)


def get_ingestion_service(uow: UoW) -> IngestionService:
    return IngestionService(uow)


Networks = Annotated[NetworkService, Depends(get_network_service)]
Analysis = Annotated[AnalysisService, Depends(get_analysis_service)]
Ingestion = Annotated[IngestionService, Depends(get_ingestion_service)]
