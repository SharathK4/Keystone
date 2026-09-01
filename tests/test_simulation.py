"""Simulator correctness.

The properties here are the ones the rest of the system depends on. If shocks
can reduce disruption, or two identical runs disagree, then every downstream
measurement is noise.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from lce.domain.enums import InterventionType, NodeStatus, ObligationStatus, ShockKind
from lce.domain.events import EXTERNAL_SINK, Obligation
from lce.domain.intervention import Intervention, InterventionPlan
from lce.domain.merchant import MerchantProfile
from lce.domain.shock import Shock
from lce.graph.builders import build_graph
from lce.simulation.counterfactual import CounterfactualEvaluator
from lce.simulation.engine import LiquiditySimulator, SimulationConfig
from lce.simulation.scenarios import missed_receivable_shock, unit_shock
from lce.simulation.state import NodeState


@pytest.fixture
def chain_graph():
    """A -> B -> C chain where B cannot pay C unless A pays B.

    Deliberately hand-built rather than generated: the cascade is then a
    property of the arithmetic, not of the generator's calibration.
    """
    profiles = [
        MerchantProfile(
            merchant_id="A", opening_balance=1_000_000.0, operating_floor=0.0, credit_limit=0.0
        ),
        MerchantProfile(
            merchant_id="B", opening_balance=10_000.0, operating_floor=0.0, credit_limit=0.0
        ),
        MerchantProfile(
            merchant_id="C", opening_balance=10_000.0, operating_floor=0.0, credit_limit=0.0
        ),
    ]
    graph = build_graph(profiles)
    graph.add_obligation(
        Obligation(
            obligation_id="obl_ab",
            debtor_id="A",
            creditor_id="B",
            amount=500_000.0,
            issued_t=0.0,
            due_t=10.0,
        )
    )
    graph.add_obligation(
        Obligation(
            obligation_id="obl_bc",
            debtor_id="B",
            creditor_id="C",
            amount=400_000.0,
            issued_t=0.0,
            due_t=40.0,
        )
    )
    return graph


@pytest.fixture
def chain_config():
    return SimulationConfig(
        horizon_hours=120.0,
        tick_hours=1.0,
        seed=5,
        apply_payment_discipline=False,  # isolate liquidity effects from lateness
    )


class TestNodeState:
    def test_disburse_draws_credit_when_cash_is_short(self):
        profile = MerchantProfile(
            merchant_id="m", opening_balance=100.0, credit_limit=500.0
        )
        state = NodeState.from_profile(profile)
        paid = state.disburse(400.0)
        assert paid == pytest.approx(400.0)
        assert state.cash == pytest.approx(0.0)
        assert state.credit_drawn == pytest.approx(300.0)

    def test_disburse_is_capped_by_available_resources(self):
        profile = MerchantProfile(merchant_id="m", opening_balance=100.0, credit_limit=50.0)
        state = NodeState.from_profile(profile)
        assert state.disburse(1000.0) == pytest.approx(150.0)

    def test_receive_repays_drawn_credit_first(self):
        profile = MerchantProfile(merchant_id="m", opening_balance=0.0, credit_limit=500.0)
        state = NodeState.from_profile(profile)
        state.disburse(300.0)
        state.receive(200.0)
        assert state.credit_drawn == pytest.approx(100.0)
        assert state.cash == pytest.approx(0.0)

    def test_operating_burn_may_breach_the_floor(self):
        # Opex is non-discretionary; the deficit term exists to measure this.
        profile = MerchantProfile(
            merchant_id="m",
            opening_balance=100.0,
            operating_floor=50.0,
            operating_burn_rate=10.0,
        )
        state = NodeState.from_profile(profile)
        state.accrue(20.0)
        assert state.cash == pytest.approx(-100.0)
        assert state.deficit == pytest.approx(150.0)


class TestCascade:
    def test_undisturbed_baseline_clears_the_chain(self, chain_graph, chain_config):
        result = LiquiditySimulator(chain_graph, chain_config).run(None, run_id="base")
        assert result.affected_ids == []
        assert result.disruption == pytest.approx(0.0, abs=1e-6)

    def test_shock_propagates_one_hop_downstream(self, chain_graph, chain_config):
        """A cannot pay B, so B cannot pay C: the defining behaviour."""
        shock = Shock.single("A", magnitude=999_000.0, t=0.0, kind=ShockKind.CASH_WITHDRAWAL)
        result = LiquiditySimulator(chain_graph, chain_config).run(shock, run_id="shocked")

        assert "A" in result.affected_ids
        assert "B" in result.affected_ids
        assert result.outcomes["A"].hop_distance == 0
        assert result.outcomes["B"].hop_distance == 1
        assert result.outcomes["B"].is_downstream_affected
        assert result.outcomes["B"].first_constrained_t >= 40.0

    def test_causal_chain_walks_back_to_the_shock(self, chain_graph, chain_config):
        shock = Shock.single("A", magnitude=999_000.0, t=0.0, kind=ShockKind.CASH_WITHDRAWAL)
        result = LiquiditySimulator(chain_graph, chain_config).run(shock, run_id="shocked")

        b_events = [e for e in result.events_for("B") if e.is_impact]
        assert b_events
        chain = result.causal_chain(b_events[-1].event_id)
        assert len(chain) >= 2
        assert chain[0].caused_by is None  # walked back to a root

    def test_missed_inbound_writes_off_the_receivable(self, chain_graph, chain_config):
        shock = missed_receivable_shock(chain_graph, "B", t=0.0)
        result = LiquiditySimulator(chain_graph, chain_config).run(shock, run_id="s")
        assert "B" in result.affected_ids

    def test_defaults_require_the_grace_period_to_elapse(self, chain_graph):
        shock = Shock.single("A", magnitude=999_000.0, t=0.0, kind=ShockKind.CASH_WITHDRAWAL)
        short = SimulationConfig(
            horizon_hours=50.0, seed=5, grace_period_hours=48.0,
            apply_payment_discipline=False,
        )
        result = LiquiditySimulator(chain_graph, short).run(shock, run_id="s")
        # A missed at t=10; the write-off point is 10 + 48 = 58, beyond a 50h run.
        assert result.outcomes["A"].obligations_missed == 1
        assert result.outcomes["A"].defaults_caused == 0

        long = replace(short, horizon_hours=120.0)
        later = LiquiditySimulator(chain_graph, long).run(shock, run_id="s")
        assert later.outcomes["A"].defaults_caused == 1

    def test_external_sink_misses_do_not_create_contagion(self, chain_config):
        """Missing payroll hurts the payer but starves no modelled merchant.

        Non-network sinks (payroll, tax, rent) compete for the same buffer and
        must show up as damage to the debtor, but they must not manufacture a
        contagion edge - there is no downstream merchant waiting on that cash.
        """
        graph = build_graph(
            [
                MerchantProfile(merchant_id="A", opening_balance=1000.0),
                MerchantProfile(merchant_id="B", opening_balance=1000.0),
            ]
        )
        graph.add_obligation(
            Obligation(
                debtor_id="A",
                creditor_id=EXTERNAL_SINK,
                amount=500_000.0,
                issued_t=0.0,
                due_t=10.0,
            ),
            require_nodes=False,
        )
        result = LiquiditySimulator(graph, chain_config).run(None, run_id="s")

        assert result.affected_ids == ["A"]
        assert result.outcomes["A"].obligations_missed == 1
        # B has no relationship to A's payroll and must stay clean.
        assert not result.outcomes["B"].is_affected
        assert result.outcomes["B"].hop_distance is None


class TestDeterminism:
    def test_identical_runs_are_bit_identical(self, medium_network, sim_config):
        graph = medium_network.graph
        shock = unit_shock(graph, medium_network.anchors()[0], fraction_of_buffer=2.0)
        first = LiquiditySimulator(graph, sim_config).run(shock, run_id="a")
        second = LiquiditySimulator(graph, sim_config).run(shock, run_id="b")

        assert first.disruption == pytest.approx(second.disruption, rel=0, abs=0)
        assert first.affected_ids == second.affected_ids
        assert len(first.events) == len(second.events)

    def test_common_random_numbers_survive_a_different_run_id(
        self, medium_network, sim_config
    ):
        """Counterfactuals must share idiosyncratic draws with the baseline.

        If the RNG were keyed on run_id, D(baseline) - D(plan) would measure
        sampling noise rather than the intervention's causal effect.
        """
        graph = medium_network.graph
        baseline_a = LiquiditySimulator(graph, sim_config).run(None, run_id="alpha")
        baseline_b = LiquiditySimulator(graph, sim_config).run(None, run_id="omega")
        assert baseline_a.disruption == pytest.approx(baseline_b.disruption, abs=0)

    def test_different_seeds_change_the_outcome(self, medium_network, sim_config):
        graph = medium_network.graph
        other = replace(sim_config, seed=sim_config.seed + 1)
        a = LiquiditySimulator(graph, sim_config).run(None, run_id="x")
        b = LiquiditySimulator(graph, other).run(None, run_id="x")
        assert a.disruption != pytest.approx(b.disruption, abs=1e-9)


class TestMonotonicity:
    def test_a_shock_never_reduces_disruption(self, medium_network, sim_config):
        """The objective must be monotone in the shock, or the metric is broken.

        This caught a real defect: charging the delay penalty only on *full*
        settlement let a part-paying node escape the penalty on everything it
        did pay, so starving a node could score as an improvement.
        """
        graph = medium_network.graph
        baseline = LiquiditySimulator(graph, sim_config).run(None, run_id="base")

        for merchant_id in medium_network.anchors()[:4]:
            shock = unit_shock(graph, merchant_id, fraction_of_buffer=2.0)
            shocked = LiquiditySimulator(graph, sim_config).run(shock, run_id="s")
            assert shocked.disruption >= baseline.disruption - 1e-6, (
                f"shock at {merchant_id} reduced disruption: "
                f"{shocked.disruption} < {baseline.disruption}"
            )

    def test_a_bigger_shock_hurts_at_least_as_much(self, medium_network, sim_config):
        graph = medium_network.graph
        anchor = medium_network.anchors()[0]
        small = LiquiditySimulator(graph, sim_config).run(
            unit_shock(graph, anchor, fraction_of_buffer=1.0), run_id="s"
        )
        large = LiquiditySimulator(graph, sim_config).run(
            unit_shock(graph, anchor, fraction_of_buffer=4.0), run_id="s"
        )
        assert large.disruption >= small.disruption - 1e-6

    def test_affected_set_grows_with_the_horizon(self, medium_network, sim_config):
        graph = medium_network.graph
        shock = unit_shock(graph, medium_network.anchors()[0], fraction_of_buffer=2.5)
        result = LiquiditySimulator(graph, sim_config).run(shock, run_id="s")
        sizes = [len(result.affected_by(t)) for t in (6, 24, 48, 72, 168)]
        assert sizes == sorted(sizes)


class TestInterventions:
    def test_injection_reduces_disruption(self, chain_graph, chain_config):
        shock = Shock.single("A", magnitude=999_000.0, t=0.0, kind=ShockKind.CASH_WITHDRAWAL)
        without = LiquiditySimulator(chain_graph, chain_config).run(shock, run_id="s")

        plan = InterventionPlan(
            interventions=[
                Intervention(
                    type=InterventionType.LIQUIDITY_INJECTION,
                    merchant_id="A",
                    t=0.0,
                    amount=600_000.0,
                )
            ]
        )
        with_plan = LiquiditySimulator(chain_graph, chain_config).run(shock, plan, run_id="s")
        assert with_plan.disruption < without.disruption
        assert "B" not in with_plan.affected_ids

    def test_credit_line_increase_restores_capacity(self, chain_graph, chain_config):
        shock = Shock.single("A", magnitude=999_000.0, t=0.0, kind=ShockKind.CASH_WITHDRAWAL)
        plan = InterventionPlan(
            interventions=[
                Intervention(
                    type=InterventionType.CREDIT_LINE_INCREASE,
                    merchant_id="A",
                    t=0.0,
                    amount=600_000.0,
                )
            ]
        )
        result = LiquiditySimulator(chain_graph, chain_config).run(shock, plan, run_id="s")
        assert "B" not in result.affected_ids

    def test_term_extension_moves_the_deadline(self, chain_graph, chain_config):
        plan = InterventionPlan(
            interventions=[
                Intervention(
                    type=InterventionType.SUPPLIER_TERM_EXTENSION,
                    merchant_id="A",
                    t=0.0,
                    amount=500_000.0,
                    shift_hours=48.0,
                    target_obligation_id="obl_ab",
                )
            ]
        )
        simulator = LiquiditySimulator(chain_graph, chain_config)
        simulator.run(None, plan, run_id="s")
        extended = next(
            o for o in simulator.obligation_book() if o.obligation_id == "obl_ab"
        )
        assert extended.due_t == pytest.approx(58.0)
        assert extended.original_due_t == pytest.approx(10.0)

    def test_restructure_preserves_principal_across_tranches(self, chain_graph, chain_config):
        plan = InterventionPlan(
            interventions=[
                Intervention(
                    type=InterventionType.REPAYMENT_RESTRUCTURE,
                    merchant_id="A",
                    t=0.0,
                    amount=500_000.0,
                    tranches=4,
                    target_obligation_id="obl_ab",
                )
            ]
        )
        simulator = LiquiditySimulator(chain_graph, chain_config)
        simulator.run(None, plan, run_id="s")
        book = simulator.obligation_book()

        parent = next(o for o in book if o.obligation_id == "obl_ab")
        children = [o for o in book if o.parent_obligation_id == "obl_ab"]
        assert parent.status is ObligationStatus.RESTRUCTURED
        assert len(children) == 4
        # Restructuring changes *when* cash is owed, never how much.
        assert sum(c.amount for c in children) == pytest.approx(500_000.0)


class TestCounterfactualEvaluator:
    def test_repeated_plans_are_cached(self, chain_graph, chain_config):
        shock = Shock.single("A", magnitude=999_000.0, t=0.0, kind=ShockKind.CASH_WITHDRAWAL)
        evaluator = CounterfactualEvaluator(
            graph=chain_graph, shock=shock, config=chain_config
        )
        action = Intervention(
            type=InterventionType.LIQUIDITY_INJECTION,
            merchant_id="A",
            t=0.0,
            amount=600_000.0,
        )
        first = evaluator.disruption([action])
        runs_after_first = evaluator.simulations_run
        second = evaluator.disruption([action])

        assert first == pytest.approx(second)
        assert evaluator.simulations_run == runs_after_first

    def test_marginal_gain_is_positive_for_a_helpful_action(
        self, chain_graph, chain_config
    ):
        shock = Shock.single("A", magnitude=999_000.0, t=0.0, kind=ShockKind.CASH_WITHDRAWAL)
        evaluator = CounterfactualEvaluator(
            graph=chain_graph, shock=shock, config=chain_config
        )
        action = Intervention(
            type=InterventionType.LIQUIDITY_INJECTION,
            merchant_id="A",
            t=0.0,
            amount=600_000.0,
        )
        assert evaluator.marginal_gain([], action) > 0


class TestNodeStatusTransitions:
    def test_status_reaches_defaulted_after_grace(self, chain_graph):
        shock = Shock.single("A", magnitude=999_000.0, t=0.0, kind=ShockKind.CASH_WITHDRAWAL)
        config = SimulationConfig(
            horizon_hours=200.0, seed=1, apply_payment_discipline=False
        )
        result = LiquiditySimulator(chain_graph, config).run(shock, run_id="s")
        assert result.outcomes["A"].final_status is NodeStatus.DEFAULTED
