"""Network lifecycle: generate, persist, load.

The service layer owns transactions and orchestration; repositories own queries
and the domain owns the maths. Nothing above this layer touches a
:class:`~sqlalchemy.orm.Session`.

Graph caching
-------------
Rehydrating a graph means reading every payment event, which for a realistic
dataset is 10^5 rows. The API would otherwise pay that on every request, so
loaded graphs are memoised per dataset. The cache is keyed on dataset version,
which is content-addressed - a changed dataset is a different key by
construction, so the cache cannot go stale.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the generator is a build-time dependency, not a serving one
    from lce.data.generator import GeneratorConfig, SyntheticNetwork
from lce.data.unit_of_work import UnitOfWork
from lce.domain.enums import RunKind
from lce.errors import NotFoundError
from lce.experiments.tracker import RunTracker
from lce.graph.builders import build_graph
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.logging import get_logger

logger = get_logger(__name__)

MAX_CACHED_GRAPHS = 4


class _GraphCache:
    """Tiny LRU over rehydrated graphs."""

    def __init__(self, capacity: int = MAX_CACHED_GRAPHS) -> None:
        self._items: OrderedDict[str, TemporalPaymentGraph] = OrderedDict()
        self.capacity = capacity

    def get(self, key: str) -> TemporalPaymentGraph | None:
        graph = self._items.get(key)
        if graph is not None:
            self._items.move_to_end(key)
        return graph

    def put(self, key: str, graph: TemporalPaymentGraph) -> None:
        self._items[key] = graph
        self._items.move_to_end(key)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._items.clear()
        else:
            self._items.pop(key, None)


_GRAPH_CACHE = _GraphCache()


class NetworkService:
    """Creates, stores and loads merchant networks."""

    def __init__(self, uow: UnitOfWork, tracker: RunTracker | None = None) -> None:
        self.uow = uow
        self.tracker = tracker or RunTracker(uow=uow, persist_db=True)

    # -------------------------------------------------------------- generate

    def generate_and_store(
        self, config: GeneratorConfig | None = None, *, notes: str = ""
    ) -> SyntheticNetwork:
        """Generate a synthetic network and persist it in one transaction."""
        from lce.data.generator import GeneratorConfig, NetworkGenerator

        cfg = config or GeneratorConfig()
        existing = self.uow.datasets.by_version(cfg.dataset_version)
        if existing is not None:
            raise ValueError(
                f"dataset {cfg.dataset_version} already exists; "
                "it is content-addressed, so regenerating it would be a no-op"
            )

        with self.tracker.run(
            RunKind.GENERATION,
            name="generate_network",
            dataset_version=cfg.dataset_version,
            seed=cfg.seed,
            config=cfg.to_dict(),
        ) as record:
            network = NetworkGenerator(cfg).generate()
            self.store(network, notes=notes)
            record.metrics = {"n_merchants": len(network.graph), **network.stats}
            return network

    def store(self, network: SyntheticNetwork, *, notes: str = "") -> str:
        """Persist a generated network. Caller owns the commit."""
        graph = network.graph
        dataset_id = network.dataset_version

        self.uow.datasets.create(
            dataset_id=dataset_id,
            dataset_version=dataset_id,
            source="synthetic",
            seed=network.config.seed,
            config=network.config.to_dict(),
            stats=network.stats,
            notes=notes,
        )
        self.uow.flush()

        self.uow.merchants.save_many(list(graph.merchants.values()), dataset_id)
        self.uow.flush()
        self.uow.payments.save_many(graph.payment_events, dataset_id)
        self.uow.obligations.save_many(graph.obligations, dataset_id)
        self.uow.edges.save_many(graph.dependency_edges, dataset_id, model_version="truth")
        self.uow.commit()

        _GRAPH_CACHE.put(dataset_id, graph)
        logger.info(
            "network_stored",
            dataset_version=dataset_id,
            n_merchants=len(graph),
            n_events=graph.stats().n_payment_events,
        )
        return dataset_id

    # ------------------------------------------------------------------ load

    def load_graph(
        self,
        dataset_id: str,
        *,
        estimator: str | None = None,
        use_cache: bool = True,
    ) -> TemporalPaymentGraph:
        """Rehydrate a stored network.

        ``estimator`` selects which dependency overlay to install. ``None`` uses
        the ground-truth edges when present, which is right for the demo but
        *wrong* for honest evaluation - pass the learner's name there.
        """
        cache_key = f"{dataset_id}:{estimator or 'default'}"
        if use_cache:
            cached = _GRAPH_CACHE.get(cache_key)
            if cached is not None:
                return cached

        dataset = self.uow.datasets.get(dataset_id)
        if dataset is None:
            raise NotFoundError(f"unknown dataset {dataset_id!r}", dataset_id=dataset_id)

        merchants = self.uow.merchants.list_for_dataset(dataset_id)
        payments = self.uow.payments.list_for_dataset(dataset_id)
        obligations = self.uow.obligations.list_for_dataset(dataset_id)
        edges = self.uow.edges.list_for_dataset(dataset_id, estimator=estimator)

        graph = build_graph(
            merchants,
            payments,
            obligations,
            edges,
            network_id=dataset_id,
            dataset_version=dataset.dataset_version,
            epoch_iso=dataset.epoch.isoformat() if dataset.epoch else None,
        )
        if use_cache:
            _GRAPH_CACHE.put(cache_key, graph)
        return graph

    def list_datasets(self, limit: int = 25) -> list[dict[str, Any]]:
        return [
            {
                "dataset_id": row.id,
                "dataset_version": row.dataset_version,
                "source": row.source,
                "seed": row.seed,
                "stats": row.stats,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "notes": row.notes,
            }
            for row in self.uow.datasets.list_recent(limit)
        ]

    def dataset_summary(self, dataset_id: str) -> dict[str, Any]:
        dataset = self.uow.datasets.require(dataset_id)
        graph = self.load_graph(dataset_id)
        return {
            "dataset_id": dataset.id,
            "dataset_version": dataset.dataset_version,
            "source": dataset.source,
            "seed": dataset.seed,
            "config": dataset.config,
            "stats": dataset.stats,
            "graph": graph.stats().to_dict(),
        }

    @staticmethod
    def invalidate_cache(dataset_id: str | None = None) -> None:
        _GRAPH_CACHE.invalidate(dataset_id)
