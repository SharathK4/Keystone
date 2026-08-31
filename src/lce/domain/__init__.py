"""The canonical mathematical data model.

Every other layer - persistence, simulation, learning, optimisation, API -
speaks in terms of these objects. They are frozen Pydantic models: values, not
mutable records.

Object map
----------
``MerchantProfile`` / ``LiquidityState``  node parameters and state L_i(t), b_i(t)
``PaymentEvent``                          a realised cash movement (i -> j, a, t)
``Obligation``                            a commitment (i -> j, a_o, d_o)
``LagDistribution`` / ``DependencyEdge``  temporal edge law and conditional dependency
``Shock`` / ``ShockComponent``            the shock vector S(t)
``Intervention`` / ``InterventionPlan``   control variables U and their cost c(u)
``PropagationEvent`` / ``CascadeResult``  what the simulator observed
``ModelPrediction`` / ``NodeExposure``    what a predictor claims will happen
``EvaluationResult``                      how well the claim matched reality
``objectives``                            the disruption objective D(G, S)
"""

from __future__ import annotations

from lce.domain import objectives
from lce.domain.base import (
    AMOUNT_TOL,
    HOURS_PER_DAY,
    DomainModel,
    EventId,
    MerchantId,
    ObligationId,
    RunId,
    SimTime,
    new_id,
    to_sim_time,
    to_wall_clock,
    utcnow,
)
from lce.domain.edges import DependencyEdge, EdgeFeatures, LagDistribution
from lce.domain.enums import (
    InterventionType,
    MerchantSector,
    MerchantTier,
    NodeStatus,
    ObligationKind,
    ObligationStatus,
    OptimizerKind,
    PaymentChannel,
    PaymentStatus,
    PredictorKind,
    PropagationEventType,
    RecurrencePattern,
    RunKind,
    RunStatus,
    ShockKind,
)
from lce.domain.evaluation import (
    ClassificationMetrics,
    EvaluationResult,
    InterventionMetrics,
    TimingMetrics,
)
from lce.domain.events import EXTERNAL_SINK, Obligation, PaymentEvent
from lce.domain.intervention import Intervention, InterventionPlan
from lce.domain.merchant import LiquidityState, MerchantProfile, MerchantSnapshot
from lce.domain.objectives import (
    DisruptionBreakdown,
    compute_disruption,
    disruption_prevented,
    disruption_prevented_per_rupee,
    systemic_importance,
)
from lce.domain.prediction import ModelPrediction, NodeExposure
from lce.domain.propagation import CascadeResult, NodeOutcome, PropagationEvent
from lce.domain.shock import Shock, ShockComponent

__all__ = [
    "AMOUNT_TOL",
    "EXTERNAL_SINK",
    "HOURS_PER_DAY",
    "CascadeResult",
    "ClassificationMetrics",
    "DependencyEdge",
    "DisruptionBreakdown",
    "DomainModel",
    "EdgeFeatures",
    "EvaluationResult",
    "EventId",
    "Intervention",
    "InterventionMetrics",
    "InterventionPlan",
    "InterventionType",
    "LagDistribution",
    "LiquidityState",
    "MerchantId",
    "MerchantProfile",
    "MerchantSector",
    "MerchantSnapshot",
    "MerchantTier",
    "ModelPrediction",
    "NodeExposure",
    "NodeOutcome",
    "NodeStatus",
    "Obligation",
    "ObligationId",
    "ObligationKind",
    "ObligationStatus",
    "OptimizerKind",
    "PaymentChannel",
    "PaymentEvent",
    "PaymentStatus",
    "PredictorKind",
    "PropagationEvent",
    "PropagationEventType",
    "RecurrencePattern",
    "RunId",
    "RunKind",
    "RunStatus",
    "Shock",
    "ShockComponent",
    "ShockKind",
    "SimTime",
    "TimingMetrics",
    "compute_disruption",
    "disruption_prevented",
    "disruption_prevented_per_rupee",
    "new_id",
    "objectives",
    "systemic_importance",
    "to_sim_time",
    "to_wall_clock",
    "utcnow",
]
