"""Closed vocabularies shared by the domain, persistence and API layers."""

from __future__ import annotations

from enum import StrEnum


class MerchantSector(StrEnum):
    """Coarse sector label. Drives seasonality and margin priors."""

    MANUFACTURING = "manufacturing"
    WHOLESALE = "wholesale"
    RETAIL = "retail"
    LOGISTICS = "logistics"
    SERVICES = "services"
    AGRI = "agri"
    CONSTRUCTION = "construction"
    OTHER = "other"


class MerchantTier(StrEnum):
    """Size band. Determines buffer depth and exogenous revenue scale."""

    MICRO = "micro"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    ANCHOR = "anchor"


class PaymentDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class PaymentChannel(StrEnum):
    """How the cash moved. Affects settlement lag."""

    UPI = "upi"
    NEFT = "neft"
    RTGS = "rtgs"
    IMPS = "imps"
    CARD = "card"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    CASH = "cash"
    OTHER = "other"


class PaymentStatus(StrEnum):
    CAPTURED = "captured"
    AUTHORIZED = "authorized"
    SETTLED = "settled"
    FAILED = "failed"
    REFUNDED = "refunded"


class ObligationKind(StrEnum):
    """Why one merchant owes another."""

    TRADE_PAYABLE = "trade_payable"      # invoice for goods/services
    LOAN_REPAYMENT = "loan_repayment"    # scheduled credit repayment
    PAYROLL = "payroll"                  # non-network, exogenous sink
    TAX = "tax"                          # non-network, exogenous sink
    RENT = "rent"
    OTHER = "other"


class ObligationStatus(StrEnum):
    """Lifecycle of an obligation o = (i -> j, a_o, d_o)."""

    PENDING = "pending"
    SETTLED = "settled"                  # paid in full at tau_o <= d_o
    SETTLED_LATE = "settled_late"        # paid in full at tau_o > d_o
    PARTIALLY_SETTLED = "partially_settled"
    DEFAULTED = "defaulted"              # unpaid beyond d_o + grace
    CANCELLED = "cancelled"
    RESTRUCTURED = "restructured"        # superseded by child tranches


class RecurrencePattern(StrEnum):
    """Recurrence class of a behavioural edge."""

    ONE_OFF = "one_off"
    DAILY = "daily"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    IRREGULAR = "irregular"


class NodeStatus(StrEnum):
    """Liquidity condition C_i(t) of a merchant."""

    HEALTHY = "healthy"          # buffer above warning threshold
    STRESSED = "stressed"        # buffer positive but below warning threshold
    CONSTRAINED = "constrained"  # cannot fully meet a due obligation
    DEFAULTED = "defaulted"      # breached grace period on some obligation


class ShockKind(StrEnum):
    """What kind of liquidity shock is applied at a node."""

    MISSED_INBOUND = "missed_inbound"      # an expected receivable does not arrive
    CASH_WITHDRAWAL = "cash_withdrawal"    # balance is drained directly
    CREDIT_LINE_CUT = "credit_line_cut"    # K_i is reduced
    DEMAND_COLLAPSE = "demand_collapse"    # exogenous inflow rate drops for a window
    COUNTERPARTY_DEFAULT = "counterparty_default"  # a specific obligation is written off


class InterventionType(StrEnum):
    """Candidate financial interventions the optimiser may deploy."""

    LIQUIDITY_INJECTION = "liquidity_injection"
    RECEIVABLE_ACCELERATION = "receivable_acceleration"
    SUPPLIER_TERM_EXTENSION = "supplier_term_extension"
    REPAYMENT_RESTRUCTURE = "repayment_restructure"
    CREDIT_LINE_INCREASE = "credit_line_increase"


class PropagationEventType(StrEnum):
    """Discrete events emitted by the simulator, in causal order."""

    SHOCK_APPLIED = "shock_applied"
    OBLIGATION_DUE = "obligation_due"
    PAYMENT_MADE = "payment_made"
    PAYMENT_PARTIAL = "payment_partial"
    PAYMENT_MISSED = "payment_missed"
    PAYMENT_DELAYED = "payment_delayed"
    NODE_STRESSED = "node_stressed"
    NODE_CONSTRAINED = "node_constrained"
    NODE_DEFAULTED = "node_defaulted"
    NODE_RECOVERED = "node_recovered"
    INTERVENTION_APPLIED = "intervention_applied"


class RunKind(StrEnum):
    """What a persisted run represents."""

    GENERATION = "generation"
    SIMULATION = "simulation"
    COUNTERFACTUAL = "counterfactual"
    TRAINING = "training"
    PREDICTION = "prediction"
    OPTIMIZATION = "optimization"
    EVALUATION = "evaluation"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PredictorKind(StrEnum):
    """Which contagion predictor produced a prediction."""

    SIMULATION_ORACLE = "simulation_oracle"   # ground truth from the simulator
    LINEAR_THRESHOLD = "linear_threshold"     # analytic propagation baseline
    HAWKES_CASCADE = "hawkes_cascade"         # point-process intensity baseline
    TEMPORAL_GNN = "temporal_gnn"             # learned temporal graph model


class OptimizerKind(StrEnum):
    GREEDY = "greedy"
    CP_SAT_KNAPSACK = "cp_sat_knapsack"
    EXHAUSTIVE = "exhaustive"
    RANDOM = "random"
    TOP_EXPOSURE = "top_exposure"
