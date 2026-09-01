"""Repositories for predictions, evaluations, experiments and webhooks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from lce.data import mappers
from lce.data.orm import (
    EvaluationRow,
    ExperimentRow,
    ImportRunRow,
    NodeExposureRow,
    PredictionRow,
    ProvenanceRow,
    WebhookEventRow,
)
from lce.data.repositories.base import Repository
from lce.data.pipeline import Provenance
from lce.domain.evaluation import EvaluationResult
from lce.domain.prediction import ModelPrediction
from lce.errors import NotFoundError


class PredictionRepository(Repository[PredictionRow]):
    model = PredictionRow

    def save(
        self, prediction: ModelPrediction, dataset_version: str | None = None
    ) -> PredictionRow:
        head, exposures = mappers.prediction_to_rows(prediction, dataset_version)
        self.add(head)
        self.add_all(exposures)
        return head

    def get(self, prediction_id: str) -> ModelPrediction | None:
        head = self.one_or_none(
            select(PredictionRow).where(PredictionRow.prediction_id == prediction_id)
        )
        if head is None:
            return None
        exposures = list(
            self.session.execute(
                select(NodeExposureRow).where(
                    NodeExposureRow.prediction_id == prediction_id
                )
            )
            .scalars()
            .all()
        )
        return mappers.prediction_from_rows(head, exposures)

    def require(self, prediction_id: str) -> ModelPrediction:
        prediction = self.get(prediction_id)
        if prediction is None:
            raise NotFoundError(
                f"unknown prediction {prediction_id!r}", prediction_id=prediction_id
            )
        return prediction

    def list_for_shock(self, shock_id: str, limit: int = 50) -> list[PredictionRow]:
        return self.scalars(
            select(PredictionRow)
            .where(PredictionRow.shock_id == shock_id)
            .order_by(PredictionRow.created_at.desc())
            .limit(limit)
        )


class EvaluationRepository(Repository[EvaluationRow]):
    model = EvaluationRow

    def save(self, result: EvaluationResult) -> EvaluationRow:
        classification = result.classification
        timing = result.timing
        intervention = result.intervention
        return self.add(
            EvaluationRow(
                evaluation_id=result.evaluation_id,
                run_id=result.run_id,
                prediction_id=result.prediction_id,
                plan_id=result.plan_id,
                shock_id=result.shock_id,
                name=result.name,
                predictor=result.predictor,
                optimizer=result.optimizer,
                model_version=result.model_version,
                dataset_version=result.dataset_version,
                horizon_hours=result.horizon_hours,
                precision=classification.precision if classification else None,
                recall=classification.recall if classification else None,
                f1=classification.f1 if classification else None,
                pr_auc=classification.pr_auc if classification else None,
                hit_time_mae_hours=timing.mae_hours if timing else None,
                disruption_prevented=(
                    intervention.disruption_prevented if intervention else None
                ),
                optimality_gap=intervention.optimality_gap if intervention else None,
                seed=result.seed,
                config_hash=result.config_hash,
                payload=result.model_dump(mode="json"),
                notes=result.notes,
            )
        )

    def get(self, evaluation_id: str) -> EvaluationResult | None:
        row = self.one_or_none(
            select(EvaluationRow).where(EvaluationRow.evaluation_id == evaluation_id)
        )
        if row is None:
            return None
        return EvaluationResult.model_validate(row.payload)

    def list_recent(
        self,
        *,
        predictor: str | None = None,
        optimizer: str | None = None,
        dataset_version: str | None = None,
        limit: int = 50,
    ) -> list[EvaluationRow]:
        stmt = select(EvaluationRow)
        if predictor is not None:
            stmt = stmt.where(EvaluationRow.predictor == predictor)
        if optimizer is not None:
            stmt = stmt.where(EvaluationRow.optimizer == optimizer)
        if dataset_version is not None:
            stmt = stmt.where(EvaluationRow.dataset_version == dataset_version)
        return self.scalars(stmt.order_by(EvaluationRow.created_at.desc()).limit(limit))


class ExperimentRepository(Repository[ExperimentRow]):
    model = ExperimentRow

    def create(
        self,
        experiment_id: str,
        name: str,
        *,
        description: str = "",
        config: dict[str, Any] | None = None,
        config_hash: str | None = None,
        seed: int = 0,
        tags: list[str] | None = None,
    ) -> ExperimentRow:
        return self.add(
            ExperimentRow(
                experiment_id=experiment_id,
                name=name,
                description=description,
                config=config or {},
                config_hash=config_hash,
                seed=seed,
                tags=tags or [],
            )
        )

    def get(self, experiment_id: str) -> ExperimentRow | None:
        return self.one_or_none(
            select(ExperimentRow).where(ExperimentRow.experiment_id == experiment_id)
        )

    def require(self, experiment_id: str) -> ExperimentRow:
        row = self.get(experiment_id)
        if row is None:
            raise NotFoundError(f"unknown experiment {experiment_id!r}")
        return row

    def list_recent(self, limit: int = 50) -> list[ExperimentRow]:
        return self.scalars(
            select(ExperimentRow).order_by(ExperimentRow.created_at.desc()).limit(limit)
        )


class ProvenanceRepository(Repository[ProvenanceRow]):
    """Audit trail for canonical events, and the source-id dedup index."""

    model = ProvenanceRow

    def save_many(
        self, records: Sequence[Provenance], dataset_id: str | None = None
    ) -> int:
        rows = [
            ProvenanceRow(
                event_id=p.event_id,
                dataset_id=dataset_id,
                source_system=p.source_system,
                source_id=p.source_id,
                source_payload_hash=p.source_payload_hash,
                source_reference=p.source_reference,
                pipeline_version=p.pipeline_version,
                raw_amount_minor=p.raw_amount_minor,
                raw_timestamp=p.raw_timestamp,
                raw_currency=p.raw_currency,
                notes=dict(p.notes),
            )
            for p in records
        ]
        self.add_all(rows)
        return len(rows)

    def seen_source_ids(
        self, source_system: str, source_ids: Sequence[str] | None = None
    ) -> set[str]:
        """Source ids already ingested - the input to batch deduplication."""
        stmt = select(ProvenanceRow.source_id).where(
            ProvenanceRow.source_system == source_system
        )
        if source_ids:
            stmt = stmt.where(ProvenanceRow.source_id.in_(list(source_ids)))
        return set(self.session.execute(stmt).scalars().all())

    def for_event(self, event_id: str) -> ProvenanceRow | None:
        return self.one_or_none(
            select(ProvenanceRow).where(ProvenanceRow.event_id == event_id)
        )

    def by_source(self, source_system: str, source_id: str) -> ProvenanceRow | None:
        return self.one_or_none(
            select(ProvenanceRow).where(
                ProvenanceRow.source_system == source_system,
                ProvenanceRow.source_id == source_id,
            )
        )

    def latest_timestamp(self, source_system: str) -> int | None:
        """Newest raw timestamp ingested - where a resumable import restarts."""
        stmt = (
            select(ProvenanceRow.raw_timestamp)
            .where(
                ProvenanceRow.source_system == source_system,
                ProvenanceRow.raw_timestamp.is_not(None),
            )
            .order_by(ProvenanceRow.raw_timestamp.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalars().one_or_none()


class ImportRunRepository(Repository[ImportRunRow]):
    model = ImportRunRow

    def record(
        self,
        import_id: str,
        *,
        source_system: str,
        resource: str,
        dataset_id: str | None = None,
        window_from: int | None = None,
        window_to: int | None = None,
        fetched: int = 0,
        accepted: int = 0,
        rejected: int = 0,
        status: str = "completed",
        error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> ImportRunRow:
        return self.add(
            ImportRunRow(
                import_id=import_id,
                dataset_id=dataset_id,
                source_system=source_system,
                resource=resource,
                window_from=window_from,
                window_to=window_to,
                fetched=fetched,
                accepted=accepted,
                rejected=rejected,
                status=status,
                error=error,
                detail=detail or {},
            )
        )

    def list_recent(self, limit: int = 25) -> list[ImportRunRow]:
        return self.scalars(
            select(ImportRunRow).order_by(ImportRunRow.created_at.desc()).limit(limit)
        )


class WebhookEventRepository(Repository[WebhookEventRow]):
    """Inbound webhook log.

    Razorpay retries deliveries, so the provider event id is unique-constrained
    and :meth:`already_seen` is checked before any state change - a redelivery
    must not inject the same payment into the network twice.
    """

    model = WebhookEventRow

    def record(
        self,
        provider_event_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        provider: str = "razorpay",
        signature_verified: bool = False,
    ) -> WebhookEventRow:
        return self.add(
            WebhookEventRow(
                provider=provider,
                provider_event_id=provider_event_id,
                event_type=event_type,
                payload=payload,
                signature_verified=signature_verified,
            )
        )

    def already_seen(self, provider_event_id: str, provider: str = "razorpay") -> bool:
        row = self.one_or_none(
            select(WebhookEventRow).where(
                WebhookEventRow.provider == provider,
                WebhookEventRow.provider_event_id == provider_event_id,
            )
        )
        return row is not None

    def mark_processed(self, provider_event_id: str, error: str | None = None) -> None:
        row = self.one_or_none(
            select(WebhookEventRow).where(
                WebhookEventRow.provider_event_id == provider_event_id
            )
        )
        if row is None:
            return
        row.processed = error is None
        row.processed_at = datetime.now(tz=UTC)
        row.error = error

    def list_unprocessed(self, limit: int = 100) -> list[WebhookEventRow]:
        return self.scalars(
            select(WebhookEventRow)
            .where(WebhookEventRow.processed.is_(False))
            .order_by(WebhookEventRow.received_at)
            .limit(limit)
        )
