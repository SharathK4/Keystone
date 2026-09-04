"""Intervention search and the scoring machinery."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from conftest import HAS_ORTOOLS
from lce.domain.enums import InterventionType, OptimizerKind
from lce.domain.intervention import Intervention
from lce.errors import OptimizationError
from lce.evaluation.harness import build_ground_truth, evaluate_prediction, evaluate_search
from lce.evaluation.metrics import (
    attributable_affected,
    average_precision,
    classification_metrics,
    confusion,
    intervention_metrics,
    roc_auc,
    timing_metrics,
)
from lce.models.propagation import LinearThresholdPropagator, PropagationConfig
from lce.optimization.candidates import CandidateConfig, generate_candidates
from lce.optimization.search import (
    CpSatSearch,
    ExhaustiveSearch,
    GreedySearch,
    SearchConfig,
    TopExposureSearch,
    build_search,
)
from lce.optimization.systemic import compute_systemic_importance
from lce.simulation.counterfactual import CounterfactualEvaluator
from lce.simulation.scenarios import unit_shock


class TestMetrics:
    def test_confusion_requires_an_explicit_universe(self):
        tp, fp, fn, tn = confusion(
            predicted=["a", "b"], truth=["b", "c"], universe=["a", "b", "c", "d"]
        )
        assert (tp, fp, fn, tn) == (1, 1, 1, 1)

    def test_perfect_and_inverted_predictions(self):
        universe = list("abcd")
        perfect = classification_metrics(
            {"a": 1.0, "b": 1.0, "c": 0.0, "d": 0.0}, ["a", "b"], universe=universe
        )
        assert perfect.precision == 1.0
        assert perfect.recall == 1.0
        assert perfect.f1 == 1.0
        assert perfect.pr_auc == pytest.approx(1.0)

        inverted = classification_metrics(
            {"a": 0.0, "b": 0.0, "c": 1.0, "d": 1.0}, ["a", "b"], universe=universe
        )
        assert inverted.recall == 0.0
        assert inverted.f1 == 0.0

    def test_average_precision_matches_a_hand_computation(self):
        # Ranked: pos, neg, pos, neg -> AP = (1/1 + 2/3) / 2
        scores = np.array([0.9, 0.8, 0.7, 0.6])
        labels = np.array([1, 0, 1, 0])
        assert average_precision(scores, labels) == pytest.approx((1.0 + 2.0 / 3.0) / 2.0)

    def test_average_precision_is_none_without_positives(self):
        assert average_precision(np.array([0.5, 0.4]), np.array([0, 0])) is None

    def test_roc_auc_endpoints(self):
        assert roc_auc(np.array([1.0, 0.0]), np.array([1, 0])) == pytest.approx(1.0)
        assert roc_auc(np.array([0.0, 1.0]), np.array([1, 0])) == pytest.approx(0.0)
        # All ties give exactly 0.5.
        assert roc_auc(np.array([0.5, 0.5]), np.array([1, 0])) == pytest.approx(0.5)
        assert roc_auc(np.array([1.0, 0.0]), np.array([1, 1])) is None

    def test_timing_metrics_only_score_mutually_known_nodes(self):
        metrics = timing_metrics(
            predicted={"a": 10.0, "b": 30.0, "z": 5.0},
            truth={"a": 12.0, "b": 20.0, "c": 4.0},
        )
        assert metrics.n_compared == 2
        assert metrics.mae_hours == pytest.approx((2.0 + 10.0) / 2.0)
        assert metrics.bias_hours == pytest.approx((-2.0 + 10.0) / 2.0)
        assert metrics.within_24h == pytest.approx(1.0)

    def test_timing_metrics_empty_is_safe(self):
        assert timing_metrics({}, {}).n_compared == 0

    def test_attributable_affected_removes_the_baseline(self):
        """Contagion is what the shock caused, not the network's standing state."""
        assert attributable_affected(["a", "b", "c"], ["a"]) == ["b", "c"]

    def test_optimality_gap_definition(self):
        metrics = intervention_metrics(
            baseline_disruption=100.0,
            achieved_disruption=60.0,
            cost=10.0,
            n_actions=1,
            optimal_disruption=40.0,
        )
        assert metrics.disruption_prevented == pytest.approx(40.0)
        assert metrics.disruption_prevented_per_rupee == pytest.approx(4.0)
        # (60 - 40) / (100 - 40)
        assert metrics.optimality_gap == pytest.approx(1.0 / 3.0)
        assert not metrics.is_optimal

    def test_optimal_plan_has_zero_gap(self):
        metrics = intervention_metrics(
            baseline_disruption=100.0,
            achieved_disruption=40.0,
            cost=1.0,
            n_actions=1,
            optimal_disruption=40.0,
        )
        assert metrics.optimality_gap == pytest.approx(0.0)
        assert metrics.is_optimal

    def test_gap_is_zero_when_nothing_was_preventable(self):
        metrics = intervention_metrics(
            baseline_disruption=50.0,
            achieved_disruption=50.0,
            cost=0.0,
            n_actions=0,
            optimal_disruption=50.0,
        )
        assert metrics.optimality_gap == pytest.approx(0.0)


@pytest.fixture(scope="module")
def search_scenario(medium_network):
    """A shock plus its candidate set, shared across the search tests."""
    from lce.simulation.engine import SimulationConfig

    graph = medium_network.graph
    anchor = max(
        medium_network.anchors(), key=lambda m: len(graph.descendants_within(m, 3))
    )
    shock = unit_shock(graph, anchor, fraction_of_buffer=3.0)
    config = SimulationConfig(horizon_hours=168.0, seed=7)
    prediction = LinearThresholdPropagator(
        PropagationConfig(horizon_hours=168.0)
    ).predict(graph, shock)
    candidates = generate_candidates(
        graph,
        shock,
        prediction,
        replace(CandidateConfig(), top_k_nodes=4, max_candidates=12),
        horizon_hours=168.0,
    )
    return graph, shock, config, candidates


class TestCandidates:
    def test_candidates_are_generated_for_exposed_nodes(self, search_scenario):
        _, _, _, candidates = search_scenario
        assert len(candidates) > 0
        assert candidates.targeted_nodes
        assert all(u.cost >= 0 for u in candidates.interventions)

    def test_generation_respects_the_cap(self, search_scenario):
        graph, shock, _, _ = search_scenario
        prediction = LinearThresholdPropagator().predict(graph, shock)
        capped = generate_candidates(
            graph,
            shock,
            prediction,
            replace(CandidateConfig(), top_k_nodes=8, max_candidates=5),
            horizon_hours=168.0,
        )
        assert len(capped) <= 5

    def test_obligation_interventions_reference_real_obligations(self, search_scenario):
        graph, _, _, candidates = search_scenario
        ids = {o.obligation_id for o in graph.obligations}
        for action in candidates.interventions:
            if action.target_obligation_id is not None:
                assert action.target_obligation_id in ids

    def test_type_filter_is_honoured(self, search_scenario):
        graph, shock, _, _ = search_scenario
        prediction = LinearThresholdPropagator().predict(graph, shock)
        only_cash = generate_candidates(
            graph,
            shock,
            prediction,
            replace(
                CandidateConfig(),
                include_types=(InterventionType.LIQUIDITY_INJECTION,),
                top_k_nodes=3,
            ),
            horizon_hours=168.0,
        )
        assert {u.type for u in only_cash.interventions} == {
            InterventionType.LIQUIDITY_INJECTION
        }


class TestSearch:
    def _evaluator(self, scenario):
        graph, shock, config, _ = scenario
        return CounterfactualEvaluator(graph=graph, shock=shock, config=config)

    def test_greedy_never_makes_things_worse(self, search_scenario):
        _, _, _, candidates = search_scenario
        result = GreedySearch().run(
            self._evaluator(search_scenario),
            candidates.interventions,
            SearchConfig(max_actions=2),
        )
        assert result.achieved_disruption <= result.baseline_disruption + 1e-6

    def test_greedy_is_no_better_than_exhaustive(self, search_scenario):
        """Exhaustive search defines the optimum; a heuristic cannot beat it."""
        _, _, _, candidates = search_scenario
        config = SearchConfig(max_actions=2)
        greedy = GreedySearch().run(
            self._evaluator(search_scenario), candidates.interventions, config
        )
        exhaustive = ExhaustiveSearch().run(
            self._evaluator(search_scenario), candidates.interventions, config
        )
        assert exhaustive.achieved_disruption <= greedy.achieved_disruption + 1e-6

    def test_budget_is_respected(self, search_scenario):
        _, _, _, candidates = search_scenario
        budget = 5_000.0
        result = GreedySearch().run(
            self._evaluator(search_scenario),
            candidates.interventions,
            SearchConfig(max_actions=3, budget=budget),
        )
        assert result.cost <= budget + 1e-6
        assert result.plan.is_feasible()

    def test_cardinality_is_respected(self, search_scenario):
        _, _, _, candidates = search_scenario
        result = GreedySearch().run(
            self._evaluator(search_scenario),
            candidates.interventions,
            SearchConfig(max_actions=1),
        )
        assert len(result.plan.interventions) <= 1

    def test_one_action_per_merchant_constraint(self, search_scenario):
        _, _, _, candidates = search_scenario
        result = GreedySearch().run(
            self._evaluator(search_scenario),
            candidates.interventions,
            SearchConfig(max_actions=3, one_per_merchant=True),
        )
        merchants = [u.merchant_id for u in result.plan.interventions]
        assert len(merchants) == len(set(merchants))

    def test_empty_candidate_set_returns_an_empty_plan(self, search_scenario):
        result = GreedySearch().run(
            self._evaluator(search_scenario), [], SearchConfig(max_actions=2)
        )
        assert result.plan.is_empty
        assert result.disruption_prevented == pytest.approx(0.0)

    def test_lazy_greedy_is_a_documented_approximation(self, search_scenario):
        """Lazy evaluation may differ from eager: the objective is not submodular."""
        _, _, _, candidates = search_scenario
        config = SearchConfig(max_actions=2, lazy=True)
        lazy = GreedySearch().run(
            self._evaluator(search_scenario), candidates.interventions, config
        )
        eager = GreedySearch().run(
            self._evaluator(search_scenario),
            candidates.interventions,
            replace(config, lazy=False),
        )
        # Both must be feasible and non-harmful; equality is not guaranteed.
        assert lazy.achieved_disruption <= lazy.baseline_disruption + 1e-6
        assert eager.achieved_disruption <= eager.baseline_disruption + 1e-6

    def test_exhaustive_refuses_intractable_instances(self, search_scenario):
        graph, shock, config, _ = search_scenario
        many = [
            Intervention(
                type=InterventionType.LIQUIDITY_INJECTION,
                merchant_id=f"m{i:04d}",
                t=0.0,
                amount=1000.0,
            )
            for i in range(200)
        ]
        with pytest.raises(OptimizationError, match="exhaustive"):
            ExhaustiveSearch().run(
                CounterfactualEvaluator(graph=graph, shock=shock, config=config),
                many,
                SearchConfig(max_actions=5),
            )

    def test_top_exposure_control_is_a_real_floor(self, search_scenario):
        """The naive control exists so the optimiser has something to beat."""
        _, _, _, candidates = search_scenario
        config = SearchConfig(max_actions=2)
        naive = TopExposureSearch().run(
            self._evaluator(search_scenario), candidates.interventions, config
        )
        greedy = GreedySearch().run(
            self._evaluator(search_scenario), candidates.interventions, config
        )
        assert greedy.disruption_prevented >= naive.disruption_prevented - 1e-6

    @pytest.mark.skipif(not HAS_ORTOOLS, reason="needs the ortools extra")
    @pytest.mark.requires_ortools
    def test_cp_sat_respects_constraints(self, search_scenario):
        _, _, _, candidates = search_scenario
        result = CpSatSearch().run(
            self._evaluator(search_scenario),
            candidates.interventions,
            SearchConfig(max_actions=2, budget=10_000.0),
        )
        assert len(result.plan.interventions) <= 2
        assert result.cost <= 10_000.0 + 1e-6
        # The reported disruption must be re-simulated, not the linear surrogate.
        assert "surrogate_objective" in result.notes or result.plan.is_empty

    def test_registry_builds_each_strategy(self):
        for kind in (
            OptimizerKind.GREEDY,
            OptimizerKind.EXHAUSTIVE,
            OptimizerKind.TOP_EXPOSURE,
        ):
            assert build_search(kind) is not None


class TestHarness:
    def test_ground_truth_differences_against_the_baseline(self, medium_network):
        from lce.simulation.engine import SimulationConfig

        graph = medium_network.graph
        config = SimulationConfig(horizon_hours=168.0, seed=7)
        shock = unit_shock(graph, medium_network.anchors()[0], fraction_of_buffer=3.0)
        truth = build_ground_truth(graph, shock, config=config)

        assert set(truth.affected).isdisjoint(set(truth.baseline.affected_ids))
        assert truth.attributable_disruption >= 0.0
        assert set(truth.affected) <= set(truth.shocked.affected_ids)

    def test_prediction_evaluation_produces_headline_numbers(self, medium_network):
        from lce.simulation.engine import SimulationConfig

        graph = medium_network.graph
        config = SimulationConfig(horizon_hours=168.0, seed=7)
        shock = unit_shock(graph, medium_network.anchors()[0], fraction_of_buffer=3.0)
        truth = build_ground_truth(graph, shock, config=config)
        prediction = LinearThresholdPropagator(
            PropagationConfig(horizon_hours=168.0)
        ).predict(graph, shock)

        evaluation = evaluate_prediction(prediction, truth, graph, name="test")
        headline = evaluation.headline()
        assert 0.0 <= headline["precision"] <= 1.0
        assert 0.0 <= headline["recall"] <= 1.0
        assert evaluation.by_horizon  # sliced views are populated

    def test_search_evaluation_reports_attribution(self, search_scenario):
        graph, shock, config, candidates = search_scenario
        truth = build_ground_truth(graph, shock, config=config)
        result = GreedySearch().run(
            CounterfactualEvaluator(graph=graph, shock=shock, config=config),
            candidates.interventions,
            SearchConfig(max_actions=2),
        )
        evaluation = evaluate_search(result, truth, optimal_disruption=None)
        assert evaluation.intervention is not None
        assert "attributable_disruption" in evaluation.metadata


class TestSystemicImportance:
    def test_ranking_is_normalised_and_ordered(self, small_network):
        from lce.simulation.engine import SimulationConfig

        graph = small_network.graph
        ranking = compute_systemic_importance(
            graph,
            config=SimulationConfig(horizon_hours=168.0, seed=3),
            shock_fraction=1.5,
            merchants=sorted(graph.merchant_ids)[:8],
        )
        scores = [score for _, score in ranking.ranked()]
        assert scores == sorted(scores, reverse=True)
        assert all(0.0 <= s <= 1.0 for s in scores)
        assert all(v >= 0.0 for v in ranking.simulated.values())

    def test_structural_and_simulated_views_cover_the_same_nodes(self, small_network):
        from lce.simulation.engine import SimulationConfig

        graph = small_network.graph
        targets = sorted(graph.merchant_ids)[:6]
        ranking = compute_systemic_importance(
            graph,
            config=SimulationConfig(horizon_hours=168.0, seed=3),
            merchants=targets,
        )
        assert set(ranking.simulated) == set(targets)
        assert set(ranking.structural) == set(targets)
        assert set(ranking.downstream_counts) == set(targets)
