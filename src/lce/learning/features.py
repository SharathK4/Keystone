"""Leak-free features, computed only from what :mod:`lce.learning.problem` allows.

Every column here is a function of an :class:`~lce.learning.problem.ObservedWindow`
and nothing else. That is the entire discipline: the builders never touch the
scenario's mutated graph, never read a dependency edge, never see a payment at or
after the origin, and never consult a latent cash-process parameter.
:func:`~lce.learning.problem.audit_leakage` checks it mechanically.

Scaling
-------
Monetary quantities enter twice: as ``log1p`` of the rupee amount, and as a ratio
to the node's own opening buffer. The ratios are what generalise. A benchmark
network spans four orders of magnitude in merchant size, so a raw amount teaches
a model to recognise *large* merchants; the same amount divided by the buffer it
has to be paid out of teaches it to recognise *exposed* ones, which is the actual
question.

Feature groups
--------------
Columns are tagged with a group so ablations can drop a whole family at once
rather than guessing at column indices:

``balance_sheet``  opening balance, credit line, operating floor (a disclosure)
``book``           the obligation book at the origin: what is owed, to whom, when
``history``        the observed payment stream: volume, cadence, concentration
``structure``      position in the *observed* payment graph, not the latent one
``shock``          the operator's trigger: who was hit, how hard, how far away
``context``        sector and size band

Time-varying columns
--------------------
The discrete-time hazard model needs covariates that change across intervals, or
its baseline hazard is doing all the work. :func:`build_interval_features`
supplies the honest ones: how much of the node's book falls due *inside* interval
``k``, and how much has fallen due by the end of it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import networkx as nx
import numpy as np

from lce.domain.enums import MerchantSector, MerchantTier
from lce.domain.events import EXTERNAL_SINK, Obligation, PaymentEvent
from lce.learning.problem import ObservedWindow

FEATURE_GROUPS: tuple[str, ...] = (
    "balance_sheet",
    "book",
    "history",
    "structure",
    "shock",
    "context",
)

#: Hops of value-weighted diffusion used for the upstream-exposure feature. Three
#: is the depth at which the benchmark's cascades actually stop; going deeper
#: mostly re-ranks nodes that are already saturated.
EXPOSURE_HOPS = 3

_SECTORS = tuple(MerchantSector)
_TIERS = (
    MerchantTier.MICRO,
    MerchantTier.SMALL,
    MerchantTier.MEDIUM,
    MerchantTier.LARGE,
    MerchantTier.ANCHOR,
)


def _log(x: float) -> float:
    return math.log1p(max(0.0, float(x)))


def _ratio(numerator: float, denominator: float, cap: float = 50.0) -> float:
    """Bounded ratio. Capped because an unbounded one dominates every linear fit."""
    return float(min(max(numerator, 0.0) / max(denominator, 1.0), cap))


def _hhi(values: Sequence[float]) -> float:
    """Herfindahl concentration of a value split. 1.0 = one counterparty."""
    total = float(sum(values))
    if total <= 0.0:
        return 0.0
    return float(sum((v / total) ** 2 for v in values))


@dataclass(slots=True)
class ObservedStats:
    """Once-per-window summaries shared by every builder.

    Recomputing these per feature would make the builder quadratic in the number
    of columns for no reason; they are cheap to derive and every group wants
    them.
    """

    window: ObservedWindow
    merchant_ids: list[str]
    index: dict[str, int]

    inflow_events: dict[str, list[PaymentEvent]] = field(default_factory=dict)
    outflow_events: dict[str, list[PaymentEvent]] = field(default_factory=dict)
    pair_value: dict[tuple[str, str], float] = field(default_factory=dict)
    pair_events: dict[tuple[str, str], list[PaymentEvent]] = field(default_factory=dict)
    payables: dict[str, list[Obligation]] = field(default_factory=dict)
    receivables: dict[str, list[Obligation]] = field(default_factory=dict)
    outstanding: dict[str, float] = field(default_factory=dict)
    digraph: nx.DiGraph = field(default_factory=nx.DiGraph)

    @classmethod
    def build(cls, window: ObservedWindow) -> ObservedStats:
        ids = window.merchant_ids
        stats = cls(window=window, merchant_ids=ids, index={m: i for i, m in enumerate(ids)})

        for merchant_id in ids:
            stats.inflow_events[merchant_id] = []
            stats.outflow_events[merchant_id] = []
            stats.payables[merchant_id] = []
            stats.receivables[merchant_id] = []

        for event in window.graph.payment_events:
            if event.payee_id in stats.inflow_events:
                stats.inflow_events[event.payee_id].append(event)
            if event.payer_id in stats.outflow_events:
                stats.outflow_events[event.payer_id].append(event)
            if EXTERNAL_SINK in (event.payer_id, event.payee_id):
                continue
            key = event.edge_key
            stats.pair_value[key] = stats.pair_value.get(key, 0.0) + event.amount
            stats.pair_events.setdefault(key, []).append(event)

        for obligation in window.graph.obligations:
            remaining = window.outstanding(obligation)
            stats.outstanding[obligation.obligation_id] = remaining
            if remaining <= 0.0:
                continue
            if obligation.debtor_id in stats.payables:
                stats.payables[obligation.debtor_id].append(obligation)
            if obligation.creditor_id in stats.receivables:
                stats.receivables[obligation.creditor_id].append(obligation)

        stats.digraph = nx.DiGraph()
        stats.digraph.add_nodes_from(ids)
        for (source, target), value in sorted(stats.pair_value.items()):
            if source in stats.index and target in stats.index:
                stats.digraph.add_edge(source, target, weight=value)
        return stats

    # ------------------------------------------------------------------ views

    def buffer(self, merchant_id: str) -> float:
        """``b_i(0)`` as disclosed. Floored at 1 so ratios never divide by zero."""
        return max(self.window.graph.merchant(merchant_id).initial_buffer, 1.0)

    def due_between(self, merchant_id: str, t0: float, t1: float) -> float:
        """Outstanding payable value falling due in ``[t0, t1)``."""
        return sum(
            self.outstanding[o.obligation_id]
            for o in self.payables[merchant_id]
            if t0 <= o.due_t < t1
        )

    def receivable_between(self, merchant_id: str, t0: float, t1: float) -> float:
        """Outstanding receivable value expected in ``[t0, t1)``.

        Expected, not guaranteed: these are the counterparties' commitments, and
        whether they arrive is precisely what contagion decides. Using them as a
        covariate is legitimate - the merchant knows what it is owed - but a
        model that treats them as certain will be over-optimistic, which is the
        behaviour the calibration layer surfaces.
        """
        return sum(
            self.outstanding[o.obligation_id]
            for o in self.receivables[merchant_id]
            if t0 <= o.due_t < t1
        )

    def pagerank(self, reverse: bool = False) -> dict[str, float]:
        graph = self.digraph.reverse(copy=False) if reverse else self.digraph
        if graph.number_of_edges() == 0:
            return dict.fromkeys(self.merchant_ids, 1.0 / max(len(self.merchant_ids), 1))
        try:
            return dict(nx.pagerank(graph, weight="weight", max_iter=200, tol=1e-8))
        except nx.PowerIterationFailedConvergence:  # pragma: no cover - defensive
            return dict.fromkeys(self.merchant_ids, 1.0 / max(len(self.merchant_ids), 1))

    def reach_counts(self, hops: int) -> tuple[dict[str, int], dict[str, int]]:
        """``(downstream, upstream)`` node counts within ``hops`` on the observed graph."""
        down: dict[str, int] = {}
        up: dict[str, int] = {}
        reverse = self.digraph.reverse(copy=False)
        for merchant_id in self.merchant_ids:
            down[merchant_id] = (
                len(nx.single_source_shortest_path_length(self.digraph, merchant_id, cutoff=hops))
                - 1
            )
            up[merchant_id] = (
                len(nx.single_source_shortest_path_length(reverse, merchant_id, cutoff=hops)) - 1
            )
        return down, up

    def hops_from(self, origins: Sequence[str], cap: int) -> dict[str, float]:
        """Shortest downstream hop count from any shock origin, ``cap`` if unreachable."""
        distance = dict.fromkeys(self.merchant_ids, float(cap))
        frontier = [m for m in origins if m in self.index]
        for merchant_id in frontier:
            distance[merchant_id] = 0.0
        depth = 0
        seen = set(frontier)
        while frontier and depth < cap:
            depth += 1
            nxt: list[str] = []
            for node in frontier:
                for successor in sorted(self.digraph.successors(node)):
                    if successor not in seen:
                        seen.add(successor)
                        distance[successor] = float(depth)
                        nxt.append(successor)
            frontier = nxt
        return distance

    def upstream_exposure(
        self, origins: Sequence[str], hops: int = EXPOSURE_HOPS
    ) -> dict[str, float]:
        """Share of a node's observed inflow value traceable to a shocked origin.

        A value-weighted diffusion rather than a hop count: being one hop from
        the shock matters only to the extent that the shocked node is where your
        money actually comes from. Iterating ``hops`` times bounds it to the
        depth cascades reach in this benchmark.
        """
        exposure = dict.fromkeys(self.merchant_ids, 0.0)
        origin_set = {m for m in origins if m in self.index}
        for merchant_id in origin_set:
            exposure[merchant_id] = 1.0

        inflow_total = {
            m: sum(e.amount for e in self.inflow_events[m]) for m in self.merchant_ids
        }
        for _ in range(hops):
            updated = dict(exposure)
            for merchant_id in self.merchant_ids:
                if merchant_id in origin_set:
                    continue
                total = inflow_total[merchant_id]
                if total <= 0.0:
                    continue
                score = 0.0
                for predecessor in sorted(self.digraph.predecessors(merchant_id)):
                    share = self.pair_value.get((predecessor, merchant_id), 0.0) / total
                    score += share * exposure[predecessor]
                updated[merchant_id] = float(min(1.0, score))
            exposure = updated
        return exposure


# ------------------------------------------------------------------ node table


def _group_columns() -> list[tuple[str, str]]:
    """``(group, name)`` for every node column, in matrix order."""
    return [
        ("balance_sheet", "log_buffer"),
        ("balance_sheet", "log_opening_balance"),
        ("balance_sheet", "credit_share_of_buffer"),
        ("book", "log_payables_open"),
        ("book", "log_receivables_open"),
        ("book", "payables_over_buffer"),
        ("book", "receivables_over_buffer"),
        ("book", "net_position_over_buffer"),
        ("book", "due_24h_over_buffer"),
        ("book", "due_72h_over_buffer"),
        ("book", "overdue_over_buffer"),
        ("book", "n_payables"),
        ("book", "n_receivables"),
        ("book", "hours_to_first_payable"),
        ("book", "hours_to_first_receivable"),
        ("history", "log_inflow_value"),
        ("history", "log_outflow_value"),
        ("history", "inflow_over_outflow"),
        ("history", "n_inflow_events"),
        ("history", "n_outflow_events"),
        ("history", "in_degree"),
        ("history", "out_degree"),
        ("history", "inflow_concentration"),
        ("history", "outflow_concentration"),
        ("history", "hours_since_last_inflow"),
        ("history", "hours_since_last_outflow"),
        ("history", "inflow_interarrival_cv"),
        ("structure", "pagerank"),
        ("structure", "reverse_pagerank"),
        ("structure", "downstream_reach_2"),
        ("structure", "upstream_reach_2"),
        ("shock", "is_shock_origin"),
        ("shock", "direct_shock_over_buffer"),
        ("shock", "log_total_shock"),
        ("shock", "hops_from_shock"),
        ("shock", "upstream_shock_exposure"),
        ("context", "sector_index"),
        ("context", "tier_index"),
    ]


NODE_COLUMNS: tuple[tuple[str, str], ...] = tuple(_group_columns())
NODE_FEATURE_NAMES: tuple[str, ...] = tuple(f"{g}.{n}" for g, n in NODE_COLUMNS)
NODE_FEATURE_DIM = len(NODE_COLUMNS)
NODE_FEATURE_INDEX: dict[str, int] = {name: i for i, name in enumerate(NODE_FEATURE_NAMES)}
INTERVAL_FEATURE_INDEX: dict[str, int] = {}

#: Hop cap for the shock-distance feature. Beyond four hops the benchmark's
#: cascades have already died out, so a larger cap only adds a constant.
MAX_SHOCK_HOPS = 4


#: Columns that cannot be computed without knowing *who* a merchant transacts
#: with. Dropping exactly these is the ``no_graph`` ablation: the model keeps
#: everything a merchant could compute from its own bank statement and invoice
#: book - volumes, cadence, what is due when, the size of the shock it was told
#: about - and loses everything that required looking at the network.
NETWORK_DEPENDENT_COLUMNS: frozenset[str] = frozenset(
    {
        "structure.pagerank",
        "structure.reverse_pagerank",
        "structure.downstream_reach_2",
        "structure.upstream_reach_2",
        "shock.hops_from_shock",
        "shock.upstream_shock_exposure",
        "history.in_degree",
        "history.out_degree",
        "history.inflow_concentration",
        "history.outflow_concentration",
    }
)


def network_free_mask() -> np.ndarray:
    """Column mask keeping only what a merchant could compute about itself."""
    return np.array(
        [name not in NETWORK_DEPENDENT_COLUMNS for name in NODE_FEATURE_NAMES], dtype=bool
    )


def group_mask(groups: Sequence[str]) -> np.ndarray:
    """Boolean column mask selecting the named feature groups."""
    allowed = set(groups)
    unknown = allowed - set(FEATURE_GROUPS)
    if unknown:
        raise ValueError(f"unknown feature groups: {sorted(unknown)}")
    return np.array([g in allowed for g, _ in NODE_COLUMNS], dtype=bool)


def build_node_features(
    window: ObservedWindow, stats: ObservedStats | None = None
) -> tuple[list[str], np.ndarray]:
    """Per-merchant feature matrix at the prediction origin.

    Returns ``(merchant_ids, X)`` with ``X`` of shape ``(n, NODE_FEATURE_DIM)``
    and columns named by :data:`NODE_FEATURE_NAMES`.
    """
    stats = stats or ObservedStats.build(window)
    ids = stats.merchant_ids
    origin = window.origin_t
    horizon = window.horizon_end
    remaining = window.remaining_hours

    shock_by_node = window.shock_by_node()
    origins = sorted(shock_by_node)
    total_shock = float(sum(shock_by_node.values()))
    hops = stats.hops_from(origins, MAX_SHOCK_HOPS) if origins else {}
    exposure = stats.upstream_exposure(origins) if origins else {}

    pagerank = stats.pagerank()
    reverse_pagerank = stats.pagerank(reverse=True)
    downstream, upstream = stats.reach_counts(2)
    n_nodes = max(len(ids), 1)

    matrix = np.zeros((len(ids), NODE_FEATURE_DIM), dtype=np.float64)
    for row, merchant_id in enumerate(ids):
        profile = window.graph.merchant(merchant_id)
        buffer = stats.buffer(merchant_id)

        payables = stats.payables[merchant_id]
        receivables = stats.receivables[merchant_id]
        payable_value = sum(stats.outstanding[o.obligation_id] for o in payables)
        receivable_value = sum(stats.outstanding[o.obligation_id] for o in receivables)
        overdue = sum(
            stats.outstanding[o.obligation_id] for o in payables if o.due_t < origin
        )
        first_payable = min((o.due_t for o in payables if o.due_t >= origin), default=horizon)
        first_receivable = min(
            (o.due_t for o in receivables if o.due_t >= origin), default=horizon
        )

        inflows = stats.inflow_events[merchant_id]
        outflows = stats.outflow_events[merchant_id]
        inflow_value = sum(e.amount for e in inflows)
        outflow_value = sum(e.amount for e in outflows)
        inflow_by_source: dict[str, float] = {}
        for event in inflows:
            inflow_by_source[event.payer_id] = (
                inflow_by_source.get(event.payer_id, 0.0) + event.amount
            )
        outflow_by_target: dict[str, float] = {}
        for event in outflows:
            outflow_by_target[event.payee_id] = (
                outflow_by_target.get(event.payee_id, 0.0) + event.amount
            )
        inflow_times = np.array(sorted(e.t for e in inflows), dtype=float)
        gaps = np.diff(inflow_times) if inflow_times.size > 1 else np.empty(0)
        inflow_cv = (
            float(gaps.std(ddof=1) / gaps.mean())
            if gaps.size > 1 and gaps.mean() > 0
            else 0.0
        )

        matrix[row] = (
            # --- balance_sheet
            _log(buffer),
            _log(profile.opening_balance),
            _ratio(profile.credit_limit, buffer, cap=5.0),
            # --- book
            _log(payable_value),
            _log(receivable_value),
            _ratio(payable_value, buffer),
            _ratio(receivable_value, buffer),
            float(np.clip((receivable_value - payable_value) / buffer, -50.0, 50.0)),
            _ratio(stats.due_between(merchant_id, origin, origin + 24.0), buffer),
            _ratio(stats.due_between(merchant_id, origin, origin + 72.0), buffer),
            _ratio(overdue, buffer),
            min(len(payables), 100) / 10.0,
            min(len(receivables), 100) / 10.0,
            min(max(first_payable - origin, 0.0), remaining) / remaining,
            min(max(first_receivable - origin, 0.0), remaining) / remaining,
            # --- history
            _log(inflow_value),
            _log(outflow_value),
            _ratio(inflow_value, max(outflow_value, 1.0), cap=10.0),
            min(len(inflows), 2000) / 100.0,
            min(len(outflows), 2000) / 100.0,
            min(len(inflow_by_source), 100) / 10.0,
            min(len(outflow_by_target), 100) / 10.0,
            _hhi(list(inflow_by_source.values())),
            _hhi(list(outflow_by_target.values())),
            _log(origin - max((e.t for e in inflows), default=origin)) / 10.0,
            _log(origin - max((e.t for e in outflows), default=origin)) / 10.0,
            float(min(inflow_cv, 10.0)),
            # --- structure
            pagerank.get(merchant_id, 0.0) * n_nodes,
            reverse_pagerank.get(merchant_id, 0.0) * n_nodes,
            min(downstream.get(merchant_id, 0), 500) / 10.0,
            min(upstream.get(merchant_id, 0), 500) / 10.0,
            # --- shock
            1.0 if merchant_id in shock_by_node else 0.0,
            _ratio(shock_by_node.get(merchant_id, 0.0), buffer),
            _log(total_shock),
            hops.get(merchant_id, float(MAX_SHOCK_HOPS)) / MAX_SHOCK_HOPS,
            exposure.get(merchant_id, 0.0),
            # --- context
            _SECTORS.index(profile.sector) / max(len(_SECTORS) - 1, 1),
            _TIERS.index(profile.tier) / max(len(_TIERS) - 1, 1),
        )

    return ids, np.nan_to_num(matrix, nan=0.0, posinf=0.0, neginf=0.0)


# ----------------------------------------------------------- interval features


#: Time-varying columns appended per hazard interval.
INTERVAL_FEATURE_NAMES: tuple[str, ...] = (
    "interval.due_in_interval_over_buffer",
    "interval.cumulative_due_over_buffer",
    "interval.cumulative_receivable_over_buffer",
    "interval.elapsed_fraction",
)
INTERVAL_FEATURE_DIM = len(INTERVAL_FEATURE_NAMES)
INTERVAL_FEATURE_INDEX.update({name: i for i, name in enumerate(INTERVAL_FEATURE_NAMES)})


def build_interval_features(
    window: ObservedWindow, edges: np.ndarray, stats: ObservedStats | None = None
) -> np.ndarray:
    """Time-varying covariates of shape ``(n_merchants, n_intervals, 4)``.

    ``edges`` are interval boundaries measured *from the origin*, so
    ``edges[0] == 0``. Without covariates like these the hazard model's baseline
    term carries all the timing, and every merchant is predicted to fail on the
    same schedule.
    """
    stats = stats or ObservedStats.build(window)
    ids = stats.merchant_ids
    origin = window.origin_t
    remaining = window.remaining_hours
    n_intervals = len(edges) - 1

    out = np.zeros((len(ids), n_intervals, INTERVAL_FEATURE_DIM), dtype=np.float64)
    for row, merchant_id in enumerate(ids):
        buffer = stats.buffer(merchant_id)
        cumulative_due = 0.0
        cumulative_recv = 0.0
        for k in range(n_intervals):
            t0 = origin + float(edges[k])
            t1 = origin + float(edges[k + 1])
            due = stats.due_between(merchant_id, t0, t1)
            cumulative_due += due
            cumulative_recv += stats.receivable_between(merchant_id, t0, t1)
            out[row, k] = (
                _ratio(due, buffer),
                _ratio(cumulative_due, buffer),
                _ratio(cumulative_recv, buffer),
                float(edges[k + 1]) / remaining,
            )
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)


#: Version of the node/interval feature contract. Any change to the columns, their
#: order, or their definitions must bump this: an exported model artifact records
#: it, and the inference service refuses a bundle whose schema it does not match.
FEATURE_SCHEMA_VERSION = "phase3-node-v1"


def build_hazard_design(
    x: np.ndarray,
    interval_x: np.ndarray,
    *,
    remaining_hours: float,
    feature_mask: np.ndarray | None = None,
) -> np.ndarray:
    """Assemble the discrete-time hazard design tensor ``(n, K, P)``.

    Lives here, in the feature module, because it *is* part of the feature
    contract: the training model and the production inference service must build
    byte-identical designs or the exported weights mean something different at
    serving time than they did at fit time. One definition, used by both, is the
    only way to guarantee that - a second copy in the inference package would
    drift the first time a column moved.

    Layout, left to right: the (optionally masked) static node features repeated
    across intervals, the time-varying interval features, a one-hot interval
    indicator giving the baseline hazard a free level per interval, the log
    interval width, and a constant.
    """
    n_intervals = interval_x.shape[1]
    node = x if feature_mask is None else x[:, feature_mask]
    static = np.repeat(node[:, None, :], n_intervals, axis=1)
    one_hot = np.tile(np.eye(n_intervals)[None, :, :], (x.shape[0], 1, 1))
    width = np.full(
        (x.shape[0], n_intervals, 1),
        math.log1p(max(remaining_hours, 0.0) / max(n_intervals, 1)),
    )
    intercept = np.ones((x.shape[0], n_intervals, 1))
    return np.concatenate([static, interval_x, one_hot, width, intercept], axis=2)


# ------------------------------------------------------------------ pair table


PAIR_FEATURE_NAMES: tuple[str, ...] = (
    "pair.log_n_events",
    "pair.log_total_value",
    "pair.log_mean_amount",
    "pair.amount_cv",
    "pair.share_of_payer_outflow",
    "pair.share_of_payee_inflow",
    "pair.regularity",
    "pair.log_median_interarrival",
    "pair.open_obligation_over_payer_buffer",
    "pair.log_median_response_lag",
    "pair.active_fraction_of_window",
)
PAIR_FEATURE_DIM = len(PAIR_FEATURE_NAMES)


def build_pair_features(
    window: ObservedWindow, stats: ObservedStats | None = None
) -> tuple[list[tuple[str, str]], np.ndarray]:
    """Descriptive features for every ordered pair that transacted before the origin.

    These are the inputs both to the temporal graph model's edge attributes and
    to the *supervised* dependency upper bound. They are deliberately descriptive
    only - no pass-through, no trigger probability. Those are estimated in
    :mod:`lce.learning.pointprocess`, and mixing an estimate into the feature
    table would make the two experiments impossible to tell apart.
    """
    from lce.models.features import compute_edge_features, estimate_response_lags

    stats = stats or ObservedStats.build(window)
    origin = window.origin_t
    events = window.graph.payment_events
    span = max(origin - min((e.t for e in events), default=origin - 1.0), 1.0)

    outflow_total = {
        m: sum(e.amount for e in stats.outflow_events[m]) for m in stats.merchant_ids
    }
    inflow_total = {
        m: sum(e.amount for e in stats.inflow_events[m]) for m in stats.merchant_ids
    }

    open_by_pair: dict[tuple[str, str], float] = {}
    for merchant_id in stats.merchant_ids:
        for obligation in stats.payables[merchant_id]:
            key = (obligation.debtor_id, obligation.creditor_id)
            open_by_pair[key] = open_by_pair.get(key, 0.0) + stats.outstanding[
                obligation.obligation_id
            ]

    keys = sorted(stats.pair_events)
    matrix = np.zeros((len(keys), PAIR_FEATURE_DIM), dtype=np.float64)
    for row, key in enumerate(keys):
        source, target = key
        pair_events = sorted(stats.pair_events[key], key=lambda e: e.t)
        features = compute_edge_features(pair_events)
        times = np.array([e.t for e in pair_events], dtype=float)
        gaps = np.diff(times) if times.size > 1 else np.empty(0)
        lags = estimate_response_lags(
            np.array([e.t for e in stats.inflow_events.get(source, [])], dtype=float),
            times,
        )
        matrix[row] = (
            _log(features.n_events),
            _log(features.total_amount),
            _log(features.mean_amount),
            float(min(features.std_amount / max(features.mean_amount, 1.0), 10.0)),
            _ratio(features.total_amount, max(outflow_total.get(source, 0.0), 1.0), cap=1.0),
            _ratio(features.total_amount, max(inflow_total.get(target, 0.0), 1.0), cap=1.0),
            features.regularity,
            _log(float(np.median(gaps)) if gaps.size else 0.0) / 10.0,
            _ratio(open_by_pair.get(key, 0.0), stats.buffer(source)),
            _log(float(np.median(lags)) if lags.size else 0.0) / 10.0,
            _active_share(features.first_t, features.last_t, span),
        )

    return keys, np.nan_to_num(matrix, nan=0.0, posinf=0.0, neginf=0.0)


def _active_share(first_t: float | None, last_t: float | None, span: float) -> float:
    """Fraction of the observed window the link was actually active over."""
    if first_t is None or last_t is None:
        return 0.0
    return float(min(max(last_t - first_t, 0.0) / max(span, 1.0), 1.0))


def feature_summary() -> dict[str, Any]:
    """What the feature layer exposes - recorded in every run manifest."""
    counts: dict[str, int] = {}
    for group, _ in NODE_COLUMNS:
        counts[group] = counts.get(group, 0) + 1
    return {
        "node_feature_dim": NODE_FEATURE_DIM,
        "node_feature_names": list(NODE_FEATURE_NAMES),
        "groups": counts,
        "interval_feature_dim": INTERVAL_FEATURE_DIM,
        "pair_feature_dim": PAIR_FEATURE_DIM,
        "pair_feature_names": list(PAIR_FEATURE_NAMES),
    }
