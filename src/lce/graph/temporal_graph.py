"""The temporal payment-dependency graph.

Structure
---------
This is a **temporal directed multigraph**, not an adjacency matrix. Concretely:

* Nodes carry a :class:`~lce.domain.merchant.MerchantProfile`.
* Every :class:`~lce.domain.events.PaymentEvent` is its own parallel edge,
  keyed by event id and stamped with ``t``. Two merchants that transact 400
  times have 400 edges between them, and every one of those timestamps stays
  queryable. Nothing is summed away.
* A second, *overlay* layer holds the learned
  :class:`~lce.domain.edges.DependencyEdge` per ordered pair - the behavioural
  summary (amount law, recurrence, lag law, reliability, pass-through). The
  overlay is derived from the event layer and can be recomputed at any time;
  it never replaces it.
* Obligations are held in their own index, keyed by id and by debtor/creditor,
  because contagion propagates along *commitments*, not along past payments.

The static adjacency view (:meth:`aggregate_matrix`) exists only as an input to
structural centrality measures. It is explicitly a lossy projection and is
never used by the simulator or the propagation model.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import networkx as nx
import numpy as np

from lce.domain.edges import DependencyEdge
from lce.domain.events import EXTERNAL_SINK, Obligation, PaymentEvent
from lce.domain.merchant import MerchantProfile
from lce.errors import GraphError, NotFoundError

# Edge-layer discriminator stored on every multigraph edge.
LAYER_EVENT = "event"
LAYER_DEPENDENCY = "dependency"


@dataclass(slots=True)
class GraphStats:
    """Cheap structural summary, for health checks and the API."""

    n_merchants: int
    n_payment_events: int
    n_obligations: int
    n_dependency_edges: int
    n_distinct_pairs: int
    t_min: float | None
    t_max: float | None
    total_payment_value: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_merchants": self.n_merchants,
            "n_payment_events": self.n_payment_events,
            "n_obligations": self.n_obligations,
            "n_dependency_edges": self.n_dependency_edges,
            "n_distinct_pairs": self.n_distinct_pairs,
            "t_min": self.t_min,
            "t_max": self.t_max,
            "total_payment_value": self.total_payment_value,
        }


@dataclass(slots=True)
class TemporalPaymentGraph:
    """Event-level temporal payment network.

    Not a Pydantic model: this is a mutable working structure that the
    generator, simulator and learners all operate on. It serialises through
    :meth:`to_payload` / :meth:`from_payload`.
    """

    network_id: str = "default"
    dataset_version: str | None = None
    epoch_iso: str | None = None

    _g: nx.MultiDiGraph = field(default_factory=nx.MultiDiGraph, repr=False)
    _profiles: dict[str, MerchantProfile] = field(default_factory=dict, repr=False)
    _events: dict[str, PaymentEvent] = field(default_factory=dict, repr=False)
    _obligations: dict[str, Obligation] = field(default_factory=dict, repr=False)
    _dependency: dict[tuple[str, str], DependencyEdge] = field(default_factory=dict, repr=False)
    _by_payer: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list), repr=False
    )
    _by_payee: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list), repr=False
    )
    _obl_by_debtor: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list), repr=False
    )
    _obl_by_creditor: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list), repr=False
    )
    _events_sorted: bool = field(default=True, repr=False)

    # ------------------------------------------------------------------ nodes

    def add_merchant(self, profile: MerchantProfile) -> None:
        """Insert or replace a merchant node."""
        self._profiles[profile.merchant_id] = profile
        self._g.add_node(profile.merchant_id, profile=profile)

    def add_merchants(self, profiles: Iterable[MerchantProfile]) -> None:
        for p in profiles:
            self.add_merchant(p)

    def merchant(self, merchant_id: str) -> MerchantProfile:
        try:
            return self._profiles[merchant_id]
        except KeyError as exc:
            raise NotFoundError(
                f"unknown merchant {merchant_id!r}", merchant_id=merchant_id
            ) from exc

    def has_merchant(self, merchant_id: str) -> bool:
        return merchant_id in self._profiles

    @property
    def merchants(self) -> dict[str, MerchantProfile]:
        return dict(self._profiles)

    @property
    def merchant_ids(self) -> list[str]:
        return list(self._profiles)

    def __len__(self) -> int:
        return len(self._profiles)

    # ----------------------------------------------------------- event layer

    def add_payment(self, event: PaymentEvent, *, require_nodes: bool = True) -> None:
        """Append one payment event as its own parallel edge."""
        if require_nodes:
            for node in (event.payer_id, event.payee_id):
                if node != EXTERNAL_SINK and node not in self._profiles:
                    raise GraphError(
                        f"payment references unknown merchant {node!r}", merchant_id=node
                    )
        if event.event_id in self._events:
            raise GraphError(f"duplicate payment event {event.event_id}")

        self._events[event.event_id] = event
        self._by_payer[event.payer_id].append(event.event_id)
        self._by_payee[event.payee_id].append(event.event_id)
        self._g.add_edge(
            event.payer_id,
            event.payee_id,
            key=event.event_id,
            layer=LAYER_EVENT,
            t=event.t,
            amount=event.amount,
            obligation_id=event.obligation_id,
            channel=str(event.channel),
        )
        self._events_sorted = False

    def add_payments(self, events: Iterable[PaymentEvent], *, require_nodes: bool = True) -> None:
        for e in events:
            self.add_payment(e, require_nodes=require_nodes)

    @property
    def payment_events(self) -> list[PaymentEvent]:
        """All events in chronological order."""
        self._ensure_sorted()
        return list(self._events.values())

    def _ensure_sorted(self) -> None:
        if self._events_sorted:
            return
        ordered = sorted(self._events.values(), key=lambda e: (e.t, e.event_id))
        self._events = {e.event_id: e for e in ordered}
        for bucket in (self._by_payer, self._by_payee):
            for key, ids in bucket.items():
                bucket[key] = sorted(ids, key=lambda i: (self._events[i].t, i))
        self._events_sorted = True

    def events_between(
        self, t0: float, t1: float, *, inclusive_end: bool = False
    ) -> list[PaymentEvent]:
        """Events with ``t0 <= t < t1`` (or ``<= t1`` when ``inclusive_end``)."""
        self._ensure_sorted()
        return [
            e
            for e in self._events.values()
            if e.t >= t0 and (e.t <= t1 if inclusive_end else e.t < t1)
        ]

    def events_on_edge(self, source: str, target: str) -> list[PaymentEvent]:
        """Every payment ``source -> target``, chronologically. The raw edge history."""
        self._ensure_sorted()
        return [
            self._events[eid]
            for eid in self._by_payer.get(source, [])
            if self._events[eid].payee_id == target
        ]

    def outbound_events(self, merchant_id: str) -> list[PaymentEvent]:
        self._ensure_sorted()
        return [self._events[eid] for eid in self._by_payer.get(merchant_id, [])]

    def inbound_events(self, merchant_id: str) -> list[PaymentEvent]:
        self._ensure_sorted()
        return [self._events[eid] for eid in self._by_payee.get(merchant_id, [])]

    def event(self, event_id: str) -> PaymentEvent:
        try:
            return self._events[event_id]
        except KeyError as exc:
            raise NotFoundError(f"unknown payment event {event_id!r}") from exc

    def distinct_pairs(self) -> list[tuple[str, str]]:
        """Ordered pairs that ever transacted."""
        return sorted({(e.payer_id, e.payee_id) for e in self._events.values()})

    # ------------------------------------------------------ obligation layer

    def add_obligation(self, obligation: Obligation, *, require_nodes: bool = True) -> None:
        if require_nodes:
            for node in (obligation.debtor_id, obligation.creditor_id):
                if node != EXTERNAL_SINK and node not in self._profiles:
                    raise GraphError(
                        f"obligation references unknown merchant {node!r}", merchant_id=node
                    )
        known = obligation.obligation_id in self._obligations
        self._obligations[obligation.obligation_id] = obligation
        if not known:
            self._obl_by_debtor[obligation.debtor_id].append(obligation.obligation_id)
            self._obl_by_creditor[obligation.creditor_id].append(obligation.obligation_id)

    def add_obligations(self, obligations: Iterable[Obligation], **kw: Any) -> None:
        for o in obligations:
            self.add_obligation(o, **kw)

    def obligation(self, obligation_id: str) -> Obligation:
        try:
            return self._obligations[obligation_id]
        except KeyError as exc:
            raise NotFoundError(f"unknown obligation {obligation_id!r}") from exc

    @property
    def obligations(self) -> list[Obligation]:
        return sorted(self._obligations.values(), key=lambda o: (o.due_t, o.obligation_id))

    def payables_of(self, merchant_id: str) -> list[Obligation]:
        """Obligations this merchant owes (its outflow commitments)."""
        return sorted(
            (self._obligations[i] for i in self._obl_by_debtor.get(merchant_id, [])),
            key=lambda o: (o.due_t, o.obligation_id),
        )

    def receivables_of(self, merchant_id: str) -> list[Obligation]:
        """Obligations owed to this merchant (its inflow expectations)."""
        return sorted(
            (self._obligations[i] for i in self._obl_by_creditor.get(merchant_id, [])),
            key=lambda o: (o.due_t, o.obligation_id),
        )

    def obligations_due_between(self, t0: float, t1: float) -> list[Obligation]:
        return sorted(
            (o for o in self._obligations.values() if t0 <= o.due_t < t1),
            key=lambda o: (o.due_t, o.obligation_id),
        )

    # ----------------------------------------------------- dependency overlay

    def set_dependency(self, edge: DependencyEdge) -> None:
        """Install or replace the learned behavioural edge for an ordered pair."""
        for node in edge.key:
            if node not in self._profiles:
                raise GraphError(f"dependency edge references unknown merchant {node!r}")
        self._dependency[edge.key] = edge

    def set_dependencies(self, edges: Iterable[DependencyEdge]) -> None:
        for e in edges:
            self.set_dependency(e)

    def dependency(self, source: str, target: str) -> DependencyEdge | None:
        return self._dependency.get((source, target))

    @property
    def dependency_edges(self) -> list[DependencyEdge]:
        return [self._dependency[k] for k in sorted(self._dependency)]

    def successors(self, merchant_id: str) -> list[str]:
        """Merchants this node pays - i.e. those it can starve when squeezed."""
        return sorted({t for (s, t) in self._dependency if s == merchant_id})

    def predecessors(self, merchant_id: str) -> list[str]:
        """Merchants that pay this node - its sources of inflow."""
        return sorted({s for (s, t) in self._dependency if t == merchant_id})

    def out_dependencies(self, merchant_id: str) -> list[DependencyEdge]:
        return [self._dependency[k] for k in sorted(self._dependency) if k[0] == merchant_id]

    def in_dependencies(self, merchant_id: str) -> list[DependencyEdge]:
        return [self._dependency[k] for k in sorted(self._dependency) if k[1] == merchant_id]

    def clear_dependencies(self) -> None:
        self._dependency.clear()

    # ------------------------------------------------------------- projections

    def dependency_graph(self, *, weight: str = "pass_through") -> nx.DiGraph:
        """Simple directed graph over the dependency overlay, for path queries.

        Lossy by construction - only use it for structural questions
        (reachability, centrality), never for propagation timing.
        """
        dg = nx.DiGraph()
        dg.add_nodes_from(self._profiles)
        for (s, t), edge in self._dependency.items():
            w = {
                "pass_through": edge.pass_through,
                "reliability": edge.reliability,
                "amount": edge.features.mean_amount,
                "conditional_probability": edge.conditional_probability,
            }.get(weight, edge.pass_through)
            dg.add_edge(s, t, weight=w, lag_hours=edge.lag.mean_hours)
        return dg

    def aggregate_matrix(
        self, t0: float | None = None, t1: float | None = None
    ) -> tuple[list[str], np.ndarray]:
        """Lossy value-flow matrix over a window. Structural analysis only."""
        ids = self.merchant_ids
        index = {m: k for k, m in enumerate(ids)}
        mat = np.zeros((len(ids), len(ids)), dtype=float)
        for e in self._events.values():
            if t0 is not None and e.t < t0:
                continue
            if t1 is not None and e.t >= t1:
                continue
            if e.payer_id in index and e.payee_id in index:
                mat[index[e.payer_id], index[e.payee_id]] += e.amount
        return ids, mat

    def structural_centrality(self, *, alpha: float = 0.1) -> dict[str, float]:
        """Katz centrality on the pass-through-weighted dependency graph.

        Answers 'how much of the network sits downstream of this node', which is
        the structural half of systemic importance. The simulated half comes
        from :func:`lce.domain.objectives.systemic_importance`.
        """
        dg = self.dependency_graph(weight="pass_through")
        if dg.number_of_nodes() == 0:
            return {}
        try:
            return dict(nx.katz_centrality(dg, alpha=alpha, max_iter=1000, tol=1e-6))
        except (nx.PowerIterationFailedConvergence, nx.NetworkXError):
            # Fall back to a spectrally safe alpha derived from the graph itself.
            return dict(nx.katz_centrality_numpy(dg, alpha=_safe_alpha(dg)))

    def descendants_within(self, source: str, max_hops: int) -> dict[str, int]:
        """Nodes reachable from ``source`` within ``max_hops``, mapped to hop count."""
        dg = self.dependency_graph()
        if source not in dg:
            raise NotFoundError(f"unknown merchant {source!r}")
        return {
            node: hops
            for node, hops in nx.single_source_shortest_path_length(
                dg, source, cutoff=max_hops
            ).items()
            if node != source
        }

    # ------------------------------------------------------------------ misc

    def stats(self) -> GraphStats:
        times = [e.t for e in self._events.values()]
        return GraphStats(
            n_merchants=len(self._profiles),
            n_payment_events=len(self._events),
            n_obligations=len(self._obligations),
            n_dependency_edges=len(self._dependency),
            n_distinct_pairs=len(self.distinct_pairs()),
            t_min=min(times) if times else None,
            t_max=max(times) if times else None,
            total_payment_value=float(sum(e.amount for e in self._events.values())),
        )

    def iter_edges(self) -> Iterator[tuple[str, str, str, dict[str, Any]]]:
        """Raw multigraph edge iteration: ``(source, target, key, attrs)``."""
        yield from self._g.edges(keys=True, data=True)

    def to_payload(self) -> dict[str, Any]:
        """Round-trippable serialisation of the whole graph."""
        return {
            "network_id": self.network_id,
            "dataset_version": self.dataset_version,
            "epoch_iso": self.epoch_iso,
            "merchants": [p.to_json_dict() for p in self._profiles.values()],
            "payments": [e.to_json_dict() for e in self.payment_events],
            "obligations": [o.to_json_dict() for o in self.obligations],
            "dependencies": [d.to_json_dict() for d in self.dependency_edges],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TemporalPaymentGraph:
        g = cls(
            network_id=payload.get("network_id", "default"),
            dataset_version=payload.get("dataset_version"),
            epoch_iso=payload.get("epoch_iso"),
        )
        g.add_merchants(MerchantProfile.model_validate(m) for m in payload.get("merchants", []))
        g.add_payments(PaymentEvent.model_validate(e) for e in payload.get("payments", []))
        g.add_obligations(Obligation.model_validate(o) for o in payload.get("obligations", []))
        g.set_dependencies(
            DependencyEdge.model_validate(d) for d in payload.get("dependencies", [])
        )
        return g

    def copy(self) -> TemporalPaymentGraph:
        """Deep-enough copy: domain objects are frozen, so indices alone are rebuilt."""
        return TemporalPaymentGraph.from_payload(self.to_payload())


def _safe_alpha(dg: nx.DiGraph) -> float:
    """Largest-eigenvalue-safe Katz alpha for a graph."""
    if dg.number_of_edges() == 0:
        return 0.1
    adj = nx.to_numpy_array(dg, weight="weight")
    spectral = float(np.max(np.abs(np.linalg.eigvals(adj))))
    if spectral <= 0:
        return 0.1
    return 0.9 / spectral
