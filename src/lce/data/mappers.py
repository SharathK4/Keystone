"""Translation between frozen domain objects and ORM rows.

Kept in one module so the money-unit boundary (rupees <-> paise) is enforced in
exactly one place. Repositories never touch ``*_minor`` fields directly.
"""

from __future__ import annotations

from typing import Any

from lce.domain.edges import DependencyEdge, EdgeFeatures, LagDistribution
from lce.domain.enums import (
    MerchantSector,
    MerchantTier,
    NodeStatus,
    ObligationKind,
    ObligationStatus,
    PaymentChannel,
    PaymentStatus,
    PredictorKind,
    PropagationEventType,
)
from lce.domain.events import Obligation, PaymentEvent
from lce.domain.intervention import Intervention, InterventionPlan
from lce.domain.merchant import MerchantProfile
from lce.domain.prediction import ModelPrediction, NodeExposure
from lce.domain.propagation import NodeOutcome, PropagationEvent
from lce.domain.shock import Shock, ShockComponent
from lce.data.orm import (
    DependencyEdgeRow,
    InterventionPlanRow,
    MerchantRow,
    NodeExposureRow,
    NodeOutcomeRow,
    ObligationRow,
    PaymentEventRow,
    PredictionRow,
    PropagationEventRow,
    ShockRow,
    from_minor,
    to_minor,
)

# --------------------------------------------------------------------- merchant


def merchant_to_row(profile: MerchantProfile, dataset_id: str) -> MerchantRow:
    return MerchantRow(
        merchant_id=profile.merchant_id,
        dataset_id=dataset_id,
        external_id=profile.external_id,
        name=profile.name,
        sector=str(profile.sector),
        tier=str(profile.tier),
        opening_balance_minor=to_minor(profile.opening_balance),
        operating_floor_minor=to_minor(profile.operating_floor),
        credit_limit_minor=to_minor(profile.credit_limit),
        exogenous_inflow_rate=profile.exogenous_inflow_rate,
        operating_burn_rate=profile.operating_burn_rate,
        payment_discipline=profile.payment_discipline,
        stress_threshold_ratio=profile.stress_threshold_ratio,
        systemic_weight=profile.systemic_weight,
        meta=dict(profile.metadata),
    )


def merchant_from_row(row: MerchantRow) -> MerchantProfile:
    return MerchantProfile(
        merchant_id=row.merchant_id,
        external_id=row.external_id,
        name=row.name,
        sector=MerchantSector(row.sector),
        tier=MerchantTier(row.tier),
        opening_balance=from_minor(row.opening_balance_minor),
        operating_floor=from_minor(row.operating_floor_minor),
        credit_limit=from_minor(row.credit_limit_minor),
        exogenous_inflow_rate=row.exogenous_inflow_rate,
        operating_burn_rate=row.operating_burn_rate,
        payment_discipline=row.payment_discipline,
        stress_threshold_ratio=row.stress_threshold_ratio,
        systemic_weight=row.systemic_weight,
        metadata=dict(row.meta or {}),
    )


# ---------------------------------------------------------------- payment event


def payment_to_row(event: PaymentEvent, dataset_id: str) -> PaymentEventRow:
    return PaymentEventRow(
        event_id=event.event_id,
        dataset_id=dataset_id,
        payer_id=event.payer_id,
        payee_id=event.payee_id,
        amount_minor=to_minor(event.amount),
        t=event.t,
        obligation_id=event.obligation_id,
        channel=str(event.channel),
        status=str(event.status),
        settlement_lag_hours=event.settlement_lag_hours,
        external_id=event.external_id,
        is_synthetic=event.is_synthetic,
        meta=dict(event.metadata),
    )


def payment_from_row(row: PaymentEventRow) -> PaymentEvent:
    return PaymentEvent(
        event_id=row.event_id,
        payer_id=row.payer_id,
        payee_id=row.payee_id,
        amount=from_minor(row.amount_minor),
        t=row.t,
        obligation_id=row.obligation_id,
        channel=PaymentChannel(row.channel),
        status=PaymentStatus(row.status),
        settlement_lag_hours=row.settlement_lag_hours,
        external_id=row.external_id,
        is_synthetic=row.is_synthetic,
        metadata=dict(row.meta or {}),
    )


# ------------------------------------------------------------------ obligation


def obligation_to_row(obligation: Obligation, dataset_id: str) -> ObligationRow:
    return ObligationRow(
        obligation_id=obligation.obligation_id,
        dataset_id=dataset_id,
        debtor_id=obligation.debtor_id,
        creditor_id=obligation.creditor_id,
        amount_minor=to_minor(obligation.amount),
        amount_paid_minor=to_minor(obligation.amount_paid),
        issued_t=obligation.issued_t,
        due_t=obligation.due_t,
        original_due_t=obligation.original_due_t,
        settled_t=obligation.settled_t,
        kind=str(obligation.kind),
        status=str(obligation.status),
        priority=obligation.priority,
        parent_obligation_id=obligation.parent_obligation_id,
        external_id=obligation.external_id,
        meta=dict(obligation.metadata),
    )


def obligation_from_row(row: ObligationRow) -> Obligation:
    return Obligation(
        obligation_id=row.obligation_id,
        debtor_id=row.debtor_id,
        creditor_id=row.creditor_id,
        amount=from_minor(row.amount_minor),
        amount_paid=from_minor(row.amount_paid_minor),
        issued_t=row.issued_t,
        due_t=row.due_t,
        original_due_t=row.original_due_t,
        settled_t=row.settled_t,
        kind=ObligationKind(row.kind),
        status=ObligationStatus(row.status),
        priority=row.priority,
        parent_obligation_id=row.parent_obligation_id,
        external_id=row.external_id,
        metadata=dict(row.meta or {}),
    )


# ------------------------------------------------------------- dependency edge


def edge_to_row(edge: DependencyEdge, dataset_id: str, model_version: str = "v0") -> DependencyEdgeRow:
    return DependencyEdgeRow(
        dataset_id=dataset_id,
        source_id=edge.source_id,
        target_id=edge.target_id,
        estimator=edge.estimator or ("generator" if edge.is_ground_truth else "unknown"),
        model_version=model_version,
        is_ground_truth=edge.is_ground_truth,
        reliability=edge.reliability,
        pass_through=edge.pass_through,
        conditional_probability=edge.conditional_probability,
        excitation_alpha=edge.excitation_alpha,
        excitation_decay=edge.excitation_decay,
        base_intensity=edge.base_intensity,
        confidence=edge.confidence,
        lag_mu_log=edge.lag.mu_log,
        lag_sigma_log=edge.lag.sigma_log,
        lag_floor_hours=edge.lag.floor_hours,
        lag_max_hours=edge.lag.max_hours,
        features=edge.features.model_dump(mode="json"),
        meta=dict(edge.metadata),
    )


def edge_from_row(row: DependencyEdgeRow) -> DependencyEdge:
    return DependencyEdge(
        source_id=row.source_id,
        target_id=row.target_id,
        features=EdgeFeatures.model_validate(row.features or {}),
        lag=LagDistribution(
            mu_log=row.lag_mu_log,
            sigma_log=row.lag_sigma_log,
            floor_hours=row.lag_floor_hours,
            max_hours=row.lag_max_hours,
        ),
        reliability=row.reliability,
        pass_through=row.pass_through,
        conditional_probability=row.conditional_probability,
        excitation_alpha=row.excitation_alpha,
        excitation_decay=row.excitation_decay,
        base_intensity=row.base_intensity,
        is_ground_truth=row.is_ground_truth,
        estimator=row.estimator,
        confidence=row.confidence,
        metadata=dict(row.meta or {}),
    )


# ----------------------------------------------------------------------- shock


def shock_to_row(shock: Shock, dataset_id: str) -> ShockRow:
    return ShockRow(
        shock_id=shock.shock_id,
        dataset_id=dataset_id,
        name=shock.name,
        description=shock.description,
        total_magnitude_minor=to_minor(shock.total_magnitude),
        onset_t=shock.onset_t,
        components={"items": [c.model_dump(mode="json") for c in shock.components]},
        meta=dict(shock.metadata),
    )


def shock_from_row(row: ShockRow) -> Shock:
    items = (row.components or {}).get("items", [])
    return Shock(
        shock_id=row.shock_id,
        name=row.name,
        description=row.description,
        components=[ShockComponent.model_validate(c) for c in items],
        metadata=dict(row.meta or {}),
    )


# ----------------------------------------------------------- intervention plan


def plan_to_row(
    plan: InterventionPlan, dataset_id: str, shock_id: str | None = None,
    run_id: str | None = None,
) -> InterventionPlanRow:
    return InterventionPlanRow(
        plan_id=plan.plan_id,
        dataset_id=dataset_id,
        shock_id=shock_id,
        run_id=run_id,
        optimizer=plan.optimizer,
        budget_minor=to_minor(plan.budget) if plan.budget is not None else None,
        max_actions=plan.max_actions,
        total_cost_minor=to_minor(plan.total_cost),
        baseline_disruption=plan.baseline_disruption,
        residual_disruption=plan.residual_disruption,
        interventions={"items": [u.model_dump(mode="json") for u in plan.interventions]},
        meta=dict(plan.metadata),
    )


def plan_from_row(row: InterventionPlanRow) -> InterventionPlan:
    items = (row.interventions or {}).get("items", [])
    return InterventionPlan(
        plan_id=row.plan_id,
        interventions=[Intervention.model_validate(u) for u in items],
        budget=from_minor(row.budget_minor) if row.budget_minor is not None else None,
        max_actions=row.max_actions,
        baseline_disruption=row.baseline_disruption,
        residual_disruption=row.residual_disruption,
        optimizer=row.optimizer,
        metadata=dict(row.meta or {}),
    )


# ------------------------------------------------------------ cascade results


def propagation_to_row(event: PropagationEvent, run_id: str) -> PropagationEventRow:
    return PropagationEventRow(
        event_id=event.event_id,
        run_id=run_id,
        sequence=event.sequence,
        t=event.t,
        type=str(event.type),
        merchant_id=event.merchant_id,
        counterparty_id=event.counterparty_id,
        obligation_id=event.obligation_id,
        amount_minor=to_minor(event.amount),
        caused_by=event.caused_by,
        hop=event.hop,
        balance_after_minor=(
            to_minor(event.balance_after) if event.balance_after is not None else None
        ),
        buffer_after_minor=(
            to_minor(event.buffer_after) if event.buffer_after is not None else None
        ),
        status_after=str(event.status_after) if event.status_after else None,
        detail=_jsonable(event.detail),
    )


def propagation_from_row(row: PropagationEventRow) -> PropagationEvent:
    return PropagationEvent(
        event_id=row.event_id,
        sequence=row.sequence,
        t=row.t,
        type=PropagationEventType(row.type),
        merchant_id=row.merchant_id,
        counterparty_id=row.counterparty_id,
        obligation_id=row.obligation_id,
        amount=from_minor(row.amount_minor),
        caused_by=row.caused_by,
        hop=row.hop,
        balance_after=(
            from_minor(row.balance_after_minor) if row.balance_after_minor is not None else None
        ),
        buffer_after=(
            from_minor(row.buffer_after_minor) if row.buffer_after_minor is not None else None
        ),
        status_after=NodeStatus(row.status_after) if row.status_after else None,
        detail=dict(row.detail or {}),
    )


def outcome_to_row(outcome: NodeOutcome, run_id: str) -> NodeOutcomeRow:
    return NodeOutcomeRow(
        run_id=run_id,
        merchant_id=outcome.merchant_id,
        systemic_weight=outcome.systemic_weight,
        final_status=str(outcome.final_status),
        was_shocked=outcome.was_shocked,
        became_constrained=outcome.became_constrained,
        became_defaulted=outcome.became_defaulted,
        is_affected=outcome.is_affected,
        first_constrained_t=outcome.first_constrained_t,
        first_defaulted_t=outcome.first_defaulted_t,
        hop_distance=outcome.hop_distance,
        value_delayed_minor=to_minor(outcome.value_delayed),
        weighted_delay=outcome.weighted_delay,
        defaults_caused=outcome.defaults_caused,
        deficit_integral=outcome.deficit_integral,
        min_buffer_minor=to_minor(outcome.min_buffer),
        final_balance_minor=to_minor(outcome.final_balance),
        obligations_missed=outcome.obligations_missed,
        obligations_settled_late=outcome.obligations_settled_late,
    )


def outcome_from_row(row: NodeOutcomeRow) -> NodeOutcome:
    return NodeOutcome(
        merchant_id=row.merchant_id,
        systemic_weight=row.systemic_weight,
        final_status=NodeStatus(row.final_status),
        was_shocked=row.was_shocked,
        became_constrained=row.became_constrained,
        became_defaulted=row.became_defaulted,
        first_constrained_t=row.first_constrained_t,
        first_defaulted_t=row.first_defaulted_t,
        hop_distance=row.hop_distance,
        value_delayed=from_minor(row.value_delayed_minor),
        weighted_delay=row.weighted_delay,
        defaults_caused=row.defaults_caused,
        deficit_integral=row.deficit_integral,
        min_buffer=from_minor(row.min_buffer_minor),
        final_balance=from_minor(row.final_balance_minor),
        obligations_missed=row.obligations_missed,
        obligations_settled_late=row.obligations_settled_late,
    )


# ------------------------------------------------------------------ prediction


def prediction_to_rows(
    prediction: ModelPrediction, dataset_version: str | None = None
) -> tuple[PredictionRow, list[NodeExposureRow]]:
    head = PredictionRow(
        prediction_id=prediction.prediction_id,
        run_id=prediction.run_id,
        dataset_version=dataset_version,
        shock_id=prediction.shock_id,
        predictor=str(prediction.predictor),
        model_version=prediction.model_version,
        horizon_hours=prediction.horizon_hours,
        threshold=prediction.threshold,
        inference_ms=prediction.inference_ms,
        seed=prediction.seed,
        config_hash=prediction.config_hash,
        meta=_jsonable(prediction.metadata),
    )
    rows = [
        NodeExposureRow(
            prediction_id=prediction.prediction_id,
            merchant_id=exposure.merchant_id,
            exposure_score=exposure.exposure_score,
            expected_shortfall_minor=to_minor(exposure.expected_shortfall),
            expected_hit_t=exposure.expected_hit_t,
            hit_t_lower=exposure.hit_t_lower,
            hit_t_upper=exposure.hit_t_upper,
            hop_distance=exposure.hop_distance,
            contributing_sources=list(exposure.contributing_sources),
        )
        for exposure in prediction.exposures.values()
    ]
    return head, rows


def prediction_from_rows(
    head: PredictionRow, exposures: list[NodeExposureRow]
) -> ModelPrediction:
    return ModelPrediction(
        prediction_id=head.prediction_id,
        run_id=head.run_id,
        shock_id=head.shock_id,
        predictor=PredictorKind(head.predictor),
        model_version=head.model_version,
        horizon_hours=head.horizon_hours,
        threshold=head.threshold,
        inference_ms=head.inference_ms,
        seed=head.seed,
        config_hash=head.config_hash,
        exposures={
            row.merchant_id: NodeExposure(
                merchant_id=row.merchant_id,
                exposure_score=row.exposure_score,
                expected_shortfall=from_minor(row.expected_shortfall_minor),
                expected_hit_t=row.expected_hit_t,
                hit_t_lower=row.hit_t_lower,
                hit_t_upper=row.hit_t_upper,
                hop_distance=row.hop_distance,
                contributing_sources=list(row.contributing_sources or []),
            )
            for row in exposures
        },
        metadata=dict(head.meta or {}),
    )


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce enum/None values in free-form detail dicts into JSON scalars."""
    return {k: (str(v) if hasattr(v, "value") else v) for k, v in (payload or {}).items()}
