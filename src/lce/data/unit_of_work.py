"""Unit of work.

Bundles every repository over a single session and transaction, so a service
method that touches datasets, merchants, events and runs either commits all of
it or none of it. Repositories deliberately do not commit on their own.

Usage::

    with UnitOfWork() as uow:
        uow.datasets.create(...)
        uow.merchants.save_many(profiles, dataset_id)
        uow.commit()
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.orm import Session, sessionmaker

from lce.data.database import get_session_factory
from lce.data.repositories.analytics import (
    EvaluationRepository,
    ExperimentRepository,
    ImportRunRepository,
    PredictionRepository,
    ProvenanceRepository,
    WebhookEventRepository,
)
from lce.data.repositories.network import (
    DatasetRepository,
    DependencyEdgeRepository,
    MerchantRepository,
    ObligationRepository,
    PaymentEventRepository,
)
from lce.data.repositories.runs import (
    CascadeResultRepository,
    InterventionPlanRepository,
    RunRepository,
    ShockRepository,
)


class UnitOfWork:
    """Transactional boundary around the full repository set."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        session: Session | None = None,
    ) -> None:
        self._factory = session_factory
        self._external_session = session
        self.session: Session = session  # type: ignore[assignment]
        self._owns_session = session is None
        if session is not None:
            self._bind(session)

    def _bind(self, session: Session) -> None:
        self.session = session
        self.datasets = DatasetRepository(session)
        self.merchants = MerchantRepository(session)
        self.payments = PaymentEventRepository(session)
        self.obligations = ObligationRepository(session)
        self.edges = DependencyEdgeRepository(session)
        self.shocks = ShockRepository(session)
        self.plans = InterventionPlanRepository(session)
        self.runs = RunRepository(session)
        self.cascades = CascadeResultRepository(session)
        self.predictions = PredictionRepository(session)
        self.evaluations = EvaluationRepository(session)
        self.experiments = ExperimentRepository(session)
        self.webhooks = WebhookEventRepository(session)
        self.provenance = ProvenanceRepository(session)
        self.imports = ImportRunRepository(session)

    def __enter__(self) -> Self:
        if self._owns_session:
            factory = self._factory or get_session_factory()
            self._bind(factory())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None:
                self.rollback()
        finally:
            if self._owns_session and self.session is not None:
                self.session.close()

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def flush(self) -> None:
        self.session.flush()
