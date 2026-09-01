"""Razorpay REST backfill.

Webhooks give the live stream; this gives history. The importer walks a time
window of Orders/Payments, runs every record through the canonical pipeline, and
persists the accepted events together with their provenance.

Idempotency
-----------
Re-running an overlapping window must not double-count cash. Two mechanisms
enforce that, and both are needed:

* the ``(source_system, source_id)`` unique constraint on the provenance table
  makes a duplicate insert impossible at the database level;
* the pipeline is *pre-loaded* with the source ids already stored, so duplicates
  are filtered before the insert is attempted rather than surfacing as
  constraint violations that would abort the batch.

Resumability
------------
Each import records its window and tallies. :meth:`RazorpayImporter.resume_from`
reads the newest ingested timestamp, so an interrupted backfill continues where
it stopped instead of re-walking history.

Scope: **read-only**. Nothing here moves money; live transfers are deliberately
out of scope until the final integration phase.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from lce.data.pipeline import (
    PipelineResult,
    SourceSystem,
    reconcile_totals,
    run_pipeline,
)
from lce.data.unit_of_work import UnitOfWork
from lce.domain.base import new_id
from lce.errors import RazorpayError, ValidationError
from lce.logging import get_logger
from lce.razorpay.client import RazorpayClient
from lce.razorpay.mapper import MerchantResolver, identity_resolver

logger = get_logger(__name__)


class RazorpayImporter:
    """Imports Razorpay Orders/Payments into the canonical event schema."""

    def __init__(
        self,
        uow: UnitOfWork,
        client: RazorpayClient | None = None,
        *,
        resolver: MerchantResolver | None = None,
        epoch: datetime | None = None,
    ) -> None:
        self.uow = uow
        self.client = client or RazorpayClient()
        self.resolver = resolver or identity_resolver({})
        self.epoch = epoch or datetime(2025, 1, 1, tzinfo=UTC)

    # ---------------------------------------------------------------- import

    def import_payments(
        self,
        *,
        dataset_id: str | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        max_items: int | None = 1000,
        allow_external: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Backfill payments in a window. Returns the import tally."""
        return self._import(
            resource="payments",
            fetch=lambda: self.client.list_payments(
                from_ts=from_ts, to_ts=to_ts, max_items=max_items
            ),
            dataset_id=dataset_id,
            from_ts=from_ts,
            to_ts=to_ts,
            allow_external=allow_external,
            dry_run=dry_run,
        )

    def import_orders(
        self,
        *,
        dataset_id: str | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        max_items: int | None = 1000,
        allow_external: bool = True,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Backfill payments captured against orders in a window.

        Orders themselves are not cash movements, so each order is expanded into
        its captured payments. An order with no captured payment contributes
        nothing here - by design: it is an expectation, not a flow, and treating
        it as one would invent inflows that never happened.
        """

        def fetch() -> list[dict[str, Any]]:
            orders = self.client.list_orders(
                from_ts=from_ts, to_ts=to_ts, max_items=max_items
            )
            records: list[dict[str, Any]] = []
            for order in orders:
                order_id = order.get("id")
                if not order_id:
                    continue
                try:
                    payments = self.client.list_order_payments(str(order_id))
                except RazorpayError as exc:
                    logger.warning(
                        "order_payments_fetch_failed", order_id=order_id, error=exc.message
                    )
                    continue
                for payment in payments:
                    payment.setdefault("order_id", order_id)
                    records.append(payment)
            return records

        return self._import(
            resource="orders",
            fetch=fetch,
            dataset_id=dataset_id,
            from_ts=from_ts,
            to_ts=to_ts,
            allow_external=allow_external,
            dry_run=dry_run,
        )

    def _import(
        self,
        *,
        resource: str,
        fetch: Any,
        dataset_id: str | None,
        from_ts: int | None,
        to_ts: int | None,
        allow_external: bool,
        dry_run: bool,
    ) -> dict[str, Any]:
        import_id = new_id("imp")
        source = SourceSystem.RAZORPAY_API

        try:
            records = list(fetch())
        except RazorpayError as exc:
            self.uow.imports.record(
                import_id,
                source_system=str(source),
                resource=resource,
                dataset_id=dataset_id,
                window_from=from_ts,
                window_to=to_ts,
                status="failed",
                error=exc.message,
            )
            self.uow.commit()
            raise

        result = self.ingest_records(
            records,
            dataset_id=dataset_id,
            source=source,
            allow_external=allow_external,
            persist=not dry_run,
        )

        tally = {
            "import_id": import_id,
            "resource": resource,
            "dataset_id": dataset_id,
            "window_from": from_ts,
            "window_to": to_ts,
            "fetched": len(records),
            "dry_run": dry_run,
            **result.summary(),
        }

        self.uow.imports.record(
            import_id,
            source_system=str(source),
            resource=resource,
            dataset_id=dataset_id,
            window_from=from_ts,
            window_to=to_ts,
            fetched=len(records),
            accepted=result.accepted,
            rejected=result.rejected,
            status="dry_run" if dry_run else "completed",
            detail={"rejection_reasons": result.reason_counts()},
        )
        self.uow.commit()

        logger.info("razorpay_import_complete", **tally)
        return tally

    # -------------------------------------------------------------- pipeline

    def ingest_records(
        self,
        records: Sequence[dict[str, Any]],
        *,
        dataset_id: str | None,
        source: SourceSystem = SourceSystem.RAZORPAY_API,
        allow_external: bool = True,
        persist: bool = True,
    ) -> PipelineResult:
        """Run raw records through the canonical pipeline and store the output."""
        source_ids = [str(r.get("id")) for r in records if r.get("id")]
        seen = self.uow.provenance.seen_source_ids(str(source), source_ids)

        result = run_pipeline(
            records,
            resolver=self.resolver,
            epoch=self.epoch,
            source_system=source,
            seen_source_ids=seen,
            allow_external=allow_external,
        )

        # Guard against a unit-conversion slip before anything is written: a
        # paise/rupee mix-up is invisible in row counts and ruinous downstream.
        reconciliation = reconcile_totals(result, records)

        if persist and result.events:
            if dataset_id is None:
                raise ValidationError(
                    "cannot persist imported events without a dataset_id"
                )
            self.uow.payments.save_many(result.events, dataset_id)
            self.uow.provenance.save_many(result.provenance, dataset_id)
            self.uow.flush()

        logger.info(
            "records_ingested",
            source=str(source),
            persisted=persist,
            **result.summary(),
            **{"reconciled_amount": reconciliation["canonical_amount"]},
        )
        return result

    # ------------------------------------------------------------ resumption

    def resume_from(self, source: SourceSystem = SourceSystem.RAZORPAY_API) -> int | None:
        """Newest raw timestamp already ingested, for a resumable backfill."""
        return self.uow.provenance.latest_timestamp(str(source))

    def trace(self, event_id: str) -> dict[str, Any] | None:
        """Full provenance for one canonical event - the audit answer."""
        row = self.uow.provenance.for_event(event_id)
        if row is None:
            return None
        return {
            "event_id": row.event_id,
            "source_system": row.source_system,
            "source_id": row.source_id,
            "source_payload_hash": row.source_payload_hash,
            "source_reference": row.source_reference,
            "pipeline_version": row.pipeline_version,
            "raw_amount_minor": row.raw_amount_minor,
            "raw_timestamp": row.raw_timestamp,
            "raw_currency": row.raw_currency,
            "ingested_at": row.ingested_at.isoformat() if row.ingested_at else None,
        }
