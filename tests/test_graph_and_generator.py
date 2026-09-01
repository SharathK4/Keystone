"""Temporal graph structure and generator correctness."""

from __future__ import annotations

from dataclasses import replace

import pytest

from lce.data.generator import GeneratorConfig, generate_network
from lce.domain.edges import DependencyEdge
from lce.domain.events import EXTERNAL_SINK, Obligation, PaymentEvent
from lce.domain.merchant import MerchantProfile
from lce.errors import GraphError, NotFoundError
from lce.graph.builders import build_graph, subgraph_around
from lce.graph.temporal_graph import TemporalPaymentGraph


@pytest.fixture
def tiny_graph() -> TemporalPaymentGraph:
    profiles = [
        MerchantProfile(merchant_id=f"m{i}", opening_balance=100_000.0) for i in range(3)
    ]
    graph = build_graph(profiles)
    for i, t in enumerate((1.0, 5.0, 9.0, 13.0)):
        graph.add_payment(
            PaymentEvent(payer_id="m0", payee_id="m1", amount=100.0 * (i + 1), t=t)
        )
    graph.add_payment(PaymentEvent(payer_id="m1", payee_id="m2", amount=50.0, t=7.0))
    graph.set_dependency(DependencyEdge(source_id="m0", target_id="m1", pass_through=0.5))
    graph.set_dependency(DependencyEdge(source_id="m1", target_id="m2", pass_through=0.3))
    return graph


class TestTemporalGraph:
    def test_parallel_edges_are_preserved_not_aggregated(self, tiny_graph):
        # The core structural claim: four payments between the same pair remain
        # four distinct, timestamped edges.
        events = tiny_graph.events_on_edge("m0", "m1")
        assert len(events) == 4
        assert [e.t for e in events] == [1.0, 5.0, 9.0, 13.0]
        assert tiny_graph.stats().n_payment_events == 5
        assert tiny_graph.stats().n_distinct_pairs == 2

    def test_time_window_queries_are_half_open(self, tiny_graph):
        window = tiny_graph.events_between(5.0, 9.0)
        assert [e.t for e in window] == [5.0, 7.0]
        inclusive = tiny_graph.events_between(5.0, 9.0, inclusive_end=True)
        assert [e.t for e in inclusive] == [5.0, 7.0, 9.0]

    def test_events_are_returned_in_chronological_order(self, tiny_graph):
        tiny_graph.add_payment(PaymentEvent(payer_id="m0", payee_id="m2", amount=5.0, t=0.5))
        times = [e.t for e in tiny_graph.payment_events]
        assert times == sorted(times)

    def test_inbound_and_outbound_views_are_consistent(self, tiny_graph):
        assert len(tiny_graph.outbound_events("m0")) == 4
        assert len(tiny_graph.inbound_events("m1")) == 4
        assert len(tiny_graph.outbound_events("m1")) == 1

    def test_unknown_merchant_references_are_rejected(self, tiny_graph):
        with pytest.raises(GraphError):
            tiny_graph.add_payment(
                PaymentEvent(payer_id="ghost", payee_id="m1", amount=1.0, t=1.0)
            )
        with pytest.raises(NotFoundError):
            tiny_graph.merchant("ghost")

    def test_duplicate_event_ids_are_rejected(self, tiny_graph):
        event = PaymentEvent(payer_id="m0", payee_id="m1", amount=1.0, t=2.0)
        tiny_graph.add_payment(event)
        with pytest.raises(GraphError):
            tiny_graph.add_payment(event)

    def test_external_sink_counterparties_are_allowed(self, tiny_graph):
        tiny_graph.add_payment(
            PaymentEvent(payer_id=EXTERNAL_SINK, payee_id="m0", amount=999.0, t=2.0)
        )
        assert len(tiny_graph.inbound_events("m0")) == 1

    def test_dependency_overlay_navigation(self, tiny_graph):
        assert tiny_graph.successors("m0") == ["m1"]
        assert tiny_graph.predecessors("m2") == ["m1"]
        assert tiny_graph.dependency("m0", "m1").pass_through == pytest.approx(0.5)
        assert tiny_graph.dependency("m0", "m2") is None

    def test_descendants_respect_the_hop_limit(self, tiny_graph):
        assert tiny_graph.descendants_within("m0", 1) == {"m1": 1}
        assert tiny_graph.descendants_within("m0", 2) == {"m1": 1, "m2": 2}

    def test_payload_round_trip_preserves_everything(self, tiny_graph):
        restored = TemporalPaymentGraph.from_payload(tiny_graph.to_payload())
        assert restored.stats().to_dict() == tiny_graph.stats().to_dict()
        assert len(restored.events_on_edge("m0", "m1")) == 4
        assert restored.dependency("m1", "m2").pass_through == pytest.approx(0.3)

    def test_copy_is_independent(self, tiny_graph):
        clone = tiny_graph.copy()
        clone.add_payment(PaymentEvent(payer_id="m0", payee_id="m1", amount=1.0, t=99.0))
        assert clone.stats().n_payment_events == tiny_graph.stats().n_payment_events + 1

    def test_aggregate_matrix_is_a_lossy_projection(self, tiny_graph):
        ids, matrix = tiny_graph.aggregate_matrix()
        i, j = ids.index("m0"), ids.index("m1")
        assert matrix[i, j] == pytest.approx(100 + 200 + 300 + 400)

    def test_structural_centrality_is_finite(self, tiny_graph):
        centrality = tiny_graph.structural_centrality()
        assert set(centrality) == set(tiny_graph.merchant_ids)
        assert all(v == v for v in centrality.values())  # no NaN

    def test_obligation_indexes(self, tiny_graph):
        obligation = Obligation(
            debtor_id="m0", creditor_id="m1", amount=1000.0, issued_t=0.0, due_t=24.0
        )
        tiny_graph.add_obligation(obligation)
        assert tiny_graph.payables_of("m0")[0].obligation_id == obligation.obligation_id
        assert tiny_graph.receivables_of("m1")[0].obligation_id == obligation.obligation_id
        assert len(tiny_graph.obligations_due_between(0.0, 48.0)) == 1
        assert len(tiny_graph.obligations_due_between(48.0, 96.0)) == 0

    def test_subgraph_keeps_full_event_history_for_kept_nodes(self, tiny_graph):
        ego = subgraph_around(tiny_graph, "m1", hops=1)
        assert set(ego.merchant_ids) == {"m0", "m1", "m2"}
        assert len(ego.events_on_edge("m0", "m1")) == 4


class TestGenerator:
    def test_dataset_version_is_content_addressed(self):
        a = GeneratorConfig(n_merchants=20, seed=5)
        b = GeneratorConfig(n_merchants=20, seed=5)
        c = GeneratorConfig(n_merchants=21, seed=5)
        assert a.dataset_version == b.dataset_version
        assert a.dataset_version != c.dataset_version

    def test_generation_is_reproducible(self):
        config = replace(GeneratorConfig(), n_merchants=18, seed=3, history_hours=240.0)
        first = generate_network(config)
        second = generate_network(config)
        assert first.dataset_version == second.dataset_version
        assert len(first.graph) == len(second.graph)
        assert first.graph.stats().n_payment_events == second.graph.stats().n_payment_events
        assert first.true_pass_through() == second.true_pass_through()

    def test_no_node_owes_more_than_it_receives(self, small_network):
        """Payables must be funded by receivables, or contagion is unmeasurable.

        A node's outgoing edge flows may never exceed its throughput net of the
        margin it retains. Equality is the common case; it can be strict when a
        back-edge or cross-layer edge delivers inflow *after* that node's
        payable budget was already allocated (allocation runs in layer order),
        which only ever leaves the node better funded.

        Without this property the network collapses with no shock applied, and
        there is no contagion signal left to measure.
        """
        graph = small_network.graph
        throughput = small_network.throughput
        checked = 0
        for merchant_id in graph.merchant_ids:
            out_edges = graph.out_dependencies(merchant_id)
            node_throughput = throughput.get(merchant_id, 0.0)
            if not out_edges or node_throughput <= 0:
                continue
            allocated = sum(float(e.metadata.get("horizon_flow", 0.0)) for e in out_edges)
            margin = float(graph.merchant(merchant_id).metadata.get("margin", 0.0))
            budget = node_throughput * (1.0 - margin)
            assert allocated <= budget * (1.0 + 1e-9), (
                f"{merchant_id} owes {allocated:,.0f} against a budget of {budget:,.0f}"
            )
            checked += 1
        assert checked > 0, "no nodes with outgoing flow - the assertion would be vacuous"

    def test_anchors_sit_at_layer_zero_and_earn_exogenously(self, small_network):
        anchors = small_network.anchors()
        assert anchors
        for merchant_id in anchors:
            assert small_network.layers[merchant_id] == 0
            assert small_network.graph.merchant(merchant_id).exogenous_inflow_rate > 0

    def test_exogenous_revenue_is_observable_in_the_event_stream(self, small_network):
        """The learner needs anchors' inflows, or their pass-through is unidentifiable."""
        sink_inflows = [
            e for e in small_network.graph.payment_events if e.payer_id == EXTERNAL_SINK
        ]
        assert sink_inflows, "anchor consumer revenue must be emitted as payment events"

    def test_history_stays_strictly_before_the_horizon(self, small_network):
        assert all(e.t < 0.0 for e in small_network.graph.payment_events)

    def test_obligations_fall_inside_the_horizon(self, small_network):
        horizon = small_network.config.horizon_hours
        for obligation in small_network.graph.obligations:
            assert 0.0 <= obligation.due_t <= horizon

    def test_trade_deadlines_are_staggered_by_layer(self, small_network):
        """Receivables must land before payables or every node fails trivially.

        Scoped to *trade* obligations between merchants. Payroll and tax sinks
        are deliberately scheduled uniformly across the horizon - they are not
        part of the supply-chain stagger, and a deep layer whose only commitment
        is one payroll run would otherwise appear to violate an invariant it was
        never subject to.
        """
        graph = small_network.graph
        layers = small_network.layers
        by_layer: dict[int, list[float]] = {}
        for obligation in graph.obligations:
            if obligation.creditor_id == EXTERNAL_SINK:
                continue
            layer = layers.get(obligation.debtor_id)
            if layer is None:
                continue
            by_layer.setdefault(layer, []).append(obligation.due_t)

        # Layers with a couple of obligations carry no signal about ordering.
        means = {
            layer: sum(v) / len(v) for layer, v in by_layer.items() if len(v) >= 3
        }
        assert len(means) >= 2, "need at least two populated layers to test stagger"
        ordered = [means[layer] for layer in sorted(means)]
        assert ordered == sorted(ordered), f"layer deadlines not monotone: {means}"

    def test_ground_truth_parameters_are_in_range(self, small_network):
        for edge in small_network.ground_truth_edges.values():
            assert 0.0 <= edge.pass_through <= 1.0
            assert 0.0 <= edge.conditional_probability <= 1.0
            assert 0.0 <= edge.reliability <= 1.0
            assert edge.lag.mean_hours > 0
            assert edge.is_ground_truth
