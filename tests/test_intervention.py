"""Phase-4 tests: the constraints, the solvers, and the counterfactual protocol.

The tests that carry weight here are the invariants. An optimiser searching a
simulated objective will find whatever loophole the simulator leaves it, so the
ones below check that the loopholes are closed: that rescheduling debt cannot
create money, that a deadline cannot be pushed out of the accounting window, that
a heuristic never reports beating an exact optimum, and that the same inputs
produce the same decision.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from conftest import HAS_ORTOOLS
from lce.benchmark.scenarios import (
    ScenarioFamily,
    baseline_affected_set,
    scenario_suite,
)
from lce.data.generator import GeneratorConfig, generate_network
from lce.domain.enums import InterventionType
from lce.domain.intervention import Intervention
from lce.errors import ConfigError, OptimizationError
from lce.execution import RazorpayTestProvider, SimulationProvider, build_provider, execute_plan
from lce.execution.providers import ExecutionError
from lce.intervention.actions import (
    baseline_actions,
    generate_actions,
    rank_by_cash_cover,
    rank_by_degree,
    rank_by_open_deficit,
    standard_injection,
)
from lce.intervention.evaluate import build_outcome, replay, score_against_reference
from lce.intervention.exact import build_surrogate, count_subsets, solve_exact, solve_milp
from lce.intervention.problem import (
    InterventionConstraints,
    ObjectiveSpec,
    accounted_principal,
    check_action,
    check_conservation,
    check_realised_floor,
)
from lce.intervention.profiles import ResourceProfile, budget_for, estimate_simulations
from lce.intervention.robust import UncertaintySpec, build_worlds, robust_select, spread_of
from lce.intervention.scalable import greedy_solve
from lce.models.propagation import LinearThresholdPropagator, PropagationConfig
from lce.simulation.counterfactual import CounterfactualEvaluator
from lce.simulation.engine import SimulationConfig

TINY = {
    "n_merchants": 26,
    "n_layers": 3,
    "history_hours": 18 * 24.0,
    "horizon_hours": 168.0,
    "coverage_low": 0.20,
    "coverage_high": 0.45,
}


# --------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def phase4_network():
    config = replace(GeneratorConfig(), seed=515, **TINY)
    network = generate_network(config)
    sim = SimulationConfig(horizon_hours=config.horizon_hours, seed=515)
    already = baseline_affected_set(network.graph, sim)
    suite = scenario_suite(
        network.graph,
        dataset_id=network.dataset_version,
        seed=515,
        config=sim,
        baseline_affected=already,
    )
    return network, sim, suite


@pytest.fixture(scope="session")
def scenario(phase4_network):
    _, _, suite = phase4_network
    for built in suite:
        if built.spec.family is ScenarioFamily.CONCENTRATED_SHOCK:
            return built
    return suite[0]


@pytest.fixture(scope="session")
def constraints(phase4_network, scenario):
    _, sim, _ = phase4_network
    return InterventionConstraints(
        max_actions=2,
        horizon_hours=sim.horizon_hours,
        decision_time=scenario.shock.onset_t,
    )


@pytest.fixture(scope="session")
def candidates(phase4_network, scenario, constraints):
    """A small feasible candidate set built the way the pipeline builds one."""
    from lce.learning.pointprocess import HawkesDependencyEstimator
    from lce.learning.problem import baseline_payment_stream, build_observed_window

    _, sim, _ = phase4_network
    stream = baseline_payment_stream(scenario.unperturbed_graph, sim)
    window = build_observed_window(scenario, config=sim, baseline_payments=stream)
    learned = HawkesDependencyEstimator().install(window)
    prediction = LinearThresholdPropagator(
        PropagationConfig(horizon_hours=sim.horizon_hours)
    ).predict(learned, scenario.shock)
    return generate_actions(
        learned, scenario.shock, prediction, constraints=constraints, max_candidates=6
    )


def _evaluator(scenario, sim):
    return CounterfactualEvaluator(graph=scenario.graph, shock=scenario.shock, config=sim)


# ------------------------------------------------------------------ feasibility


class TestFeasibility:
    def test_a_well_formed_action_is_feasible(self, scenario, constraints):
        action = standard_injection(
            scenario.graph,
            scenario.shock.origin_ids[0],
            constraints=constraints,
            t=scenario.shock.onset_t,
            rule="test",
        )
        assert action is not None
        assert check_action([action], scenario.graph, constraints).feasible

    def test_budget_is_enforced(self, scenario, constraints):
        action = standard_injection(
            scenario.graph,
            scenario.shock.origin_ids[0],
            constraints=constraints,
            t=scenario.shock.onset_t,
            rule="test",
        )
        tight = replace(constraints, budget=action.cost / 2.0)
        report = check_action([action], scenario.graph, tight)
        assert not report.feasible
        assert "budget" in report.names()

    def test_cardinality_is_enforced(self, scenario, constraints, candidates):
        actions = candidates.interventions[:3]
        if len(actions) < 3:
            pytest.skip("not enough candidates to exceed the cardinality cap")
        report = check_action(actions, scenario.graph, replace(constraints, max_actions=2))
        assert "cardinality" in report.names()

    def test_capacity_is_enforced(self, scenario, constraints):
        merchant = scenario.shock.origin_ids[0]
        pair = [
            standard_injection(
                scenario.graph, merchant, constraints=constraints,
                t=scenario.shock.onset_t, rule=f"test{i}",
            )
            for i in range(2)
        ]
        report = check_action(pair, scenario.graph, constraints)
        assert "capacity" in report.names()

    def test_an_action_cannot_precede_the_decision(self, scenario, constraints):
        action = Intervention(
            type=InterventionType.LIQUIDITY_INJECTION,
            merchant_id=scenario.shock.origin_ids[0],
            t=0.0,
            amount=50_000.0,
        )
        bounds = replace(constraints, decision_time=48.0)
        report = check_action([action], scenario.graph, bounds)
        assert "timing" in report.names()

    def test_term_extension_bound(self, scenario, constraints):
        payables = [
            o
            for o in scenario.graph.payables_of(scenario.shock.origin_ids[0])
            if o.is_open
        ]
        if not payables:
            pytest.skip("target has no open payable")
        action = Intervention(
            type=InterventionType.SUPPLIER_TERM_EXTENSION,
            merchant_id=scenario.shock.origin_ids[0],
            t=scenario.shock.onset_t,
            amount=payables[0].outstanding,
            shift_hours=constraints.max_extension_hours * 3,
            target_obligation_id=payables[0].obligation_id,
        )
        report = check_action([action], scenario.graph, constraints)
        assert "max_term_extension" in report.names()

    def test_a_deadline_may_not_leave_the_horizon(self, scenario, constraints):
        """The Phase-2 loophole: an obligation pushed past T stops being charged."""
        payables = sorted(
            (o for o in scenario.graph.payables_of(scenario.shock.origin_ids[0]) if o.is_open),
            key=lambda o: -o.due_t,
        )
        if not payables:
            pytest.skip("target has no open payable")
        target = payables[0]
        shift = constraints.horizon_hours - target.due_t + 1.0
        action = Intervention(
            type=InterventionType.SUPPLIER_TERM_EXTENSION,
            merchant_id=scenario.shock.origin_ids[0],
            t=scenario.shock.onset_t,
            amount=target.outstanding,
            shift_hours=max(shift, 1.0),
            target_obligation_id=target.obligation_id,
        )
        bounds = replace(constraints, max_extension_hours=constraints.horizon_hours)
        report = check_action([action], scenario.graph, bounds)
        assert "deadline" in report.names()

    def test_restructure_tranche_bound(self, scenario, constraints):
        payables = [
            o for o in scenario.graph.payables_of(scenario.shock.origin_ids[0]) if o.is_open
        ]
        if not payables:
            pytest.skip("target has no open payable")
        action = Intervention(
            type=InterventionType.REPAYMENT_RESTRUCTURE,
            merchant_id=scenario.shock.origin_ids[0],
            t=scenario.shock.onset_t,
            amount=payables[0].outstanding,
            tranches=constraints.max_tranches + 4,
            target_obligation_id=payables[0].obligation_id,
        )
        report = check_action([action], scenario.graph, constraints)
        assert "max_repayment_modification" in report.names()

    def test_unknown_merchant_is_rejected(self, scenario, constraints):
        action = Intervention(
            type=InterventionType.LIQUIDITY_INJECTION,
            merchant_id="not_a_merchant",
            t=scenario.shock.onset_t,
            amount=10_000.0,
        )
        assert "unknown_merchant" in check_action(
            [action], scenario.graph, constraints
        ).names()

    def test_candidate_generation_filters_before_it_ranks(self, candidates, scenario, constraints):
        assert candidates.n_feasible <= candidates.n_generated
        for action in candidates.interventions:
            assert check_action([action], scenario.graph, constraints).feasible

    def test_every_candidate_carries_its_reasoning(self, candidates):
        for entry in candidates.scored:
            provenance = entry.intervention.provenance
            assert provenance["stage"] == "candidate_generation"
            assert set(provenance["factors"]) == set(entry.factors)


# ------------------------------------------------------------------ invariants


class TestInvariants:
    def test_rescheduling_cannot_create_money(self, scenario, phase4_network, constraints):
        """Every non-capital action must conserve accounted principal."""
        _, sim, _ = phase4_network
        merchant = scenario.shock.origin_ids[0]
        payables = [o for o in scenario.graph.payables_of(merchant) if o.is_open]
        if not payables:
            pytest.skip("target has no open payable")
        target = max(payables, key=lambda o: o.outstanding)

        none = replay(scenario.graph, scenario.shock, [], config=sim, run_id="none")
        for action in (
            Intervention(
                type=InterventionType.SUPPLIER_TERM_EXTENSION,
                merchant_id=merchant,
                t=scenario.shock.onset_t,
                amount=target.outstanding,
                shift_hours=24.0,
                target_obligation_id=target.obligation_id,
            ),
            Intervention(
                type=InterventionType.REPAYMENT_RESTRUCTURE,
                merchant_id=merchant,
                t=scenario.shock.onset_t,
                amount=target.outstanding,
                tranches=3,
                target_obligation_id=target.obligation_id,
            ),
        ):
            after = replay(scenario.graph, scenario.shock, [action], config=sim, run_id="act")
            report = check_conservation(none.obligations, after.obligations, [action])
            assert report.feasible, report.to_dict()

    def test_a_restructure_conserves_principal_exactly(self, scenario, phase4_network):
        _, sim, _ = phase4_network
        merchant = scenario.shock.origin_ids[0]
        payables = [o for o in scenario.graph.payables_of(merchant) if o.is_open]
        if not payables:
            pytest.skip("target has no open payable")
        target = max(payables, key=lambda o: o.outstanding)
        action = Intervention(
            type=InterventionType.REPAYMENT_RESTRUCTURE,
            merchant_id=merchant,
            t=scenario.shock.onset_t,
            amount=target.outstanding,
            tranches=4,
            target_obligation_id=target.obligation_id,
        )
        none = replay(scenario.graph, scenario.shock, [], config=sim, run_id="none")
        after = replay(scenario.graph, scenario.shock, [action], config=sim, run_id="act")
        assert accounted_principal(after.obligations) == pytest.approx(
            accounted_principal(none.obligations), rel=1e-6
        )

    def test_conservation_catches_a_fabricated_book(self, scenario, phase4_network):
        """Negative control: shrink the book and the invariant must fire."""
        _, sim, _ = phase4_network
        none = replay(scenario.graph, scenario.shock, [], config=sim, run_id="none")
        tampered = [
            o.model_copy(update={"amount": o.amount * 0.5, "amount_paid": 0.0})
            for o in none.obligations
        ]
        action = Intervention(
            type=InterventionType.SUPPLIER_TERM_EXTENSION,
            merchant_id=scenario.shock.origin_ids[0],
            t=scenario.shock.onset_t,
            amount=1000.0,
            shift_hours=24.0,
            target_obligation_id=none.obligations[0].obligation_id,
        )
        report = check_conservation(none.obligations, tampered, [action])
        assert "no_money_creation" in report.names()

    def test_capital_actions_are_exempt_from_conservation(self, scenario, phase4_network):
        _, sim, _ = phase4_network
        none = replay(scenario.graph, scenario.shock, [], config=sim, run_id="none")
        injection = Intervention(
            type=InterventionType.LIQUIDITY_INJECTION,
            merchant_id=scenario.shock.origin_ids[0],
            t=scenario.shock.onset_t,
            amount=100_000.0,
        )
        assert check_conservation(none.obligations, [], [injection]).feasible

    def test_liquidity_floor_is_measured_against_doing_nothing(self, scenario, phase4_network):
        """A node already under water without an action is not the action's fault."""
        _, sim, _ = phase4_network
        none = replay(scenario.graph, scenario.shock, [], config=sim, run_id="none")
        assert check_realised_floor(none.cascade.outcomes, none.cascade.outcomes).feasible

    def test_liquidity_floor_fires_when_an_action_makes_it_worse(self):
        class _Outcome:
            def __init__(self, value: float) -> None:
                self.min_buffer = value

        baseline = {"m1": _Outcome(-100.0)}
        worse = {"m1": _Outcome(-500.0)}
        assert check_realised_floor(baseline, baseline).feasible
        assert "liquidity_floor" in check_realised_floor(worse, baseline).names()


# --------------------------------------------------------------------- solvers


class TestSolvers:
    def test_exact_matches_a_brute_force_search(self, scenario, phase4_network, constraints):
        """Cross-check the reference optimum against an independent enumeration."""
        import itertools

        _, sim, _ = phase4_network
        pool = _small_pool(scenario, constraints)
        if len(pool) < 2:
            pytest.skip("not enough feasible candidates")

        objective = ObjectiveSpec(lam=1.0)
        solved = solve_exact(
            _evaluator(scenario, sim), pool, scenario.graph,
            constraints=constraints, objective=objective,
        )

        evaluator = _evaluator(scenario, sim)
        best = None
        for size in range(0, constraints.max_actions + 1):
            for subset in itertools.combinations(pool, size):
                chosen = list(subset)
                if chosen and not check_action(chosen, scenario.graph, constraints).feasible:
                    continue
                value = objective.value(
                    evaluator.disruption(chosen), sum(u.cost for u in chosen)
                )
                if best is None or value < best:
                    best = value
        assert solved.objective_value == pytest.approx(best, rel=1e-9)

    def test_exact_may_choose_to_do_nothing(self, scenario, phase4_network, constraints):
        """At a high enough cost weight, the optimum is the empty plan."""
        _, sim, _ = phase4_network
        pool = _small_pool(scenario, constraints)
        solved = solve_exact(
            _evaluator(scenario, sim), pool, scenario.graph,
            constraints=constraints, objective=ObjectiveSpec(lam=1e9),
        )
        assert solved.interventions == []
        assert solved.status == "OPTIMAL"

    def test_exact_refuses_an_unaffordable_enumeration(self, scenario, constraints, phase4_network):
        _, sim, _ = phase4_network
        pool = _small_pool(scenario, constraints)
        if not pool:
            pytest.skip("no candidates")
        with pytest.raises(OptimizationError, match="subsets"):
            solve_exact(
                _evaluator(scenario, sim), pool, scenario.graph,
                constraints=constraints, objective=ObjectiveSpec(), subset_cap=1,
            )

    def test_greedy_never_beats_the_exact_optimum(self, scenario, phase4_network, constraints):
        _, sim, _ = phase4_network
        pool = _small_pool(scenario, constraints)
        if len(pool) < 2:
            pytest.skip("not enough feasible candidates")
        objective = ObjectiveSpec(lam=1.0)
        exact = solve_exact(
            _evaluator(scenario, sim), pool, scenario.graph,
            constraints=constraints, objective=objective,
        )
        greedy = greedy_solve(
            _evaluator(scenario, sim), pool, scenario.graph,
            constraints=constraints, objective=objective,
        )
        assert greedy.objective_value >= exact.objective_value - 1e-6

    def test_optimisation_is_deterministic(self, scenario, phase4_network, constraints):
        _, sim, _ = phase4_network
        pool = _small_pool(scenario, constraints)
        first = greedy_solve(
            _evaluator(scenario, sim), pool, scenario.graph, constraints=constraints
        )
        second = greedy_solve(
            _evaluator(scenario, sim), pool, scenario.graph, constraints=constraints
        )
        assert [u.intervention_id for u in first.interventions] == [
            u.intervention_id for u in second.interventions
        ]
        assert first.disruption == second.disruption

    def test_constrained_form_respects_its_ceiling(self, scenario, phase4_network, constraints):
        _, sim, _ = phase4_network
        pool = _small_pool(scenario, constraints)
        if not pool:
            pytest.skip("no candidates")
        objective = ObjectiveSpec(form="constrained", epsilon_fraction=0.999)
        solved = solve_exact(
            _evaluator(scenario, sim), pool, scenario.graph,
            constraints=constraints, objective=objective,
        )
        if solved.feasible:
            ceiling = objective.resolve_epsilon(solved.baseline_disruption)
            assert solved.disruption <= ceiling + 1e-6

    def test_surrogate_measures_interactions(self, scenario, phase4_network, constraints):
        _, sim, _ = phase4_network
        pool = _small_pool(scenario, constraints)[:4]
        if len(pool) < 2:
            pytest.skip("not enough candidates")
        surrogate = build_surrogate(_evaluator(scenario, sim), pool, pairwise=True)
        assert surrogate.pairwise
        assert len(surrogate.gains) == len(pool)
        assert len(surrogate.residuals) == len(pool) * (len(pool) - 1) // 2

    @pytest.mark.requires_ortools
    @pytest.mark.skipif(not HAS_ORTOOLS, reason="needs the opt extra")
    def test_milp_reports_a_re_simulated_value(self, scenario, phase4_network, constraints):
        _, sim, _ = phase4_network
        pool = _small_pool(scenario, constraints)[:4]
        if len(pool) < 2:
            pytest.skip("not enough candidates")
        evaluator = _evaluator(scenario, sim)
        solved = solve_milp(
            evaluator, pool, scenario.graph,
            constraints=constraints, objective=ObjectiveSpec(), time_limit_s=5.0,
        )
        assert solved.status in {"OPTIMAL", "FEASIBLE"}
        # The reported disruption is the simulator's, not the surrogate's.
        assert solved.disruption == pytest.approx(
            evaluator.disruption(solved.interventions)
        )

    @pytest.mark.requires_ortools
    @pytest.mark.skipif(not HAS_ORTOOLS, reason="needs the opt extra")
    def test_milp_survives_a_tiny_time_limit(self, scenario, phase4_network, constraints):
        """A solver that runs out of time must still return something usable."""
        _, sim, _ = phase4_network
        pool = _small_pool(scenario, constraints)[:4]
        if len(pool) < 2:
            pytest.skip("not enough candidates")
        solved = solve_milp(
            _evaluator(scenario, sim), pool, scenario.graph,
            constraints=constraints, objective=ObjectiveSpec(), time_limit_s=0.001,
        )
        assert solved.status in {"OPTIMAL", "FEASIBLE", "UNKNOWN", "INFEASIBLE"}
        assert solved.runtime_s >= 0.0

    def test_subset_counting(self):
        assert count_subsets(12, 2) == 1 + 12 + 66
        assert count_subsets(3, 10) == 8


def _small_pool(scenario, constraints):
    """A handful of feasible, cheap candidates - enough to enumerate quickly."""
    graph = scenario.graph
    pool = []
    for merchant_id in sorted(graph.merchant_ids)[:6]:
        action = standard_injection(
            graph,
            merchant_id,
            constraints=constraints,
            t=scenario.shock.onset_t,
            rule="pool",
            fraction_of_payables=0.5,
        )
        if action is not None:
            pool.append(action)
    return pool[:4]


# ----------------------------------------------------------- counterfactuals


class TestCounterfactualEvaluation:
    def test_replay_reports_the_simulator_not_the_model(self, scenario, phase4_network, constraints):
        _, sim, _ = phase4_network
        none = replay(scenario.graph, scenario.shock, [], config=sim, run_id="none")
        pool = _small_pool(scenario, constraints)
        if not pool:
            pytest.skip("no candidates")

        outcome = build_outcome(
            "probe", pool[:1], graph=scenario.graph, shock=scenario.shock,
            config=sim, constraints=constraints, no_intervention=none,
            predicted_disruption=1.0,
        )
        direct = replay(scenario.graph, scenario.shock, pool[:1], config=sim, run_id="x")
        assert outcome.true_disruption == pytest.approx(direct.disruption)
        assert outcome.predicted_disruption == 1.0
        assert outcome.prediction_error == pytest.approx(1.0 - direct.disruption)

    def test_doing_nothing_prevents_nothing(self, scenario, phase4_network, constraints):
        _, sim, _ = phase4_network
        none = replay(scenario.graph, scenario.shock, [], config=sim, run_id="none")
        outcome = build_outcome(
            "no_intervention", [], graph=scenario.graph, shock=scenario.shock,
            config=sim, constraints=constraints, no_intervention=none,
        )
        assert outcome.disruption_prevented == 0.0
        assert outcome.cost == 0.0
        assert outcome.violations == []

    def test_gaps_are_only_scored_against_a_real_reference(self, scenario, phase4_network, constraints):
        from lce.intervention.evaluate import CounterfactualReport

        _, sim, _ = phase4_network
        none = replay(scenario.graph, scenario.shock, [], config=sim, run_id="none")
        report = CounterfactualReport(objective=ObjectiveSpec())
        report.outcomes.append(
            build_outcome(
                "no_intervention", [], graph=scenario.graph, shock=scenario.shock,
                config=sim, constraints=constraints, no_intervention=none,
            )
        )
        score_against_reference(report, "does_not_exist", ObjectiveSpec())
        assert report.reference is None
        assert report.outcomes[0].relative_gap is None

    def test_baseline_rules_produce_feasible_actions(self, scenario, constraints):
        graph = scenario.graph
        horizon = constraints.horizon_hours
        for ranking in (
            rank_by_open_deficit(graph, horizon),
            rank_by_degree(graph),
            rank_by_cash_cover(graph, horizon),
        ):
            actions = baseline_actions(
                graph, ranking, constraints=constraints, t=scenario.shock.onset_t,
                rule="probe", max_actions=1,
            )
            for action in actions:
                assert check_action([action], graph, constraints).feasible


# -------------------------------------------------------------------- robust


class TestRobustness:
    def test_worlds_are_deterministic(self, scenario, phase4_network):
        _, sim, _ = phase4_network
        spec = UncertaintySpec(n_scenarios=4, seed=11)
        first = build_worlds(scenario.shock, sim, spec)
        second = build_worlds(scenario.shock, sim, spec)
        assert [w.magnitude_factor for w in first] == [w.magnitude_factor for w in second]
        assert first[0].name == "nominal"
        assert first[0].shock.total_magnitude == scenario.shock.total_magnitude

    def test_kappa_zero_is_expected_disruption(self, scenario, phase4_network, constraints):
        _, sim, _ = phase4_network
        pool = _small_pool(scenario, constraints)[:2]
        spec = UncertaintySpec(n_scenarios=3, kappa=0.0, seed=5)
        result = robust_select(
            scenario.graph, [[], *[[u] for u in pool]], shock=scenario.shock,
            config=sim, constraints=constraints,
            objective=ObjectiveSpec(lam=0.0), spec=spec,
        )
        assert result.chosen.robust_value == pytest.approx(result.chosen.mean_disruption)

    def test_spread_measures(self):
        values = np.array([1.0, 2.0, 3.0, 10.0])
        std = spread_of(values, UncertaintySpec(spread_measure="std"))
        cvar = spread_of(values, UncertaintySpec(spread_measure="cvar", cvar_alpha=0.5))
        assert std > 0
        assert cvar > 0
        assert spread_of(np.array([5.0]), UncertaintySpec()) == 0.0


# ------------------------------------------------------------------ profiles


class TestProfiles:
    def test_every_profile_is_bounded(self):
        for profile in ResourceProfile:
            budget = budget_for(profile)
            assert budget.max_candidates > 0
            assert budget.max_actions > 0
            assert budget.solver_time_limit_s > 0

    def test_small_fast_can_afford_its_exact_search(self):
        budget = budget_for(ResourceProfile.SMALL_FAST)
        assert budget.exact_optimum
        assert estimate_simulations(budget, method="exact") == budget.exact_subset_count
        assert budget.exact_subset_count < 200

    def test_large_demo_disables_the_quadratic_surrogate(self):
        budget = budget_for(ResourceProfile.LARGE_DEMO)
        assert not budget.pairwise_surrogate
        assert not budget.exact_optimum
        assert estimate_simulations(budget, method="milp") < 100


# ------------------------------------------------------------------ execution


class TestExecutionProviders:
    def test_simulation_provider_accepts_every_type(self):
        provider = SimulationProvider()
        assert all(provider.capabilities().values())

    def test_simulation_provider_plans_by_default(self):
        action = Intervention(
            type=InterventionType.LIQUIDITY_INJECTION,
            merchant_id="m1", t=0.0, amount=1000.0,
        )
        assert SimulationProvider().execute(action).status == "planned"
        assert SimulationProvider().execute(action, dry_run=False).executed

    def test_razorpay_provider_refuses_live_mode(self):
        from lce.config import RazorpayMode, RazorpaySettings

        settings = RazorpaySettings(RAZORPAY_MODE=RazorpayMode.LIVE)
        with pytest.raises(ConfigError, match="Test Mode"):
            RazorpayTestProvider(settings=settings)

    def test_unconfigured_razorpay_plans_rather_than_failing(self):
        action = Intervention(
            type=InterventionType.LIQUIDITY_INJECTION,
            merchant_id="m1", t=0.0, amount=1000.0,
        )
        record = RazorpayTestProvider().execute(action)
        assert record.status == "planned"
        assert "transfers" in record.detail["reason"]

    def test_amounts_map_to_paise(self):
        action = Intervention(
            type=InterventionType.LIQUIDITY_INJECTION,
            merchant_id="m1", t=0.0, amount=1234.56,
        )
        mapped = RazorpayTestProvider().map_to_request(action)
        assert mapped["endpoint"] == "/transfers"
        assert mapped["body"]["amount"] == 123456

    def test_term_actions_have_no_payments_endpoint(self):
        action = Intervention(
            type=InterventionType.REPAYMENT_RESTRUCTURE,
            merchant_id="m1", t=0.0, amount=1000.0, tranches=3,
            target_obligation_id="obl_1",
        )
        mapped = RazorpayTestProvider().map_to_request(action)
        assert mapped["endpoint"] is None
        assert "lending-ledger" in mapped["note"]

    def test_no_secret_reaches_a_record(self):
        action = Intervention(
            type=InterventionType.LIQUIDITY_INJECTION,
            merchant_id="m1", t=0.0, amount=1000.0,
        )
        record = RazorpayTestProvider().execute(action)
        blob = repr(record.to_dict())
        assert "key_secret" not in blob
        assert "secret" not in blob.lower() or "webhook" not in blob.lower()

    def test_unknown_provider_is_rejected(self):
        with pytest.raises(ExecutionError, match="unknown execution provider"):
            build_provider("not_a_provider")

    def test_execute_plan_returns_one_record_per_action(self):
        actions = [
            Intervention(
                type=InterventionType.LIQUIDITY_INJECTION,
                merchant_id=f"m{i}", t=0.0, amount=1000.0,
            )
            for i in range(3)
        ]
        records = execute_plan(SimulationProvider(), actions)
        assert len(records) == 3
        assert {r.intervention_id for r in records} == {a.intervention_id for a in actions}


# ---------------------------------------------------------------- the pipeline


class TestPipeline:
    @pytest.mark.slow
    def test_end_to_end_scenario(self, scenario, phase4_network):
        """Predict, decide, replay, compare - the acceptance path, in miniature."""
        from lce.intervention.experiment import Phase4Config, run_scenario

        _, sim, _ = phase4_network
        config = Phase4Config(
            profile=ResourceProfile.SMALL_FAST,
            seeds=(515,),
            robust=False,
            systemic=False,
            pruning_benchmark=False,
        )
        result = run_scenario(scenario, config=config, sim_config=sim)

        names = {o.name for o in result.counterfactual.outcomes}
        assert "no_intervention" in names
        assert "model_guided_greedy" in names
        assert result.prediction["source"] == "propagation"

        # The reference optimum, where one was computed, must dominate.
        if "exact_optimum" in names:
            optimum = result.counterfactual.by_name("exact_optimum")
            for outcome in result.counterfactual.outcomes:
                assert outcome.objective_value(
                    config.objective
                ) >= optimum.objective_value(config.objective) - 1e-6

    @pytest.mark.slow
    def test_the_same_config_reproduces_the_same_decision(self, scenario, phase4_network):
        from lce.intervention.experiment import Phase4Config, run_scenario

        _, sim, _ = phase4_network
        config = Phase4Config(
            profile=ResourceProfile.SMALL_FAST, seeds=(515,),
            robust=False, systemic=False, pruning_benchmark=False,
        )
        first = run_scenario(scenario, config=config, sim_config=sim)
        second = run_scenario(scenario, config=config, sim_config=sim)

        def chosen(result):
            outcome = result.counterfactual.by_name("model_guided_greedy")
            return [(u.type, u.merchant_id, round(u.amount, 6)) for u in outcome.interventions]

        assert chosen(first) == chosen(second)
        assert first.counterfactual.by_name(
            "model_guided_greedy"
        ).true_disruption == pytest.approx(
            second.counterfactual.by_name("model_guided_greedy").true_disruption
        )

    def test_config_hash_is_sensitive_to_the_objective(self):
        from lce.intervention.experiment import Phase4Config

        base = Phase4Config(seeds=(1,))
        assert base.config_hash == Phase4Config(seeds=(1,)).config_hash
        assert base.config_hash != Phase4Config(
            seeds=(1,), objective=ObjectiveSpec(lam=2.0)
        ).config_hash

    def test_unknown_predictor_is_rejected(self):
        from lce.intervention.experiment import Phase4Config

        with pytest.raises(ValueError, match="unknown predictor"):
            Phase4Config(predictor="magic")
