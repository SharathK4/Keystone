"""Loading a snapshot once, and answering questions from it.

The store holds one immutable snapshot in memory and serves reads from it. The
only operation that computes anything is :meth:`SnapshotStore.analyze`, and it is
bounded twice over - by a hard merchant ceiling and by a candidate cap - so no
request can start work the machine cannot finish.

What the store deliberately does not do
---------------------------------------
It does not generate networks, estimate dependency structure, sweep systemic
importance, or enumerate intervention subsets. All four are build-time
operations. A snapshot ships the *estimated* dependency overlay, so on-demand
analysis of a new shock runs the propagator and a bounded greedy search over the
true simulator and nothing else.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from lce.errors import NotFoundError, ValidationError
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.logging import get_logger
from lce.simulation.engine import SimulationConfig
from lce.snapshot.models import (
    SNAPSHOT_FORMAT_VERSION,
    DependencyView,
    MerchantView,
    NetworkOverview,
    Provenance,
    ScenarioSnapshot,
    ScenarioSummary,
    SnapshotManifest,
    SystemicRankingView,
)
from lce.snapshot.views import graph_from_payload, scenario_config

logger = get_logger(__name__)

SNAPSHOT_NAME = "snapshot.json"
MANIFEST_NAME = "manifest.json"

#: On-demand analysis is refused above this many merchants. The bound exists so
#: a request can never start a simulation sweep that a laptop cannot finish
#: inside a request timeout; larger networks are analysed at build time instead.
MAX_MERCHANTS_FOR_ON_DEMAND = 2_000

#: Candidates considered by an on-demand analysis, whatever the caller asks for.
MAX_ON_DEMAND_CANDIDATES = 12

#: Actions an on-demand plan may contain. Two keeps the greedy search at roughly
#: 2 x candidates simulations.
MAX_ON_DEMAND_ACTIONS = 2

#: Origin merchants one on-demand request may shock. Enforced by the request
#: model; declared here so ``analysis_limits`` can publish the same number the
#: API rejects on, rather than a client discovering it by getting a 422.
MAX_ON_DEMAND_ORIGINS = 5


def save_snapshot(payload: dict[str, Any], manifest: SnapshotManifest, directory: Path) -> Path:
    """Write a built snapshot and its manifest."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SNAPSHOT_NAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    (directory / MANIFEST_NAME).write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
    )
    return directory


def list_snapshots(root: Path) -> list[dict[str, Any]]:
    root = Path(root)
    if not root.exists():
        return []
    found = []
    for path in sorted(root.glob("*/" + MANIFEST_NAME)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        found.append(
            {
                "path": str(path.parent),
                "snapshot_id": payload.get("snapshot_id"),
                "dataset_version": payload.get("dataset_version"),
                "scale": payload.get("scale"),
                "seed": payload.get("seed"),
                "n_scenarios": payload.get("n_scenarios"),
                "created_at": payload.get("created_at"),
            }
        )
    return sorted(found, key=lambda r: r.get("created_at") or "", reverse=True)


def resolve_snapshot(root: Path, snapshot_id: str | None = None) -> Path:
    entries = list_snapshots(root)
    if not entries:
        raise NotFoundError(f"no analytical snapshots under {root}", path=str(root))
    if snapshot_id is None:
        return Path(entries[0]["path"])
    for entry in entries:
        if entry.get("snapshot_id") == snapshot_id:
            return Path(entry["path"])
    raise NotFoundError(
        f"no snapshot with id {snapshot_id!r} under {root}",
        available=[e.get("snapshot_id") for e in entries],
    )


class SnapshotStore:
    """One loaded snapshot, read-only, plus bounded on-demand analysis."""

    def __init__(self, directory: Path) -> None:
        started = time.perf_counter()
        directory = Path(directory)
        snapshot_path = directory / SNAPSHOT_NAME
        if not snapshot_path.exists():
            raise NotFoundError(
                f"no {SNAPSHOT_NAME} at {directory}", path=str(directory)
            )

        self.directory = directory
        self.payload: dict[str, Any] = json.loads(
            snapshot_path.read_text(encoding="utf-8")
        )
        if self.payload.get("format_version") != SNAPSHOT_FORMAT_VERSION:
            raise ValidationError(
                f"snapshot format {self.payload.get('format_version')!r} is not "
                f"supported (this build reads {SNAPSHOT_FORMAT_VERSION})",
                path=str(directory),
            )

        manifest_path = directory / MANIFEST_NAME
        self.manifest = SnapshotManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        self.provenance = Provenance.model_validate(self.payload["provenance"])

        self._graph: TemporalPaymentGraph = graph_from_payload(
            self.payload["serving_graph"]
        )
        self._sim_config, self.horizon_hours = scenario_config(self.payload)
        self._scenarios: dict[str, ScenarioSnapshot] = {
            s["scenario_id"]: ScenarioSnapshot.model_validate(s)
            for s in self.payload["scenarios"]
        }
        self._merchants: dict[str, MerchantView] = {
            m["merchant_id"]: MerchantView.model_validate(m)
            for m in self.payload["merchants"]
        }
        self._lock = threading.Lock()
        self._analysis_cache: dict[str, ScenarioSnapshot] = {}
        self.load_ms = (time.perf_counter() - started) * 1000.0

        logger.info(
            "snapshot_loaded",
            snapshot_id=self.manifest.snapshot_id,
            n_merchants=len(self._merchants),
            n_scenarios=len(self._scenarios),
            load_ms=round(self.load_ms, 2),
        )

    # ------------------------------------------------------------------ reads

    @property
    def snapshot_id(self) -> str:
        return self.manifest.snapshot_id

    @property
    def graph(self) -> TemporalPaymentGraph:
        return self._graph

    @property
    def sim_config(self) -> SimulationConfig:
        return self._sim_config

    def network(self) -> NetworkOverview:
        return NetworkOverview.model_validate(self.payload["network"])

    def merchants(self) -> list[MerchantView]:
        return list(self._merchants.values())

    def merchant(self, merchant_id: str) -> MerchantView:
        try:
            return self._merchants[merchant_id]
        except KeyError as exc:
            raise NotFoundError(
                f"unknown merchant {merchant_id!r}", merchant_id=merchant_id
            ) from exc

    def dependencies(self) -> list[DependencyView]:
        return [DependencyView.model_validate(d) for d in self.payload["dependencies"]]

    def dependencies_for(self, merchant_id: str) -> dict[str, list[DependencyView]]:
        """Relationships this merchant is on either side of."""
        self.merchant(merchant_id)  # raises when unknown
        edges = self.dependencies()
        return {
            "downstream": [e for e in edges if e.source_id == merchant_id],
            "upstream": [e for e in edges if e.target_id == merchant_id],
        }

    def systemic(self) -> SystemicRankingView:
        return SystemicRankingView.model_validate(self.payload["systemic"])

    def scenarios(self) -> list[ScenarioSnapshot]:
        return list(self._scenarios.values())

    def scenario(self, scenario_id: str) -> ScenarioSnapshot:
        found = self._scenarios.get(scenario_id) or self._analysis_cache.get(scenario_id)
        if found is None:
            raise NotFoundError(
                f"unknown scenario {scenario_id!r}", scenario_id=scenario_id
            )
        return found

    def scenario_summaries(self) -> list[ScenarioSummary]:
        from lce.snapshot.build import summarise_scenario

        return [summarise_scenario(s) for s in self.scenarios()]

    def offer(self, merchant_id: str) -> Any:
        """The most valuable offer recommended for a merchant, across scenarios.

        Offers are scenario-specific by construction - an amount only means
        something relative to the shock it was sized against - so when several
        scenarios recommend one for the same merchant, the one preventing the
        most disruption is returned and the rest are reachable through their own
        scenarios.
        """
        self.merchant(merchant_id)
        offers = [
            s.offer
            for s in self.scenarios()
            if s.offer is not None and s.offer.merchant_id == merchant_id
        ]
        if not offers:
            raise NotFoundError(
                f"no intervention was recommended for {merchant_id!r} in this snapshot",
                merchant_id=merchant_id,
            )
        return max(
            offers, key=lambda o: o.expected_network_benefit.get("disruption_prevented", 0.0)
        )

    def offers(self) -> list[Any]:
        return [s.offer for s in self.scenarios() if s.offer is not None]

    # ------------------------------------------------------- bounded analysis

    def analysis_limits(self) -> dict[str, int]:
        return {
            "max_merchants": MAX_MERCHANTS_FOR_ON_DEMAND,
            "max_shocked_merchants": MAX_ON_DEMAND_ORIGINS,
            "max_candidates": MAX_ON_DEMAND_CANDIDATES,
            "max_actions": MAX_ON_DEMAND_ACTIONS,
            "n_merchants": len(self._merchants),
        }

    def analyze(
        self,
        *,
        merchant_ids: list[str],
        magnitude_multiple: float = 2.0,
        onset_hours: float = 0.0,
        max_actions: int = MAX_ON_DEMAND_ACTIONS,
    ) -> ScenarioSnapshot:
        """Analyse a shock the caller chose, inside fixed bounds.

        Refuses rather than degrading when the network is too large: an API that
        silently returns a worse answer under load is harder to reason about than
        one that says the request is out of scope.
        """
        if len(self._merchants) > MAX_MERCHANTS_FOR_ON_DEMAND:
            raise ValidationError(
                f"on-demand analysis is limited to {MAX_MERCHANTS_FOR_ON_DEMAND:,} "
                f"merchants; this snapshot holds {len(self._merchants):,}. Build a "
                "snapshot with the scenario included instead.",
                n_merchants=len(self._merchants),
            )
        if not merchant_ids:
            raise ValidationError("a shock needs at least one origin merchant")
        for merchant_id in merchant_ids:
            self.merchant(merchant_id)
        if not 0.0 <= onset_hours < self.horizon_hours:
            raise ValidationError(
                f"onset_hours must lie in [0, {self.horizon_hours})",
                onset_hours=onset_hours,
            )

        from lce.snapshot.analysis import analyse_shock

        with self._lock:  # one analysis at a time keeps the CPU budget predictable
            snapshot = analyse_shock(
                self,
                merchant_ids=merchant_ids,
                magnitude_multiple=magnitude_multiple,
                onset_hours=onset_hours,
                max_actions=min(max_actions, MAX_ON_DEMAND_ACTIONS),
            )
            self._analysis_cache[snapshot.scenario_id] = snapshot
        return snapshot

    # ------------------------------------------------------------------ meta

    def health(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.manifest.snapshot_id,
            "format_version": self.payload["format_version"],
            "dataset_version": self.manifest.dataset_version,
            "scale": self.manifest.scale,
            "seed": self.manifest.seed,
            "n_merchants": len(self._merchants),
            "n_scenarios": len(self._scenarios),
            "content_hash": self.manifest.content_hash[:16],
            "created_at": self.manifest.created_at,
            "code_version": self.manifest.code_version,
            "load_ms": round(self.load_ms, 2),
            "limits": self.analysis_limits(),
            "path": str(self.directory),
        }


_STORE: SnapshotStore | None = None


def get_store(root: Path | None = None, *, snapshot_id: str | None = None) -> SnapshotStore:
    """Process-wide singleton, built on first use.

    Loading a snapshot parses a few megabytes of JSON and rebuilds a graph; doing
    it per request would put that on every call for an object that never changes.
    """
    global _STORE
    if _STORE is None:
        from lce.config import get_settings

        base = Path(root) if root else get_settings().model_artifact_dir / "snapshots"
        _STORE = SnapshotStore(resolve_snapshot(base, snapshot_id))
    return _STORE


def reset_store() -> None:
    """Drop the cached store. Tests use it; production should not need to."""
    global _STORE
    _STORE = None


def require_store() -> SnapshotStore:
    """API dependency: a loaded store, or a clear error explaining why not."""
    try:
        return get_store()
    except (NotFoundError, ValidationError) as exc:
        raise NotFoundError(
            "no analytical snapshot is available; build one with "
            "'lce snapshot build' before starting the API",
            reason=exc.message,
        ) from exc
