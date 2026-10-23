"""Build the pulled-stone artifact: every merchant's failure, analysed offline.

For each of the snapshot's merchants, the same bounded analysis the API runs on
``POST /scenarios/analyze`` is run for a cash shock of ``2x`` the merchant's
liquidity slack at hour 0, and again at ``1.5x`` and ``2.5x`` so every figure
carries the range it moves over when the shock is a quarter smaller or larger.

The hosted free tier answers a pulled stone instantly from this artifact; the
interface still offers to compute one now.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from lce import __version__
from lce.logging import get_logger
from lce.snapshot.models import ScenarioSnapshot
from lce.snapshot.store import (
    MAX_ON_DEMAND_ACTIONS,
    MAX_ON_DEMAND_CANDIDATES,
    MAX_ON_DEMAND_ORIGINS,
    SnapshotStore,
)
from lce.structure.models import (
    LABEL,
    PullAffected,
    PullPlan,
    PullRange,
    PullRecord,
    PullsProvenance,
)

logger = get_logger(__name__)

FORMAT = "keystone-pulls-v1"
MULTIPLES: tuple[float, ...] = (1.5, 2.0, 2.5)
CENTRAL = 2.0
ONSET_HOURS = 0.0


def _first(s: ScenarioSnapshot) -> float | None:
    hours = [
        a.time_to_constraint_hours
        for a in s.affected_merchants
        if a.time_to_constraint_hours is not None
    ]
    return min(hours) if hours else None


def _span(values: Sequence[float | None]) -> list[float] | None:
    known = [v for v in values if v is not None]
    return [min(known), max(known)] if known else None


def pull_record(
    store: SnapshotStore, merchant_id: str, multiples: Sequence[float] = MULTIPLES
) -> PullRecord:
    runs = {
        k: store.analyze(merchant_ids=[merchant_id], magnitude_multiple=k, onset_hours=ONSET_HOURS)
        for k in multiples
    }
    s = runs[CENTRAL]
    m = store.merchant(merchant_id)
    rec, cf = s.recommended_intervention, s.counterfactual
    others = list(runs.values())
    return PullRecord(
        merchant_id=merchant_id,
        tier=m.tier,
        sector=m.sector,
        magnitude_multiple=CENTRAL,
        magnitude_inr=s.shock.magnitude,
        onset_hours=ONSET_HOURS,
        n_affected=s.projected_impact.n_affected,
        n_defaulted=s.projected_impact.n_defaulted,
        max_depth=s.projected_impact.max_cascade_depth,
        disrupted_value_inr=s.projected_impact.disrupted_value,
        first_constraint_hours=_first(s),
        affected=[
            PullAffected(
                merchant_id=a.merchant_id,
                cascade_depth=a.cascade_depth,
                hours_to_constraint=a.time_to_constraint_hours,
                disrupted_value_inr=a.disrupted_value,
            )
            for a in s.affected_merchants
        ],
        range=PullRange(
            multiples=[min(multiples), max(multiples)],
            n_affected=[
                min(o.projected_impact.n_affected for o in others),
                max(o.projected_impact.n_affected for o in others),
            ],
            first_constraint_hours=_span([_first(o) for o in others]),
            disrupted_value_inr=[
                min(o.projected_impact.disrupted_value for o in others),
                max(o.projected_impact.disrupted_value for o in others),
            ],
        ),
        plan=(
            PullPlan(
                action_type=str(rec.type),
                merchant_id=rec.merchant_id,
                amount_inr=rec.amount,
                disruption_cut_pct=cf.disruption_reduction_pct,
                payments_kept_on_time_inr=cf.commerce_preserved,
            )
            if rec is not None
            else None
        ),
    )


def build_pulls(
    *,
    snapshot_dir: Path,
    merchant_ids: Sequence[str] | None = None,
    multiples: Sequence[float] = MULTIPLES,
) -> dict[str, Any]:
    store = SnapshotStore(snapshot_dir)
    ids = list(merchant_ids) if merchant_ids else sorted(m.merchant_id for m in store.merchants())
    records = []
    for i, merchant_id in enumerate(ids, start=1):
        records.append(pull_record(store, merchant_id, multiples))
        logger.info("pull_analysed", merchant_id=merchant_id, done=i, of=len(ids))
    provenance = PullsProvenance(
        kind="replayed_offline",
        label=LABEL,
        snapshot_id=store.snapshot_id,
        shock="cash withdrawal at hour 0, sized as a multiple of the merchant's liquidity slack",
        multiples=list(multiples),
        central_multiple=CENTRAL,
        limits={
            "max_origins": MAX_ON_DEMAND_ORIGINS,
            "max_candidates": MAX_ON_DEMAND_CANDIDATES,
            "max_actions": MAX_ON_DEMAND_ACTIONS,
        },
        code_version=__version__,
    )
    return {
        "format": FORMAT,
        "provenance": provenance.model_dump(mode="json"),
        "records": [r.model_dump(mode="json") for r in records],
    }
