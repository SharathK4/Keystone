"""The cost of waiting, for the walk's own shock. Built offline, served read-only.

Gate H measured the value of speed on the benchmark scenarios. The walk follows
a different shock - one merchant losing a multiple of its cushion - so the page
would be showing someone else's curve. This replays the walk's shock on the
same machinery the bounded analysis uses (the snapshot's estimated network, the
same candidate generation, greedy search and replay), so at zero delay the plan
and its effect are the ones the live analysis reports, by construction and by
test.

Two arms per delay, as in Gate H:

* **as chosen** - the plan picked at the shock, with every action landing
  ``delay`` hours later;
* **re-planned** - the search run again knowing the money can land no earlier
  than ``delay`` hours after the shock.

Re-planning runs the search once per delay, which is too slow for a page read,
so this is an offline build: content-addressed, labelled "replayed offline",
rebuilt only by ``lce walk build-speed``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lce import __version__
from lce.domain.enums import ShockKind
from lce.domain.intervention import Intervention
from lce.domain.shock import Shock, ShockComponent
from lce.errors import NotFoundError, ValidationError
from lce.intervention.actions import generate_actions
from lce.intervention.evaluate import Replay, replay
from lce.intervention.problem import InterventionConstraints, ObjectiveSpec
from lce.intervention.scalable import greedy_solve
from lce.models.propagation import LinearThresholdPropagator, PropagationConfig
from lce.simulation.counterfactual import CounterfactualEvaluator
from lce.snapshot.analysis import _liquidity_slack
from lce.snapshot.store import MAX_ON_DEMAND_ACTIONS, SnapshotStore

#: Gate H's delay grid, in hours after the shock. Scenario parameters, not
#: production timings.
DELAYS_HOURS: tuple[float, ...] = (0.0, 1.0, 6.0, 12.0, 24.0, 48.0, 72.0)
MAX_CANDIDATES = 12
ARTIFACT_KIND = "keystone-walk-speed-v1"
LABEL = "Replayed offline, on the snapshot's estimated network"
_LAST_INSTANT = 1e-6


def _shock(store: SnapshotStore, merchant_ids: Sequence[str], multiple: float,
           onset: float) -> Shock:
    components = [
        ShockComponent(
            merchant_id=m,
            magnitude=max(_liquidity_slack(store, m) * multiple, 1.0),
            t=onset,
            kind=ShockKind.CASH_WITHDRAWAL,
        )
        for m in sorted(set(merchant_ids))
    ]
    return Shock(
        name="on_demand",
        description=(
            f"{', '.join(c.merchant_id for c in components)} "
            f"{'lose' if len(components) > 1 else 'loses'} "
            f"INR {sum(c.magnitude for c in components):,.0f} of liquidity at t={onset:.0f}h"
        ),
        components=components,
    )


def _arm(shocked: Replay, treated: Replay, actions: Sequence[Intervention]) -> dict[str, Any]:
    prevented = shocked.disruption - treated.disruption
    return {
        "disruption_cut_pct": (100.0 * prevented / shocked.disruption
                               if shocked.disruption > 0 else 0.0),
        "payments_kept_on_time_inr": shocked.value_delayed - treated.value_delayed,
        "merchants_protected": max(0, shocked.n_affected - treated.n_affected),
        "cost_inr": sum(u.cost for u in actions),
        "actions": [
            {"action_type": str(u.type), "merchant_id": u.merchant_id,
             "amount_inr": u.amount, "lands_at_hours": u.t}
            for u in actions
        ],
    }


def build_walk_speed(
    store: SnapshotStore,
    *,
    merchant_ids: Sequence[str],
    magnitude_multiple: float,
    onset_hours: float = 0.0,
    delays: Sequence[float] = DELAYS_HOURS,
) -> dict[str, Any]:
    """Both arms on the delay grid for one on-demand shock. Returns the payload."""
    if not delays or delays[0] != 0.0 or list(delays) != sorted(set(delays)):
        raise ValidationError("delays must be strictly increasing and start at 0",
                              delays=list(delays))
    for m in merchant_ids:
        store.merchant(m)
    graph = store.graph
    sim = store.sim_config
    horizon = store.horizon_hours
    shock = _shock(store, merchant_ids, magnitude_multiple, onset_hours)
    shocked = replay(graph, shock, [], config=sim, run_id="adhoc:shocked")
    prediction = LinearThresholdPropagator(PropagationConfig(horizon_hours=horizon)).predict(
        graph, shock)

    def optimise(decision_time: float) -> list[Intervention]:
        if decision_time >= horizon - 1.0:
            return []
        constraints = InterventionConstraints(
            max_actions=MAX_ON_DEMAND_ACTIONS, horizon_hours=horizon,
            decision_time=decision_time,
        )
        action_set = generate_actions(graph, shock, prediction, constraints=constraints,
                                      max_candidates=MAX_CANDIDATES)
        solved = greedy_solve(
            CounterfactualEvaluator(graph=graph, shock=shock, config=sim),
            action_set.interventions, graph, constraints=constraints, objective=ObjectiveSpec(),
        )
        return list(solved.interventions)

    def treat(actions: Sequence[Intervention], run_id: str) -> Replay:
        return replay(graph, shock, list(actions), config=sim, run_id=run_id) if actions \
            else shocked

    chosen = optimise(onset_hours)
    rows = []
    for d in delays:
        moved = [u.model_copy(update={"t": min(u.t + d, horizon - _LAST_INSTANT)})
                 for u in chosen]
        replanned = chosen if d == 0 else optimise(onset_hours + d)
        rows.append({
            "delay_hours": d,
            "as_chosen": _arm(shocked, treat(moved, "adhoc:treated" if d == 0 else
                                             f"walk:f{d:g}"), moved),
            "replanned": _arm(shocked, treat(replanned, "adhoc:treated" if d == 0 else
                                             f"walk:r{d:g}"), replanned),
        })

    body = {
        "kind": ARTIFACT_KIND,
        "label": LABEL,
        "provenance": {
            "kind": "replayed_offline",
            "snapshot_id": store.snapshot_id,
            "code_version": __version__,
            "built_at": datetime.now(UTC).isoformat(),
        },
        "shock": {
            "merchant_ids": sorted(set(merchant_ids)),
            "magnitude_multiple": magnitude_multiple,
            "onset_hours": onset_hours,
            "description": shock.description,
        },
        "unplanned": {
            "payments_late_inr": shocked.value_delayed,
            "merchants_affected": shocked.n_affected,
        },
        "delays": rows,
    }
    return body


def _key(snapshot_id: str, merchant_ids: Sequence[str], multiple: float, onset: float) -> str:
    blob = json.dumps({"snapshot": snapshot_id, "merchants": sorted(set(merchant_ids)),
                       "multiple": multiple, "onset": onset}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def artifact_path(root: Path, snapshot_id: str, merchant_ids: Sequence[str], multiple: float,
                  onset: float) -> Path:
    return root / f"walk-{_key(snapshot_id, merchant_ids, multiple, onset)}" / "walk_speed.json"


def write_walk_speed(root: Path, payload: dict[str, Any]) -> Path:
    shock = payload["shock"]
    path = artifact_path(root, payload["provenance"]["snapshot_id"], shock["merchant_ids"],
                         shock["magnitude_multiple"], shock["onset_hours"])
    path.parent.mkdir(parents=True, exist_ok=True)
    content = {k: v for k, v in payload.items() if k != "content_hash"}
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    path.write_text(json.dumps({**content, "content_hash": digest}, indent=2), encoding="utf-8")
    return path


def read_walk_speed(root: Path, snapshot_id: str, merchant_ids: Sequence[str], multiple: float,
                    onset: float) -> dict[str, Any]:
    path = artifact_path(root, snapshot_id, merchant_ids, multiple, onset)
    if not path.is_file():
        raise NotFoundError("the cost of waiting has not been built for this walk; run "
                            "`lce walk build-speed`", merchant_ids=list(merchant_ids))
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    content = {k: v for k, v in data.items() if k != "content_hash"}
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if digest != data.get("content_hash"):
        raise ValidationError("the walk's cost-of-waiting artifact does not match its hash",
                              path=str(path))
    return data
