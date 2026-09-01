"""Domain invariants: the mathematical core."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError as PydanticValidationError

from lce.config import ObjectiveSettings
from lce.domain import (
    CascadeResult,
    DependencyEdge,
    Intervention,
    InterventionPlan,
    InterventionType,
    LagDistribution,
    LiquidityState,
    MerchantProfile,
    NodeOutcome,
    Obligation,
    ObligationStatus,
    PaymentEvent,
    Shock,
    ShockKind,
    compute_disruption,
)
from lce.domain.base import to_sim_time, to_wall_clock
from lce.domain.objectives import phi


class TestMerchant:
    def test_buffer_is_cash_plus_credit_minus_floor(self):
        profile = MerchantProfile(
            opening_balance=1_000_000, operating_floor=100_000, credit_limit=200_000
        )
        assert profile.initial_buffer == pytest.approx(1_100_000)

    def test_floor_above_resources_is_rejected(self):
        # A node that starts already constrained is a config error, not a state.
        with pytest.raises(PydanticValidationError):
            MerchantProfile(opening_balance=1000, operating_floor=5000, credit_limit=0)

    def test_autonomy_is_infinite_when_cash_positive(self):
        profile = MerchantProfile(
            opening_balance=100_000, exogenous_inflow_rate=100.0, operating_burn_rate=50.0
        )
        assert profile.autonomy_hours() == math.inf

    def test_autonomy_is_buffer_over_net_burn(self):
        profile = MerchantProfile(
            opening_balance=100_000, exogenous_inflow_rate=0.0, operating_burn_rate=100.0
        )
        assert profile.autonomy_hours() == pytest.approx(1000.0)

    def test_liquidity_state_buffer_and_deficit(self):
        state = LiquidityState(
            merchant_id="m",
            t=5.0,
            cash_balance=50_000,
            credit_limit=30_000,
            credit_drawn=10_000,
            operating_floor=80_000,
        )
        assert state.available_credit == pytest.approx(20_000)
        assert state.buffer == pytest.approx(50_000 + 20_000 - 80_000)
        assert state.deficit == pytest.approx(30_000)
        assert not state.can_pay(1.0)


class TestObligation:
    def test_partial_then_full_settlement_transitions(self):
        obligation = Obligation(
            debtor_id="a", creditor_id="b", amount=1000.0, issued_t=0.0, due_t=100.0
        )
        assert obligation.status is ObligationStatus.PENDING

        partial = obligation.with_payment(400.0, t=90.0, grace_hours=48.0)
        assert partial.status is ObligationStatus.PARTIALLY_SETTLED
        assert partial.outstanding == pytest.approx(600.0)
        assert partial.settled_t is None

        full = partial.with_payment(600.0, t=120.0, grace_hours=48.0)
        assert full.status is ObligationStatus.SETTLED_LATE
        assert full.outstanding == pytest.approx(0.0)
        assert full.delay() == pytest.approx(20.0)

    def test_on_time_settlement_has_no_delay(self):
        obligation = Obligation(
            debtor_id="a", creditor_id="b", amount=500.0, issued_t=0.0, due_t=100.0
        )
        settled = obligation.with_payment(500.0, t=80.0, grace_hours=48.0)
        assert settled.status is ObligationStatus.SETTLED
        assert settled.delay() == 0.0

    def test_default_requires_passing_the_grace_period(self):
        obligation = Obligation(
            debtor_id="a", creditor_id="b", amount=500.0, issued_t=0.0, due_t=100.0
        )
        assert obligation.is_overdue_at(120.0)
        assert not obligation.is_defaulted_at(120.0, grace_hours=48.0)
        assert obligation.is_defaulted_at(160.0, grace_hours=48.0)
        assert obligation.touched(160.0, 48.0).status is ObligationStatus.DEFAULTED

    def test_deadline_change_preserves_the_original(self):
        obligation = Obligation(
            debtor_id="a", creditor_id="b", amount=500.0, issued_t=0.0, due_t=100.0
        )
        extended = obligation.with_deadline(150.0)
        assert extended.due_t == 150.0
        assert extended.original_due_t == 100.0
        # A second shift must not overwrite the true original deadline.
        assert extended.with_deadline(200.0).original_due_t == 100.0

    def test_overpayment_is_rejected(self):
        with pytest.raises(PydanticValidationError):
            Obligation(
                debtor_id="a",
                creditor_id="b",
                amount=100.0,
                amount_paid=500.0,
                issued_t=0.0,
                due_t=10.0,
            )

    def test_self_dealing_is_rejected(self):
        with pytest.raises(PydanticValidationError):
            Obligation(debtor_id="a", creditor_id="a", amount=1.0, issued_t=0, due_t=1)
        with pytest.raises(PydanticValidationError):
            PaymentEvent(payer_id="a", payee_id="a", amount=1.0, t=0.0)


class TestLagDistribution:
    def test_mean_cv_round_trip(self):
        lag = LagDistribution.from_mean_cv(48.0, cv=0.6)
        assert lag.mean_hours == pytest.approx(48.0, rel=1e-6)

    def test_cdf_is_monotone_and_bounded(self):
        lag = LagDistribution.from_mean_cv(24.0, cv=0.5)
        values = [lag.cdf(t) for t in (0.0, 6.0, 12.0, 24.0, 48.0, 240.0)]
        assert values == sorted(values)
        assert values[0] == 0.0
        assert values[-1] <= 1.0

    def test_quantiles_are_ordered(self):
        lag = LagDistribution.from_mean_cv(36.0, cv=0.8)
        assert lag.quantile(0.1) < lag.quantile(0.5) < lag.quantile(0.9)

    def test_fit_from_samples_recovers_scale(self):
        import numpy as np

        rng = np.random.default_rng(0)
        truth = LagDistribution.from_mean_cv(30.0, cv=0.5)
        samples = np.asarray(truth.sample(rng, 5000))
        fitted = LagDistribution.from_samples(samples)
        assert fitted.mean_hours == pytest.approx(truth.mean_hours, rel=0.1)

    def test_sampling_uses_the_supplied_generator(self):
        import numpy as np

        lag = LagDistribution.from_mean_cv(12.0, cv=0.4)
        first = lag.sample(np.random.default_rng(5), 10)
        second = lag.sample(np.random.default_rng(5), 10)
        assert np.allclose(first, second)


class TestShock:
    def test_impulse_mass_lands_in_exactly_one_tick(self):
        shock = Shock.single("m", magnitude=1000.0, t=5.0)
        assert shock.magnitude_in("m", 5.0, 6.0) == pytest.approx(1000.0)
        assert shock.magnitude_in("m", 6.0, 7.0) == 0.0

    def test_windowed_shock_spreads_its_mass(self):
        from lce.domain.shock import ShockComponent

        shock = Shock(
            components=[
                ShockComponent(
                    merchant_id="m",
                    magnitude=1000.0,
                    t=0.0,
                    kind=ShockKind.DEMAND_COLLAPSE,
                    duration_hours=10.0,
                )
            ]
        )
        assert shock.magnitude_in("m", 0.0, 1.0) == pytest.approx(100.0)
        total = sum(shock.magnitude_in("m", t, t + 1.0) for t in range(10))
        assert total == pytest.approx(1000.0)

    def test_duplicate_components_are_rejected(self):
        from lce.domain.shock import ShockComponent

        with pytest.raises(PydanticValidationError):
            Shock(
                components=[
                    ShockComponent(merchant_id="m", magnitude=1.0, t=0.0),
                    ShockComponent(merchant_id="m", magnitude=2.0, t=0.0),
                ]
            )


class TestIntervention:
    def test_injection_cost_is_the_capital_deployed(self):
        action = Intervention(
            type=InterventionType.LIQUIDITY_INJECTION, merchant_id="m", t=0.0, amount=50_000
        )
        assert action.cost == pytest.approx(50_000)
        assert action.is_capital_deploying

    def test_acceleration_cost_scales_with_days_pulled_forward(self):
        action = Intervention(
            type=InterventionType.RECEIVABLE_ACCELERATION,
            merchant_id="m",
            t=0.0,
            amount=100_000,
            shift_hours=48.0,
            target_obligation_id="obl_1",
            discount_rate_per_day=0.001,
        )
        assert action.cost == pytest.approx(100_000 * 0.001 * 2.0)
        assert not action.is_capital_deploying

    @pytest.mark.parametrize(
        "kind",
        [
            InterventionType.RECEIVABLE_ACCELERATION,
            InterventionType.SUPPLIER_TERM_EXTENSION,
            InterventionType.REPAYMENT_RESTRUCTURE,
        ],
    )
    def test_obligation_types_require_a_target(self, kind):
        with pytest.raises(PydanticValidationError):
            Intervention(merchant_id="m", type=kind, t=0.0, amount=1.0, shift_hours=1.0)

    def test_plan_feasibility_and_dpr(self):
        action = Intervention(
            type=InterventionType.LIQUIDITY_INJECTION, merchant_id="m", t=0.0, amount=1000.0
        )
        plan = InterventionPlan(interventions=[action], budget=500.0, max_actions=3)
        assert not plan.is_feasible()  # over budget

        plan = InterventionPlan(interventions=[action], budget=5000.0, max_actions=3)
        assert plan.is_feasible()
        assert plan.disruption_prevented is None

        evaluated = plan.with_evaluation(baseline=10_000.0, residual=4_000.0, optimizer="greedy")
        assert evaluated.disruption_prevented == pytest.approx(6_000.0)
        assert evaluated.disruption_prevented_per_rupee == pytest.approx(6.0)

    def test_free_effective_plan_reports_infinite_dpr(self):
        plan = InterventionPlan(interventions=[]).with_evaluation(100.0, 40.0, "none")
        assert plan.total_cost == 0.0
        assert plan.disruption_prevented_per_rupee == math.inf


class TestObjective:
    def test_disruption_sums_weighted_terms(self):
        settings = ObjectiveSettings(
            gamma_delay=1.0, gamma_default=1000.0, gamma_deficit=0.5, delay_unit_hours=24.0
        )
        result = CascadeResult(
            horizon_hours=168.0,
            outcomes={
                "a": NodeOutcome(
                    merchant_id="a",
                    systemic_weight=2.0,
                    weighted_delay=10.0,
                    defaults_caused=1,
                    deficit_integral=100.0,
                ),
            },
        )
        breakdown = compute_disruption(result, settings)
        assert breakdown.delay_term == pytest.approx(2.0 * 10.0)
        assert breakdown.default_term == pytest.approx(2.0 * 1000.0)
        assert breakdown.deficit_term == pytest.approx(2.0 * 0.5 * 100.0)
        assert breakdown.total == pytest.approx(20.0 + 2000.0 + 100.0)

    def test_phi_is_days_late_and_never_negative(self):
        assert phi(48.0, 24.0) == pytest.approx(2.0)
        assert phi(-10.0, 24.0) == 0.0

    def test_affected_set_excludes_merely_late_payers(self):
        # Settling late by choice is not contagion; being unable to pay is.
        late_only = NodeOutcome(merchant_id="a", obligations_settled_late=3)
        constrained = NodeOutcome(merchant_id="b", became_constrained=True)
        assert not late_only.is_affected
        assert constrained.is_affected

    def test_downstream_affected_excludes_directly_shocked(self):
        shocked = NodeOutcome(merchant_id="a", was_shocked=True, became_constrained=True)
        downstream = NodeOutcome(merchant_id="b", became_constrained=True)
        assert not shocked.is_downstream_affected
        assert downstream.is_downstream_affected


class TestTimeConversion:
    def test_sim_time_round_trip(self):
        from datetime import UTC, datetime

        epoch = datetime(2025, 1, 1, tzinfo=UTC)
        moment = datetime(2025, 1, 3, 12, tzinfo=UTC)
        t = to_sim_time(moment, epoch)
        assert t == pytest.approx(60.0)
        assert to_wall_clock(t, epoch) == moment


class TestDependencyEdge:
    def test_self_loops_are_rejected(self):
        with pytest.raises(PydanticValidationError):
            DependencyEdge(source_id="a", target_id="a")

    def test_hit_probability_grows_with_the_horizon(self):
        edge = DependencyEdge(
            source_id="a", target_id="b", lag=LagDistribution.from_mean_cv(24.0, 0.5)
        )
        assert edge.hit_probability_by(0.0, 0.0) == 0.0
        assert edge.hit_probability_by(0.0, 12.0) < edge.hit_probability_by(0.0, 72.0)

    def test_excitation_kernel_decays(self):
        edge = DependencyEdge(
            source_id="a", target_id="b", excitation_alpha=1.0, excitation_decay=0.1
        )
        assert edge.excitation_kernel(-1.0) == 0.0
        assert edge.excitation_kernel(0.0) > edge.excitation_kernel(10.0) > 0.0
