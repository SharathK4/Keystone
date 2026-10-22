"""SQLAlchemy ORM schema.

Design notes
------------
* **Event-level fidelity is preserved in the database too.** ``payment_events``
  is the fact table; there is no pre-aggregated edge table that would let the
  event stream be discarded. ``dependency_edges`` is a *derived* overlay and
  carries the ``estimator``/``model_version`` that produced it, so estimates
  from different learners coexist rather than overwrite each other.
* **Amounts are stored in paise** (``BigInteger`` minor units) rather than
  floats. Money that round-trips through binary floating point accumulates
  error, and this system sums millions of amounts when computing the objective.
  The domain layer works in rupees as floats; conversion happens at this
  boundary only, via :func:`to_minor` / :func:`from_minor`.
* **Portability.** JSON columns use the PostgreSQL ``JSONB`` variant where
  available and fall back to generic ``JSON`` on SQLite, so the same schema
  serves production Postgres and the in-memory database used by the tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB on PostgreSQL, plain JSON everywhere else.
JsonType = JSON().with_variant(JSONB(), "postgresql")

MINOR_UNITS = 100  # paise per rupee


def to_minor(amount: float) -> int:
    """Rupees (float) -> paise (int), half-up."""
    return round(amount * MINOR_UNITS)


def from_minor(amount: int | None) -> float:
    """Paise (int) -> rupees (float)."""
    return 0.0 if amount is None else amount / MINOR_UNITS


class Base(DeclarativeBase):
    """Declarative base with a shared type map."""

    type_annotation_map = {dict[str, Any]: JsonType}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------- datasets


class DatasetRow(Base, TimestampMixin):
    """A generated or ingested network snapshot.

    ``dataset_version`` is content-addressed from the generator config, so the
    same config and seed always produce the same id - that is what makes a run
    reproducible from its manifest alone.
    """

    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_version: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="synthetic")
    epoch: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")

    merchants: Mapped[list[MerchantRow]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )


# -------------------------------------------------------------------- merchants


class MerchantRow(Base, TimestampMixin):
    """Node parameters. State lives in ``liquidity_states``."""

    __tablename__ = "merchants"
    __table_args__ = (
        UniqueConstraint("dataset_id", "merchant_id", name="uq_merchant_per_dataset"),
        Index("ix_merchants_dataset", "dataset_id"),
        Index("ix_merchants_external", "external_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(255), default="")
    sector: Mapped[str] = mapped_column(String(32), nullable=False)
    tier: Mapped[str] = mapped_column(String(32), nullable=False)

    opening_balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operating_floor_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    credit_limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    exogenous_inflow_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    operating_burn_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    payment_discipline: Mapped[float] = mapped_column(Float, nullable=False, default=0.9)
    stress_threshold_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.25)
    systemic_weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    meta: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)

    dataset: Mapped[DatasetRow] = relationship(back_populates="merchants")


class LiquidityStateRow(Base):
    """Time-stamped snapshot of L_i(t), b_i(t) and the node's status."""

    __tablename__ = "liquidity_states"
    __table_args__ = (
        Index("ix_liquidity_states_lookup", "dataset_id", "merchant_id", "t"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64))
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    t: Mapped[float] = mapped_column(Float, nullable=False)

    cash_balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credit_drawn_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    credit_limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    operating_floor_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    pending_payable_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    pending_receivable_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    overdue_payable_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="healthy")


# ------------------------------------------------------------- event fact table


class PaymentEventRow(Base):
    """The fact table. One row per realised cash movement - never aggregated."""

    __tablename__ = "payment_events"
    __table_args__ = (
        UniqueConstraint("dataset_id", "event_id", name="uq_payment_event"),
        Index("ix_payment_events_payer", "dataset_id", "payer_id", "t"),
        Index("ix_payment_events_payee", "dataset_id", "payee_id", "t"),
        Index("ix_payment_events_pair", "dataset_id", "payer_id", "payee_id", "t"),
        Index("ix_payment_events_external", "external_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(64), nullable=False)

    payer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payee_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    t: Mapped[float] = mapped_column(Float, nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    obligation_id: Mapped[str | None] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="neft")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="captured")
    settlement_lag_hours: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    external_id: Mapped[str | None] = mapped_column(String(128))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


class ObligationRow(Base, TimestampMixin):
    """Commitments - what contagion actually propagates along."""

    __tablename__ = "obligations"
    __table_args__ = (
        UniqueConstraint("dataset_id", "obligation_id", name="uq_obligation"),
        Index("ix_obligations_debtor", "dataset_id", "debtor_id", "due_t"),
        Index("ix_obligations_creditor", "dataset_id", "creditor_id", "due_t"),
        Index("ix_obligations_status", "dataset_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    obligation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(64), nullable=False)

    debtor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    creditor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_paid_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    issued_t: Mapped[float] = mapped_column(Float, nullable=False)
    due_t: Mapped[float] = mapped_column(Float, nullable=False)
    original_due_t: Mapped[float | None] = mapped_column(Float)
    settled_t: Mapped[float | None] = mapped_column(Float)

    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="trade_payable")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_obligation_id: Mapped[str | None] = mapped_column(String(64))

    external_id: Mapped[str | None] = mapped_column(String(128))
    meta: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


# ---------------------------------------------------------- dependency overlay


class DependencyEdgeRow(Base, TimestampMixin):
    """Learned (or ground-truth) behavioural edge.

    Keyed by ``(dataset, source, target, estimator, model_version)`` so several
    estimators' views of the same link coexist and can be compared against the
    generator's ground truth rather than clobbering it.
    """

    __tablename__ = "dependency_edges"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "source_id",
            "target_id",
            "estimator",
            "model_version",
            name="uq_dependency_edge",
        ),
        Index("ix_dependency_source", "dataset_id", "source_id"),
        Index("ix_dependency_target", "dataset_id", "target_id"),
        Index("ix_dependency_truth", "dataset_id", "is_ground_truth"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)

    estimator: Mapped[str] = mapped_column(String(64), nullable=False, default="generator")
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v0")
    is_ground_truth: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    reliability: Mapped[float] = mapped_column(Float, nullable=False, default=0.9)
    pass_through: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    conditional_probability: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    excitation_alpha: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    excitation_decay: Mapped[float] = mapped_column(Float, nullable=False, default=0.04)
    base_intensity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    lag_mu_log: Mapped[float] = mapped_column(Float, nullable=False, default=3.0)
    lag_sigma_log: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    lag_floor_hours: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    lag_max_hours: Mapped[float] = mapped_column(Float, nullable=False, default=2160.0)

    features: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    meta: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


# -------------------------------------------------------- shocks & interventions


class ShockRow(Base, TimestampMixin):
    """A named shock scenario. Components are stored inline as JSON."""

    __tablename__ = "shocks"
    __table_args__ = (Index("ix_shocks_dataset", "dataset_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shock_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    dataset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    total_magnitude_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    onset_t: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    components: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    meta: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


class InterventionPlanRow(Base, TimestampMixin):
    """A candidate set of interventions and its measured effect."""

    __tablename__ = "intervention_plans"
    __table_args__ = (Index("ix_plans_shock", "shock_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    dataset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    shock_id: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64))

    optimizer: Mapped[str | None] = mapped_column(String(64))
    budget_minor: Mapped[int | None] = mapped_column(BigInteger)
    max_actions: Mapped[int | None] = mapped_column(Integer)
    total_cost_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    baseline_disruption: Mapped[float | None] = mapped_column(Float)
    residual_disruption: Mapped[float | None] = mapped_column(Float)
    interventions: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    meta: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


# -------------------------------------------------------------- runs & results


class RunRow(Base, TimestampMixin):
    """The reproducibility record.

    Every simulation, training, prediction, optimisation and evaluation writes
    one of these. Between ``dataset_version``, ``seed``, ``config`` and
    ``config_hash`` it is enough to replay the run exactly.
    """

    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_kind_status", "kind", "status"),
        Index("ix_runs_dataset", "dataset_version"),
        Index("ix_runs_experiment", "experiment_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    experiment_id: Mapped[str | None] = mapped_column(String(64))
    parent_run_id: Mapped[str | None] = mapped_column(String(64))

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    name: Mapped[str] = mapped_column(String(255), default="")

    dataset_version: Mapped[str | None] = mapped_column(String(64))
    model_version: Mapped[str | None] = mapped_column(String(64))
    shock_id: Mapped[str | None] = mapped_column(String(64))
    plan_id: Mapped[str | None] = mapped_column(String(64))

    seed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    config_hash: Mapped[str | None] = mapped_column(String(64))
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    seeds: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    git_sha: Mapped[str | None] = mapped_column(String(64))
    code_version: Mapped[str | None] = mapped_column(String(32))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)


class PropagationEventRow(Base):
    """Causally-linked cascade events, for audit and replay."""

    __tablename__ = "propagation_events"
    __table_args__ = (
        Index("ix_propagation_run", "run_id", "sequence"),
        Index("ix_propagation_merchant", "run_id", "merchant_id", "t"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    t: Mapped[float] = mapped_column(Float, nullable=False)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_id: Mapped[str | None] = mapped_column(String(64))
    obligation_id: Mapped[str | None] = mapped_column(String(64))
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    caused_by: Mapped[str | None] = mapped_column(String(64))
    hop: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    balance_after_minor: Mapped[int | None] = mapped_column(BigInteger)
    buffer_after_minor: Mapped[int | None] = mapped_column(BigInteger)
    status_after: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


class NodeOutcomeRow(Base):
    """Per-merchant cascade outcome - the ground-truth label set."""

    __tablename__ = "node_outcomes"
    __table_args__ = (
        UniqueConstraint("run_id", "merchant_id", name="uq_node_outcome"),
        Index("ix_node_outcomes_run", "run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    systemic_weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    final_status: Mapped[str] = mapped_column(String(32), nullable=False, default="healthy")
    was_shocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    became_constrained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    became_defaulted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_affected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    first_constrained_t: Mapped[float | None] = mapped_column(Float)
    first_defaulted_t: Mapped[float | None] = mapped_column(Float)
    hop_distance: Mapped[int | None] = mapped_column(Integer)

    value_delayed_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    weighted_delay: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    defaults_caused: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deficit_integral: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    min_buffer_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    final_balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    obligations_missed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    obligations_settled_late: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# ---------------------------------------------------------- model predictions


class PredictionRow(Base, TimestampMixin):
    """A predictor's answer for one (network, shock) pair."""

    __tablename__ = "predictions"
    __table_args__ = (Index("ix_predictions_shock", "shock_id", "predictor"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prediction_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    run_id: Mapped[str | None] = mapped_column(String(64))
    dataset_version: Mapped[str | None] = mapped_column(String(64))
    shock_id: Mapped[str | None] = mapped_column(String(64))

    predictor: Mapped[str] = mapped_column(String(48), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v0")
    horizon_hours: Mapped[float] = mapped_column(Float, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    inference_ms: Mapped[float | None] = mapped_column(Float)
    seed: Mapped[int | None] = mapped_column(BigInteger)
    config_hash: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


class NodeExposureRow(Base):
    """Per-merchant exposure score within a prediction."""

    __tablename__ = "node_exposures"
    __table_args__ = (
        UniqueConstraint("prediction_id", "merchant_id", name="uq_node_exposure"),
        Index("ix_node_exposures_prediction", "prediction_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prediction_id: Mapped[str] = mapped_column(String(64), nullable=False)
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False)

    exposure_score: Mapped[float] = mapped_column(Float, nullable=False)
    expected_shortfall_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    expected_hit_t: Mapped[float | None] = mapped_column(Float)
    hit_t_lower: Mapped[float | None] = mapped_column(Float)
    hit_t_upper: Mapped[float | None] = mapped_column(Float)
    hop_distance: Mapped[int | None] = mapped_column(Integer)
    contributing_sources: Mapped[dict[str, Any]] = mapped_column(JsonType, default=list)


class EvaluationRow(Base, TimestampMixin):
    """A scored comparison of a prediction or plan against ground truth."""

    __tablename__ = "evaluations"
    __table_args__ = (
        Index("ix_evaluations_run", "run_id"),
        Index("ix_evaluations_predictor", "predictor", "model_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evaluation_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    run_id: Mapped[str | None] = mapped_column(String(64))
    prediction_id: Mapped[str | None] = mapped_column(String(64))
    plan_id: Mapped[str | None] = mapped_column(String(64))
    shock_id: Mapped[str | None] = mapped_column(String(64))

    name: Mapped[str] = mapped_column(String(255), default="")
    predictor: Mapped[str | None] = mapped_column(String(48))
    optimizer: Mapped[str | None] = mapped_column(String(48))
    model_version: Mapped[str | None] = mapped_column(String(64))
    dataset_version: Mapped[str | None] = mapped_column(String(64))
    horizon_hours: Mapped[float | None] = mapped_column(Float)

    precision: Mapped[float | None] = mapped_column(Float)
    recall: Mapped[float | None] = mapped_column(Float)
    f1: Mapped[float | None] = mapped_column(Float)
    pr_auc: Mapped[float | None] = mapped_column(Float)
    hit_time_mae_hours: Mapped[float | None] = mapped_column(Float)
    disruption_prevented: Mapped[float | None] = mapped_column(Float)
    optimality_gap: Mapped[float | None] = mapped_column(Float)

    seed: Mapped[int | None] = mapped_column(BigInteger)
    config_hash: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")


class ExperimentRow(Base, TimestampMixin):
    """A named group of runs sharing a hypothesis and a config."""

    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    config_hash: Mapped[str | None] = mapped_column(String(64))
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tags: Mapped[dict[str, Any]] = mapped_column(JsonType, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")


class ProvenanceRow(Base):
    """Audit trail linking a canonical payment event to its raw source record.

    Normalization is lossy - paise become rupees, epochs become simulation
    hours, provider statuses collapse into our enum. This table keeps the
    pre-conversion values so any canonical figure can be traced and re-derived.

    ``(source_system, source_id)`` is unique, which is what makes REST backfill
    idempotent: re-importing an overlapping window is a no-op rather than a
    duplicated cash flow.
    """

    __tablename__ = "event_provenance"
    __table_args__ = (
        UniqueConstraint("source_system", "source_id", name="uq_provenance_source"),
        Index("ix_provenance_event", "event_id"),
        Index("ix_provenance_dataset", "dataset_id"),
        Index("ix_provenance_ingested", "ingested_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_id: Mapped[str | None] = mapped_column(String(64))

    source_system: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(128))
    pipeline_version: Mapped[str] = mapped_column(String(32), nullable=False)

    # Pre-normalisation values, kept verbatim.
    raw_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    raw_timestamp: Mapped[int | None] = mapped_column(BigInteger)
    raw_currency: Mapped[str | None] = mapped_column(String(8))

    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    notes: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


class ImportRunRow(Base, TimestampMixin):
    """One REST backfill window, with its acceptance/rejection tallies.

    Recording the window makes an import resumable: the next run starts from the
    last successfully imported timestamp rather than re-walking history.
    """

    __tablename__ = "import_runs"
    __table_args__ = (Index("ix_import_runs_source", "source_system", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    dataset_id: Mapped[str | None] = mapped_column(String(64))
    source_system: Mapped[str] = mapped_column(String(32), nullable=False)
    resource: Mapped[str] = mapped_column(String(32), nullable=False, default="payments")

    window_from: Mapped[int | None] = mapped_column(BigInteger)
    window_to: Mapped[int | None] = mapped_column(BigInteger)
    fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    error: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


class WebhookEventRow(Base, TimestampMixin):
    """Raw inbound Razorpay webhooks.

    Persisted *before* processing and keyed on the provider's event id, so a
    redelivered webhook is recognised and not double-counted into the network.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_webhook_event"),
        Index("ix_webhook_processed", "processed", "received_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="razorpay")
    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    signature_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


# -------------------------------------------------------------------- execution
#
# Everything below records what a human approved and what was sent to a payment
# provider. None of it feeds the analysis, and the analysis serves without it:
# these tables are needed only by the execution routes.
#
# Executed money is stored in the sandbox currency's **minor units** (integer),
# never as a float, and always next to its currency code. Analytic rupee amounts
# are stored as the exact text of the float the snapshot holds, so the two kinds
# of amount cannot be summed by accident.


class PayPalIdentityRow(Base, TimestampMixin):
    """A merchant in the synthetic network mapped to a PayPal sandbox account.

    Only mapped merchants can be paid out to. This is the "live subset": the
    analysis covers all merchants, execution only these.
    """

    __tablename__ = "paypal_identities"
    __table_args__ = (UniqueConstraint("merchant_id", name="uq_paypal_identity_merchant"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient_type: Mapped[str] = mapped_column(String(16), nullable=False)
    receiver: Mapped[str] = mapped_column(String(127), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")


class ExecutionApprovalRow(Base):
    """A human's explicit approval of one exact plan, bound to its hash."""

    __tablename__ = "execution_approvals"
    __table_args__ = (
        UniqueConstraint("approval_id", name="uq_execution_approval_id"),
        Index("ix_execution_approval_plan", "plan_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    approval_id: Mapped[str] = mapped_column(String(40), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    approver: Mapped[str] = mapped_column(String(80), nullable=False)
    executed_total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    demo_scale: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ExecutionRecordRow(Base, TimestampMixin):
    """One action of an approved plan, and what the provider did with it.

    ``idempotency_key`` is unique: the database itself refuses a second record
    for the same (approved plan, action), which is the guard against paying
    twice. The same key is sent to PayPal as ``PayPal-Request-Id`` and
    ``sender_batch_id``, so PayPal refuses a duplicate too.
    """

    __tablename__ = "execution_records"
    __table_args__ = (
        UniqueConstraint("record_id", name="uq_execution_record_id"),
        UniqueConstraint("idempotency_key", name="uq_execution_idempotency"),
        Index("ix_execution_record_plan", "plan_hash"),
        Index("ix_execution_record_scenario", "scenario_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_id: Mapped[str] = mapped_column(String(40), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_key: Mapped[str] = mapped_column(String(64), nullable=False)
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    analytic_amount_inr: Mapped[str] = mapped_column(String(40), nullable=False)
    executed_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    provider_batch_id: Mapped[str | None] = mapped_column(String(64))
    provider_item_id: Mapped[str | None] = mapped_column(String(64))
    provider_status: Mapped[str | None] = mapped_column(String(32))
    debug_id: Mapped[str | None] = mapped_column(String(64))


class ExecutionEventRow(Base):
    """Append-only audit trail: every approval, submission and status change."""

    __tablename__ = "execution_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_execution_event_id"),
        Index("ix_execution_event_scenario", "scenario_id"),
        Index("ix_execution_event_record", "record_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(40), nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    record_id: Mapped[str | None] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str | None] = mapped_column(String(16))
    detail: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ReentryAnalysisRow(Base):
    """A failed payout, re-entered as a shock and analysed within fixed bounds.

    One row per (execution record, trigger): a redelivered or out-of-order
    failure event cannot start a second analysis. The full scenario the bounded
    analysis returned is kept, so the new plan it offers survives a restart; it
    is never executed without a fresh approval of its own.
    """

    __tablename__ = "reentry_analyses"
    __table_args__ = (
        UniqueConstraint("analysis_id", name="uq_reentry_analysis_id"),
        UniqueConstraint("record_id", "trigger", name="uq_reentry_record_trigger"),
        Index("ix_reentry_scenario", "scenario_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[str] = mapped_column(String(40), nullable=False)
    record_id: Mapped[str] = mapped_column(String(40), nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False)
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    shock: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PayPalWebhookEventRow(Base):
    """Raw inbound PayPal webhooks, recorded once before anything acts on them.

    ``event_id`` is unique: two concurrent deliveries of one event produce one
    row, which is the race-safe dedupe. Only verified events are stored; a
    delivery that fails verification is refused before it reaches this table.
    Transmission headers are kept as provenance; nothing secret is.
    """

    __tablename__ = "paypal_webhook_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_paypal_webhook_event"),
        Index("ix_paypal_webhook_type", "event_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    verification: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    transmission: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
