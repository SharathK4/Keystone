"""Dependency learning, propagation prediction and the artifact registry."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from lce.domain.enums import PredictorKind, RecurrencePattern
from lce.domain.events import PaymentEvent
from lce.errors import ModelError, NotFoundError
from lce.models.dependency import (
    DependencyLearner,
    DependencyLearnerConfig,
    compare_to_ground_truth,
)
from lce.models.features import (
    classify_recurrence,
    compute_edge_features,
    estimate_reliability,
    regularity_score,
)
from lce.models.hawkes import fit_marked_hawkes
from lce.models.propagation import (
    HawkesCascadePredictor,
    LinearThresholdPropagator,
    PropagationConfig,
)
from lce.models.registry import ModelRegistry
from lce.simulation.scenarios import unit_shock


class TestFeatures:
    def test_recurrence_is_classified_from_inter_arrivals(self):
        weekly, period = classify_recurrence(np.array([168.0, 170.0, 166.0]))
        assert weekly is RecurrencePattern.WEEKLY
        assert period == pytest.approx(168.0)

        # Median 55h sits in no billing band (>30% from daily and from weekly).
        irregular, gap = classify_recurrence(np.array([50.0, 60.0, 55.0]))
        assert irregular is RecurrencePattern.IRREGULAR
        assert gap == pytest.approx(55.0)

        empty, none_period = classify_recurrence(np.array([]))
        assert empty is RecurrencePattern.ONE_OFF
        assert none_period is None

    def test_regularity_is_one_for_clockwork_and_zero_for_chaos(self):
        assert regularity_score(np.array([24.0, 24.0, 24.0])) == pytest.approx(1.0)
        assert regularity_score(np.array([1.0, 500.0, 2.0])) < 0.3
        assert regularity_score(np.array([24.0])) == 0.0

    def test_edge_features_summarise_the_history(self):
        events = [
            PaymentEvent(payer_id="a", payee_id="b", amount=100.0 * (i + 1), t=24.0 * i)
            for i in range(5)
        ]
        features = compute_edge_features(events)
        assert features.n_events == 5
        assert features.mean_amount == pytest.approx(300.0)
        assert features.first_t == 0.0
        assert features.last_t == pytest.approx(96.0)
        assert features.recurrence is RecurrencePattern.DAILY

    def test_empty_history_yields_empty_features(self):
        assert compute_edge_features([]).n_events == 0

    def test_reliability_reports_its_basis_honestly(self):
        """Without settled obligations, reliability is a proxy - and says so."""
        features = compute_edge_features(
            [PaymentEvent(payer_id="a", payee_id="b", amount=1.0, t=float(i)) for i in range(4)]
        )
        value, basis = estimate_reliability((), features=features)
        assert basis == "regularity_proxy"
        assert 0.0 <= value <= 1.0

        value, basis = estimate_reliability((), features=None)
        assert basis == "prior"


class TestMarkedHawkes:
    def test_recovers_a_known_pass_through(self):
        """The estimator must recover theta from a stream it did not generate."""
        rng = np.random.default_rng(0)
        theta_true, lag_true = 0.35, 30.0

        inflow_times = np.sort(rng.uniform(0.0, 4000.0, size=400))
        inflow_amounts = rng.lognormal(10.0, 0.4, size=400)

        out_t, out_a = [], []
        for t, amount in zip(inflow_times, inflow_amounts, strict=True):
            if rng.random() < 0.8:
                out_t.append(t + rng.lognormal(np.log(lag_true), 0.4))
                out_a.append(theta_true * amount * rng.lognormal(0.0, 0.15))
        order = np.argsort(out_t)

        fit = fit_marked_hawkes(
            np.asarray(out_t)[order],
            np.asarray(out_a)[order],
            inflow_times,
            inflow_amounts,
            window=4000.0,
            beta=1.0 / 48.0,
        )
        assert fit.theta == pytest.approx(theta_true, abs=0.08)
        assert fit.conditional_probability > 0.4

    def test_degenerate_input_returns_a_baseline_fit(self):
        fit = fit_marked_hawkes(
            np.array([1.0, 2.0]),
            np.array([10.0, 20.0]),
            np.array([]),
            np.array([]),
            window=100.0,
            beta=0.02,
        )
        assert fit.theta == 0.0
        assert fit.conditional_probability == 0.0
        assert not fit.converged


class TestDependencyLearner:
    @pytest.fixture(scope="class")
    def learned(self, medium_network):
        edges = DependencyLearner().fit_graph(medium_network.graph, t_end=0.0)
        return edges, compare_to_ground_truth(edges, medium_network.ground_truth_edges)

    def test_recovers_the_edge_set(self, learned):
        _, metrics = learned
        assert metrics["edge_precision"] == pytest.approx(1.0)
        assert metrics["edge_recall"] == pytest.approx(1.0)

    def test_recovers_pass_through_accurately(self, learned):
        """The headline claim: latent theta is inferred from events alone."""
        _, metrics = learned
        assert metrics["pass_through_mae"] < 0.15
        assert metrics["pass_through_corr"] > 0.7
        assert metrics["pass_through_spearman"] > 0.6

    def test_estimates_are_in_range_and_marked_as_estimates(self, learned):
        edges, _ = learned
        for edge in edges:
            assert 0.0 <= edge.pass_through <= 1.0
            assert 0.0 <= edge.conditional_probability <= 1.0
            assert edge.lag.mean_hours > 0
            assert not edge.is_ground_truth
            assert edge.estimator == "marked_hawkes_em"

    def test_horizon_data_is_held_out(self, medium_network):
        """t_end=0 must exclude the simulation window from the fit."""
        graph = medium_network.graph
        seen = [e for e in graph.payment_events if e.t < 0.0]
        assert len(seen) == graph.stats().n_payment_events  # history is all pre-zero

        config = replace(DependencyLearnerConfig(), em_iterations=3)
        edges = DependencyLearner(config).fit_graph(graph, t_end=-100.0)
        assert edges  # still fits on a truncated window

    def test_thin_edges_are_reported_with_zero_confidence(self, small_network):
        """An underdetermined edge must not masquerade as a measurement."""
        edges = DependencyLearner().fit_graph(small_network.graph, t_end=0.0)
        thin = [e for e in edges if e.metadata.get("underdetermined")]
        for edge in thin:
            assert edge.confidence == 0.0
            assert edge.pass_through == 0.0

    def test_learning_is_reproducible(self, small_network):
        a = DependencyLearner().fit_graph(small_network.graph, t_end=0.0)
        b = DependencyLearner().fit_graph(small_network.graph, t_end=0.0)
        assert [e.pass_through for e in a] == [e.pass_through for e in b]


class TestPropagationPredictors:
    @pytest.fixture
    def scenario(self, medium_network):
        graph = medium_network.graph
        anchor = max(
            medium_network.anchors(), key=lambda m: len(graph.descendants_within(m, 3))
        )
        return graph, unit_shock(graph, anchor, fraction_of_buffer=3.0), anchor

    def test_linear_threshold_flags_the_shocked_node(self, scenario):
        graph, shock, anchor = scenario
        prediction = LinearThresholdPropagator(PropagationConfig()).predict(graph, shock)
        assert prediction.exposures[anchor].exposure_score > 0.5
        assert prediction.exposures[anchor].hop_distance == 0

    def test_scores_are_probabilities_over_every_node(self, scenario):
        graph, shock, _ = scenario
        for model in (LinearThresholdPropagator(), HawkesCascadePredictor()):
            prediction = model.predict(graph, shock)
            assert set(prediction.exposures) == set(graph.merchant_ids)
            assert all(0.0 <= e.exposure_score <= 1.0 for e in prediction.exposures.values())

    def test_unreachable_nodes_score_zero(self, scenario):
        graph, shock, anchor = scenario
        reachable = set(graph.descendants_within(anchor, 10)) | {anchor}
        prediction = LinearThresholdPropagator().predict(graph, shock)
        for merchant_id in graph.merchant_ids:
            if merchant_id not in reachable:
                assert prediction.exposures[merchant_id].exposure_score == 0.0

    def test_hit_times_lie_inside_the_horizon(self, scenario):
        graph, shock, _ = scenario
        config = PropagationConfig(horizon_hours=168.0)
        prediction = LinearThresholdPropagator(config).predict(graph, shock)
        for exposure in prediction.exposures.values():
            if exposure.expected_hit_t is not None:
                assert 0.0 <= exposure.expected_hit_t <= 168.0

    def test_bigger_shocks_do_not_lower_exposure(self, scenario):
        graph, _, anchor = scenario
        model = LinearThresholdPropagator()
        small = model.predict(graph, unit_shock(graph, anchor, fraction_of_buffer=1.0))
        large = model.predict(graph, unit_shock(graph, anchor, fraction_of_buffer=5.0))
        for merchant_id in graph.merchant_ids:
            assert (
                large.exposures[merchant_id].exposure_score
                >= small.exposures[merchant_id].exposure_score - 1e-9
            )

    def test_ranking_and_thresholding(self, scenario):
        graph, shock, _ = scenario
        prediction = LinearThresholdPropagator().predict(graph, shock)
        ranked = prediction.ranked()
        scores = [e.exposure_score for e in ranked]
        assert scores == sorted(scores, reverse=True)
        assert set(prediction.predicted_affected_at(0.9)) <= set(
            prediction.predicted_affected_at(0.1)
        )

    def test_predictor_kinds_are_reported(self, scenario):
        graph, shock, _ = scenario
        assert (
            LinearThresholdPropagator().predict(graph, shock).predictor
            is PredictorKind.LINEAR_THRESHOLD
        )
        assert (
            HawkesCascadePredictor().predict(graph, shock).predictor
            is PredictorKind.HAWKES_CASCADE
        )


class TestModelRegistry:
    def test_manifest_round_trip(self, tmp_path):
        registry = ModelRegistry(tmp_path)
        manifest = registry.save(
            "contagion",
            "v1",
            kind="analytic",
            config={"alpha": 1},
            dataset_version="synth-abc",
            seed=42,
            metrics={"f1": 0.8},
        )
        assert manifest.code_version
        loaded = registry.load_manifest("contagion", "v1")
        assert loaded.dataset_version == "synth-abc"
        assert loaded.metrics["f1"] == 0.8
        assert registry.exists("contagion", "v1")
        assert registry.list_versions("contagion") == ["v1"]
        assert registry.list_models() == {"contagion": ["v1"]}

    def test_missing_manifest_raises(self, tmp_path):
        with pytest.raises(NotFoundError):
            ModelRegistry(tmp_path).load_manifest("nope", "v1")

    def test_missing_artifact_raises(self, tmp_path):
        registry = ModelRegistry(tmp_path)
        registry.save("m", "v1", kind="analytic", config={})
        with pytest.raises(NotFoundError):
            registry.artifact_path("m", "v1")

    def test_artifact_is_written_and_deletable(self, tmp_path):
        registry = ModelRegistry(tmp_path)
        registry.save(
            "m",
            "v1",
            kind="learned",
            config={},
            artifact_writer=lambda path: path.write_bytes(b"weights"),
        )
        assert registry.artifact_path("m", "v1").read_bytes() == b"weights"
        assert registry.delete("m", "v1")
        assert not registry.exists("m", "v1")
        assert not registry.delete("m", "v1")
