"""Assembling :class:`TemporalPaymentGraph` instances from parts."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from lce.domain.edges import DependencyEdge
from lce.domain.events import Obligation, PaymentEvent
from lce.domain.merchant import MerchantProfile
from lce.graph.temporal_graph import TemporalPaymentGraph


def build_graph(
    merchants: Sequence[MerchantProfile],
    payments: Iterable[PaymentEvent] = (),
    obligations: Iterable[Obligation] = (),
    dependencies: Iterable[DependencyEdge] = (),
    *,
    network_id: str = "default",
    dataset_version: str | None = None,
    epoch_iso: str | None = None,
) -> TemporalPaymentGraph:
    """Construct a graph from already-materialised domain objects."""
    graph = TemporalPaymentGraph(
        network_id=network_id, dataset_version=dataset_version, epoch_iso=epoch_iso
    )
    graph.add_merchants(merchants)
    graph.add_payments(payments)
    graph.add_obligations(obligations)
    graph.set_dependencies(dependencies)
    return graph


def subgraph_around(
    graph: TemporalPaymentGraph,
    center: str,
    hops: int = 2,
    *,
    include_upstream: bool = True,
) -> TemporalPaymentGraph:
    """Ego network around ``center`` out to ``hops`` dependency hops.

    Used to keep the API's network views bounded on large ecosystems. Keeps the
    full event history for the retained nodes - the subgraph is a node filter,
    not a temporal one.
    """
    keep: set[str] = {center}
    keep |= set(graph.descendants_within(center, hops))
    if include_upstream:
        reverse = graph.dependency_graph().reverse(copy=True)
        import networkx as nx

        keep |= {
            n
            for n in nx.single_source_shortest_path_length(reverse, center, cutoff=hops)
            if n != center
        }

    sub = TemporalPaymentGraph(
        network_id=f"{graph.network_id}:ego({center},{hops})",
        dataset_version=graph.dataset_version,
        epoch_iso=graph.epoch_iso,
    )
    sub.add_merchants(p for m, p in graph.merchants.items() if m in keep)
    sub.add_payments(
        e for e in graph.payment_events if e.payer_id in keep and e.payee_id in keep
    )
    sub.add_obligations(
        o for o in graph.obligations if o.debtor_id in keep and o.creditor_id in keep
    )
    sub.set_dependencies(
        d for d in graph.dependency_edges if d.source_id in keep and d.target_id in keep
    )
    return sub
