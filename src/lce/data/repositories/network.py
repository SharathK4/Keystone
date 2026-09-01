"""Repositories for the network itself: datasets, merchants, events, edges."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select

from lce.data import mappers
from lce.data.orm import (
    DatasetRow,
    DependencyEdgeRow,
    MerchantRow,
    ObligationRow,
    PaymentEventRow,
    to_minor,
)
from lce.data.repositories.base import Repository
from lce.domain.edges import DependencyEdge
from lce.domain.events import Obligation, PaymentEvent
from lce.domain.merchant import MerchantProfile
from lce.errors import NotFoundError


class DatasetRepository(Repository[DatasetRow]):
    model = DatasetRow

    def create(
        self,
        dataset_id: str,
        dataset_version: str,
        *,
        source: str = "synthetic",
        seed: int = 0,
        config: dict[str, Any] | None = None,
        stats: dict[str, Any] | None = None,
        epoch: datetime | None = None,
        notes: str = "",
    ) -> DatasetRow:
        return self.add(
            DatasetRow(
                id=dataset_id,
                dataset_version=dataset_version,
                source=source,
                seed=seed,
                config=config or {},
                stats=stats or {},
                epoch=epoch,
                notes=notes,
            )
        )

    def get(self, dataset_id: str) -> DatasetRow | None:
        return self.session.get(DatasetRow, dataset_id)

    def require(self, dataset_id: str) -> DatasetRow:
        row = self.get(dataset_id)
        if row is None:
            raise NotFoundError(f"unknown dataset {dataset_id!r}", dataset_id=dataset_id)
        return row

    def by_version(self, dataset_version: str) -> DatasetRow | None:
        return self.one_or_none(
            select(DatasetRow).where(DatasetRow.dataset_version == dataset_version)
        )

    def list_recent(self, limit: int = 25) -> list[DatasetRow]:
        return self.scalars(
            select(DatasetRow).order_by(DatasetRow.created_at.desc()).limit(limit)
        )


class MerchantRepository(Repository[MerchantRow]):
    model = MerchantRow

    def save_many(self, profiles: Sequence[MerchantProfile], dataset_id: str) -> int:
        rows = [mappers.merchant_to_row(p, dataset_id) for p in profiles]
        self.add_all(rows)
        return len(rows)

    def get(self, dataset_id: str, merchant_id: str) -> MerchantProfile | None:
        row = self.one_or_none(
            select(MerchantRow).where(
                MerchantRow.dataset_id == dataset_id,
                MerchantRow.merchant_id == merchant_id,
            )
        )
        return mappers.merchant_from_row(row) if row else None

    def require(self, dataset_id: str, merchant_id: str) -> MerchantProfile:
        profile = self.get(dataset_id, merchant_id)
        if profile is None:
            raise NotFoundError(f"unknown merchant {merchant_id!r}", merchant_id=merchant_id)
        return profile

    def list_for_dataset(
        self,
        dataset_id: str,
        *,
        sector: str | None = None,
        tier: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[MerchantProfile]:
        stmt = select(MerchantRow).where(MerchantRow.dataset_id == dataset_id)
        if sector:
            stmt = stmt.where(MerchantRow.sector == sector)
        if tier:
            stmt = stmt.where(MerchantRow.tier == tier)
        stmt = stmt.order_by(MerchantRow.merchant_id).offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return [mappers.merchant_from_row(r) for r in self.scalars(stmt)]

    def count_for_dataset(self, dataset_id: str) -> int:
        return self.count(select(MerchantRow).where(MerchantRow.dataset_id == dataset_id))

    def by_external_id(self, external_id: str) -> MerchantProfile | None:
        row = self.one_or_none(
            select(MerchantRow).where(MerchantRow.external_id == external_id)
        )
        return mappers.merchant_from_row(row) if row else None


class PaymentEventRepository(Repository[PaymentEventRow]):
    """The event fact table. Bulk paths matter: datasets carry 10^5+ rows."""

    model = PaymentEventRow

    def save_many(self, events: Sequence[PaymentEvent], dataset_id: str) -> int:
        return self.bulk_insert(
            [
                {
                    "event_id": e.event_id,
                    "dataset_id": dataset_id,
                    "payer_id": e.payer_id,
                    "payee_id": e.payee_id,
                    "amount_minor": to_minor(e.amount),
                    "t": e.t,
                    "obligation_id": e.obligation_id,
                    "channel": str(e.channel),
                    "status": str(e.status),
                    "settlement_lag_hours": e.settlement_lag_hours,
                    "external_id": e.external_id,
                    "is_synthetic": e.is_synthetic,
                    "meta": dict(e.metadata),
                }
                for e in events
            ]
        )

    def list_for_dataset(
        self,
        dataset_id: str,
        *,
        t0: float | None = None,
        t1: float | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[PaymentEvent]:
        stmt = select(PaymentEventRow).where(PaymentEventRow.dataset_id == dataset_id)
        if t0 is not None:
            stmt = stmt.where(PaymentEventRow.t >= t0)
        if t1 is not None:
            stmt = stmt.where(PaymentEventRow.t < t1)
        stmt = stmt.order_by(PaymentEventRow.t, PaymentEventRow.event_id).offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return [mappers.payment_from_row(r) for r in self.scalars(stmt)]

    def for_pair(self, dataset_id: str, payer_id: str, payee_id: str) -> list[PaymentEvent]:
        """Full event history on one directed edge - never aggregated away."""
        stmt = (
            select(PaymentEventRow)
            .where(
                PaymentEventRow.dataset_id == dataset_id,
                PaymentEventRow.payer_id == payer_id,
                PaymentEventRow.payee_id == payee_id,
            )
            .order_by(PaymentEventRow.t)
        )
        return [mappers.payment_from_row(r) for r in self.scalars(stmt)]

    def inbound(self, dataset_id: str, merchant_id: str) -> list[PaymentEvent]:
        stmt = (
            select(PaymentEventRow)
            .where(
                PaymentEventRow.dataset_id == dataset_id,
                PaymentEventRow.payee_id == merchant_id,
            )
            .order_by(PaymentEventRow.t)
        )
        return [mappers.payment_from_row(r) for r in self.scalars(stmt)]

    def outbound(self, dataset_id: str, merchant_id: str) -> list[PaymentEvent]:
        stmt = (
            select(PaymentEventRow)
            .where(
                PaymentEventRow.dataset_id == dataset_id,
                PaymentEventRow.payer_id == merchant_id,
            )
            .order_by(PaymentEventRow.t)
        )
        return [mappers.payment_from_row(r) for r in self.scalars(stmt)]

    def count_for_dataset(self, dataset_id: str) -> int:
        return self.count(
            select(PaymentEventRow).where(PaymentEventRow.dataset_id == dataset_id)
        )

    def by_external_id(self, external_id: str) -> PaymentEvent | None:
        row = self.one_or_none(
            select(PaymentEventRow).where(PaymentEventRow.external_id == external_id)
        )
        return mappers.payment_from_row(row) if row else None


class ObligationRepository(Repository[ObligationRow]):
    model = ObligationRow

    def save_many(self, obligations: Sequence[Obligation], dataset_id: str) -> int:
        rows = [mappers.obligation_to_row(o, dataset_id) for o in obligations]
        self.add_all(rows)
        return len(rows)

    def get(self, dataset_id: str, obligation_id: str) -> Obligation | None:
        row = self._row(dataset_id, obligation_id)
        return mappers.obligation_from_row(row) if row else None

    def require(self, dataset_id: str, obligation_id: str) -> Obligation:
        obligation = self.get(dataset_id, obligation_id)
        if obligation is None:
            raise NotFoundError(
                f"unknown obligation {obligation_id!r}", obligation_id=obligation_id
            )
        return obligation

    def _row(self, dataset_id: str, obligation_id: str) -> ObligationRow | None:
        return self.one_or_none(
            select(ObligationRow).where(
                ObligationRow.dataset_id == dataset_id,
                ObligationRow.obligation_id == obligation_id,
            )
        )

    def list_for_dataset(
        self, dataset_id: str, *, limit: int | None = None, offset: int = 0
    ) -> list[Obligation]:
        stmt = (
            select(ObligationRow)
            .where(ObligationRow.dataset_id == dataset_id)
            .order_by(ObligationRow.due_t, ObligationRow.obligation_id)
            .offset(offset)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return [mappers.obligation_from_row(r) for r in self.scalars(stmt)]

    def payables_of(self, dataset_id: str, merchant_id: str) -> list[Obligation]:
        stmt = (
            select(ObligationRow)
            .where(
                ObligationRow.dataset_id == dataset_id,
                ObligationRow.debtor_id == merchant_id,
            )
            .order_by(ObligationRow.due_t)
        )
        return [mappers.obligation_from_row(r) for r in self.scalars(stmt)]

    def receivables_of(self, dataset_id: str, merchant_id: str) -> list[Obligation]:
        stmt = (
            select(ObligationRow)
            .where(
                ObligationRow.dataset_id == dataset_id,
                ObligationRow.creditor_id == merchant_id,
            )
            .order_by(ObligationRow.due_t)
        )
        return [mappers.obligation_from_row(r) for r in self.scalars(stmt)]

    def update_status(
        self, dataset_id: str, obligation: Obligation
    ) -> ObligationRow:
        """Write back settlement facts after a run."""
        row = self._row(dataset_id, obligation.obligation_id)
        if row is None:
            raise NotFoundError(f"unknown obligation {obligation.obligation_id!r}")
        row.amount_paid_minor = to_minor(obligation.amount_paid)
        row.settled_t = obligation.settled_t
        row.status = str(obligation.status)
        row.due_t = obligation.due_t
        row.original_due_t = obligation.original_due_t
        return row


class DependencyEdgeRepository(Repository[DependencyEdgeRow]):
    """The derived overlay. Several estimators' views coexist by design."""

    model = DependencyEdgeRow

    def save_many(
        self,
        edges: Sequence[DependencyEdge],
        dataset_id: str,
        *,
        model_version: str = "v0",
    ) -> int:
        rows = [mappers.edge_to_row(e, dataset_id, model_version) for e in edges]
        self.add_all(rows)
        return len(rows)

    def replace_for_estimator(
        self,
        edges: Sequence[DependencyEdge],
        dataset_id: str,
        estimator: str,
        model_version: str = "v0",
    ) -> int:
        """Overwrite one estimator's edges, leaving every other estimator intact."""
        self.delete_where(
            DependencyEdgeRow.dataset_id == dataset_id,
            DependencyEdgeRow.estimator == estimator,
            DependencyEdgeRow.model_version == model_version,
        )
        self.flush()
        return self.save_many(edges, dataset_id, model_version=model_version)

    def list_for_dataset(
        self,
        dataset_id: str,
        *,
        estimator: str | None = None,
        model_version: str | None = None,
        ground_truth: bool | None = None,
    ) -> list[DependencyEdge]:
        stmt = select(DependencyEdgeRow).where(DependencyEdgeRow.dataset_id == dataset_id)
        if estimator is not None:
            stmt = stmt.where(DependencyEdgeRow.estimator == estimator)
        if model_version is not None:
            stmt = stmt.where(DependencyEdgeRow.model_version == model_version)
        if ground_truth is not None:
            stmt = stmt.where(DependencyEdgeRow.is_ground_truth == ground_truth)
        stmt = stmt.order_by(DependencyEdgeRow.source_id, DependencyEdgeRow.target_id)
        return [mappers.edge_from_row(r) for r in self.scalars(stmt)]

    def out_edges(self, dataset_id: str, merchant_id: str) -> list[DependencyEdge]:
        stmt = select(DependencyEdgeRow).where(
            DependencyEdgeRow.dataset_id == dataset_id,
            DependencyEdgeRow.source_id == merchant_id,
        )
        return [mappers.edge_from_row(r) for r in self.scalars(stmt)]

    def in_edges(self, dataset_id: str, merchant_id: str) -> list[DependencyEdge]:
        stmt = select(DependencyEdgeRow).where(
            DependencyEdgeRow.dataset_id == dataset_id,
            DependencyEdgeRow.target_id == merchant_id,
        )
        return [mappers.edge_from_row(r) for r in self.scalars(stmt)]
