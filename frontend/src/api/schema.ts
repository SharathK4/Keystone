// Generated from the backend's OpenAPI schema by scripts/gen_frontend_types.py.
// Do not edit by hand; re-run the script after a backend change.

/** One merchant the shock reaches, and how hard. */
export interface AffectedMerchant {
  merchant_id: string
  rank: number
  probability_constrained?: number | null
  time_to_constraint_hours: number | null
  disrupted_value: number
  cascade_depth: number | null
  sector: string
  tier: string
}

/** ``POST /scenarios/analyze`` - a shock the caller chose. */
export interface AnalyzeRequest {
  merchant_ids: string[]
  magnitude_multiple?: number
  onset_hours?: number
  max_actions?: number
}

export interface ApprovalRequest {
  approver: string
  plan_hash: string
}

export interface ApprovalView {
  approval_id: string
  scenario_id: string
  snapshot_id: string
  plan_hash: string
  provider: string
  approver: string
  approved_at: string
  executed_total: MoneyView
  demo_scale: string
}

/** One plan, replayed with the shock, compared against no plan at all. */
export interface ArmResult {
  actions: SpeedAction[]
  cost_inr: number
  disruption_index: number
  disruption_cut_pct: number
  payments_kept_on_time_inr: number
  merchants_protected: number
  defaults_avoided: number
  reduction: ObjectiveTerms
}

export interface AuditLine {
  name: string
  passed: boolean
}

export interface BootstrapSpec {
  resamples: number
  seed: number
  level: number
  unit: "scenario"
}

export interface CapabilityView {
  name: string
  available: boolean | null
  reason: string
  endpoint: string | null
  http_status: number | null
  verified_in_sandbox: boolean
}

export interface CascadeResponse {
  run_id: string
  shock_id: string | null
  plan_id: string | null
  horizon_hours: number
  disruption: number | null
  disruption_breakdown: Record<string, number>
  summary: Record<string, unknown>
  affected_ids: string[]
  downstream_affected_ids: string[]
  timeline?: Record<string, string[]>
  outcomes?: NodeOutcomeView[]
}

/** How much to trust the projection, and why. */
export interface Confidence {
  source: "propagation" | "artifact"
  calibrated: boolean
  model_version?: string | null
  robust_mode: boolean
  n_scenarios_considered: number
  disruption_spread?: number | null
  recommendation_stable_under_uncertainty?: boolean | null
  note?: string
}

/** The feasible set a caller is willing to act inside. */
export interface ConstraintsIn {
  budget?: number | null
  max_actions?: number
  max_per_merchant?: number
  max_extension_hours?: number
  max_acceleration_hours?: number
  max_tranches?: number
  min_amount?: number
}

/** ``POST /api/v1/predict/contagion``. */
export interface ContagionRequest {
  network: NetworkStateIn
  shock: ShockIn
  observation_cutoff?: number
  horizon_hours?: number
  model_version?: string | null
}

export interface ContagionResponse {
  model_version: string
  feature_schema_version: string
  artifact_hash: string
  calibrator: string
  threshold: number
  observation_cutoff: number
  horizon_hours: number
  interval_edges: number[]
  n_flagged: number
  nodes: NodePredictionOut[]
  latency_ms: number
}

/** Before and after, both measured by replaying the action. */
export interface CounterfactualView {
  baseline_disruption: number
  attributable_disruption: number
  baseline_disrupted_value: number
  baseline_affected: number
  with_intervention_disruption: number
  with_intervention_disrupted_value: number
  with_intervention_affected: number
  disruption_prevented: number
  disruption_reduction_pct: number
  commerce_preserved: number
  merchants_protected: number
  cost: number
  capital_efficiency: number | null
  optimality_gap?: number | null
  regret?: number | null
}

export interface CreateShockRequest {
  name?: string
  description?: string
  components: ShockComponentRequest[]
}

/** The admin data contract, computed - never hardcoded. */
export interface DashboardSummary {
  network: NetworkOverview
  merchants_vulnerable: number
  vulnerable_share: number
  total_value_exposed: number
  top_systemic: SystemicEntry[]
  top_dependencies: DependencyView[]
  recent_scenarios: ScenarioSummary[]
  mean_failure_probability: number | null
  projected_disrupted_value: number
  intervention_opportunities: number
  best_capital_efficiency: number | null
  total_recommended_capital: number
  recommended_offer: OfferContract | null
  execution: ExecutionStatus
  provenance: Provenance
}

export interface DatasetDetail {
  dataset_id: string
  dataset_version: string
  source: string
  seed: number
  config?: Record<string, unknown>
  stats?: Record<string, unknown>
  graph?: Record<string, unknown>
}

export interface DatasetSummary {
  dataset_id: string
  dataset_version: string
  source: string
  seed: number
  stats?: Record<string, unknown>
  created_at?: string | null
  notes?: string
}

export interface DelayResult {
  delay_hours: number
  frozen: ArmResult
  replanned: ArmResult
}

export interface DemoScaleView {
  currency: string
  scale: string
  unit: string
  rounding: string
  note?: string
}

/** How the hosted demo treats execution, so a page can say so up front. */
export interface DemoStatusView {
  mode: "off" | "recorded"
  token_required: boolean
  token_header: string
  executes_only: string | null
  rate_limit: number | null
  rate_window_s: number | null
  note: string | null
}

/** One estimated relationship. Strength is inferred, never observed directly. */
export interface DependencyView {
  source_id: string
  target_id: string
  pass_through: number
  conditional_probability: number
  lag_mean_hours: number
  reliability: number
  observed_value: number
  n_events: number
  estimated?: boolean
}

export interface EdgeView {
  source_id: string
  target_id: string
  pass_through: number
  conditional_probability: number
  reliability: number
  mean_lag_hours: number
  n_events: number
  mean_amount: number
  estimator: string | null
  is_ground_truth: boolean
  confidence: number
}

export interface EvaluateRequest {
  shock_id: string
  prediction_id: string
  horizon_hours?: number
  estimator?: string | null
}

export interface EvaluationResponse {
  evaluation_id: string
  name: string
  predictor: string | null
  optimizer: string | null
  headline: Record<string, unknown>
  by_horizon?: Record<string, Record<string, number>>
}

/** The exact plan an approver sees. ``plan_hash`` is what they approve. */
export interface ExecutablePlanView {
  scenario_id: string
  snapshot_id: string
  provider: string
  plan_hash: string
  complete: boolean
  incomplete_reason: string | null
  executes_whole_plan: boolean
  replayed_cost_inr: string
  executed_total: MoneyView
  actions: PlannedActionView[]
  demo: DemoScaleView
}

export interface ExecuteRequest {
  approval_id: string
}

export interface ExecutionRecordView {
  record_id: string
  approval_id: string
  plan_hash: string
  scenario_id: string
  action_key: string
  merchant_id: string
  action_type: string
  analytic_amount_inr: string
  executed: MoneyView
  provider: string
  status: "planned" | "submitted" | "pending" | "held" | "succeeded" | "failed" | "unknown"
  reason: string | null
  paypal_payout_batch_id: string | null
  paypal_payout_item_id: string | null
  paypal_status: string | null
  paypal_debug_id: string | null
  created_at: string
  updated_at: string
}

/** What the payment provider can actually do right now. */
export interface ExecutionStatus {
  provider: string
  mode: string
  configured: boolean
  api_reachable: boolean
  capabilities: Record<string, boolean>
  executable_intervention_types: string[]
  fallback_provider: string
  note: string
}

export interface FailedItem {
  title: string
  detail: string
}

export interface GenerateNetworkRequest {
  n_merchants?: number
  n_layers?: number
  seed?: number | null
  horizon_hours?: number
  history_hours?: number
  mean_out_degree?: number
  coverage_low?: number
  coverage_high?: number
  notes?: string
}

export interface GraphView {
  dataset_id: string
  stats: Record<string, unknown>
  nodes: MerchantSummary[]
  edges: EdgeView[]
}

export interface HTTPValidationError {
  detail?: ValidationError[]
}

export interface HealthResponse {
  status: string
  version: string
  environment: string
  database: Record<string, unknown>
  razorpay: Record<string, unknown>
  checks?: Record<string, string>
}

/** Mean over scenarios, with a percentile bootstrap interval over scenarios. */
export interface Interval {
  mean: number
  low: number
  high: number
}

/** An action to replay. Same shape the recommender returns. */
export interface InterventionIn {
  type: InterventionType
  merchant_id: string
  t: number
  amount?: number
  shift_hours?: number
  tranches?: number
  target_obligation_id?: string | null
}

/** One action the backend would consider, with its measured effect. */
export interface InterventionOption {
  intervention_id: string
  type: string
  merchant_id: string
  liquidity_required: number
  amount: number
  duration_hours: number
  apply_at_hours: number
  cost: number
  predicted_downstream_disruption: number
  disruption_prevented: number
  capital_efficiency: number | null
  confidence: number
  feasible: boolean
  constraint_violations?: string[]
  rationale?: Record<string, number>
  selected?: boolean
}

export interface InterventionOut {
  intervention_id: string
  type: InterventionType
  merchant_id: string
  t: number
  amount: number
  shift_hours: number
  tranches: number
  target_obligation_id: string | null
  cost: number
  description: string
  explanation: Record<string, unknown>
}

/** Candidate financial interventions the optimiser may deploy. */
export type InterventionType = "liquidity_injection" | "receivable_acceleration" | "supplier_term_extension" | "repayment_restructure" | "credit_line_increase"

export interface InterventionView {
  intervention_id: string
  type: InterventionType
  merchant_id: string
  t: number
  amount: number
  shift_hours: number
  tranches: number
  target_obligation_id: string | null
  cost: number
  description: string
}

/** The barrier for one scenario: history used to estimate ends before the shock. */
export interface LeakageRow {
  scenario_id: string
  family: string
  onset_hours: number
  first_observed_hours: number | null
  last_observed_hours: number | null
  n_observed_events: number
  holds: boolean
}

export interface LearnDependenciesRequest {
  t_end?: number
  em_iterations?: number
  max_parents_per_event?: number
}

export interface LedgerEventView {
  event_id: string
  recorded_at: string
  kind: string
  status: string | null
  record_id: string | null
  detail: Record<string, unknown>
}

/** Approvals, records and the append-only audit trail for one scenario. */
export interface LedgerView {
  scenario_id: string
  approvals: ApprovalView[]
  records: ExecutionRecordView[]
  events: LedgerEventView[]
  demo: DemoScaleView
}

export interface MeasuredResult {
  label: string
  value: number
  low: number | null
  high: number | null
  unit: string
  note: string
}

/** Coarse sector label. Drives seasonality and margin priors. */
export type MerchantSector = "manufacturing" | "wholesale" | "retail" | "logistics" | "services" | "agri" | "construction" | "other"

/** A merchant as a caller can observe it. */
export interface MerchantStateIn {
  merchant_id: string
  sector?: MerchantSector
  tier?: MerchantTier
  opening_balance: number
  credit_limit?: number
  operating_floor?: number
}

export interface MerchantSummary {
  merchant_id: string
  name: string
  sector: string
  tier: string
  opening_balance: number
  operating_floor: number
  credit_limit: number
  initial_buffer: number
  systemic_weight: number
  payment_discipline: number
}

/** Size band. Determines buffer depth and exogenous revenue scale. */
export type MerchantTier = "micro" | "small" | "medium" | "large" | "anchor"

/** One merchant: who it is, what it holds, what it owes, how exposed it is. */
export interface MerchantView {
  merchant_id: string
  sector: string
  tier: string
  opening_balance: number
  credit_limit: number
  operating_floor: number
  liquidity_buffer: number
  payables_in_horizon: number
  receivables_in_horizon: number
  net_position: number
  cover_ratio?: number | null
  throughput: number
  in_degree: number
  out_degree: number
  systemic_importance?: number | null
  systemic_rank?: number | null
  vulnerable: boolean
}

export interface MoneyView {
  currency: string
  value: string
}

/** Reported, never assumed: does the cut only fall as the delay grows? */
export interface Monotonicity {
  frozen_non_increasing: boolean
  replanned_non_increasing: boolean
  rises: string[]
}

/** Size and shape of the network, and how much value sits in it. */
export interface NetworkOverview {
  dataset_id: string
  dataset_version: string
  scale: string
  n_merchants: number
  n_relationships: number
  n_payment_events: number
  n_obligations: number
  total_payment_value: number
  total_obligation_value: number
  obligation_value_in_horizon: number
  horizon_hours: number
  currency?: string
  sectors?: Record<string, number>
  tiers?: Record<string, number>
}

export interface NetworkStateIn {
  network_id?: string
  merchants: MerchantStateIn[]
  obligations?: ObligationIn[]
  payments?: PaymentIn[]
}

export interface NodeExposureView {
  merchant_id: string
  exposure_score: number
  expected_shortfall: number
  expected_hit_t: number | null
  hop_distance: number | null
  contributing_sources?: string[]
}

export interface NodeOutcomeView {
  merchant_id: string
  is_affected: boolean
  became_constrained: boolean
  became_defaulted: boolean
  first_constrained_t: number | null
  hop_distance: number | null
  value_delayed: number
  weighted_delay: number
  defaults_caused: number
  min_buffer: number
  final_status: string
}

export interface NodePredictionOut {
  merchant_id: string
  probability_constrained: number
  expected_time_to_constraint_hours: number | null
  probability_by: Record<string, number>
}

export interface ObjectiveIn {
  form?: "penalised" | "constrained"
  lambda?: number
  epsilon?: number | null
  epsilon_fraction?: number
}

/** The three terms of the disruption index. ``total`` is their sum. */
export interface ObjectiveTerms {
  delay: number
  default: number
  deficit: number
  total: number
}

export interface ObligationIn {
  obligation_id: string
  debtor_id: string
  creditor_id: string
  amount: number
  issued_t: number
  due_t: number
  priority?: number
  amount_paid?: number
}

/** A structured recommendation for one merchant. Not a financial product. */
export interface OfferContract {
  offer_id: string
  merchant_id: string
  scenario_id: string
  intervention_type: string
  proposed_amount: number
  currency?: string
  duration_hours: number
  repayment: RepaymentTerms
  indicative_cost: number
  indicative_rate_annual_pct?: number | null
  rationale: string
  expected_network_benefit: Record<string, number>
  eligibility: OfferEligibility
  status?: "recommended"
  disclaimer?: string
  provenance: Provenance
}

export interface OfferEligibility {
  eligible: boolean
  criteria: Record<string, boolean>
  constraints: Record<string, number | null>
}

export interface OptimizeRequest {
  shock_id: string
  optimizer?: OptimizerKind
  budget?: number | null
  max_actions?: number
  top_k_nodes?: number
  max_candidates?: number
  horizon_hours?: number
  lazy?: boolean
  estimator?: string | null
}

export interface OptimizeResponse {
  plan_id: string
  optimizer: string
  interventions: InterventionView[]
  total_cost: number
  baseline_disruption: number
  achieved_disruption: number
  disruption_prevented: number
  disruption_prevented_per_rupee: number | null
  candidates_considered: number
  simulations_run: number
  elapsed_ms: number
  notes?: Record<string, unknown>
}

export type OptimizerKind = "greedy" | "cp_sat_knapsack" | "exhaustive" | "random" | "top_exposure"

export interface PagedMerchants {
  items: MerchantSummary[]
  total: number
  limit: number
  offset: number
}

export interface PagedRuns {
  items: RunView[]
  limit: number
  offset: number
}

/** How well one estimated strength tracks the generator's true value, link by link. */
export interface ParameterRecovery {
  name: string
  label: string
  n_edges: number
  rank_correlation: number
  low: number
  high: number
  mean_abs_error: number
  unit: string
}

/** What the PayPal sandbox can do for this app, each answer with its reason. */
export interface PayPalStatusView {
  provider: "paypal_sandbox"
  mode: string
  configured: boolean
  refused: string | null
  probed: boolean
  probed_at: string | null
  api_host: string
  capabilities: CapabilityView[]
  executable_intervention_types: string[]
  demo: DemoScaleView
}

/** What happened to one delivery. Returned with 200 for every verified event. */
export interface PayPalWebhookAck {
  status: "accepted" | "duplicate" | "ignored"
  event_id: string
  event_type: string
  record_id: string | null
  record_status: string | null
  applied: boolean
  reason: string | null
  reentry_scheduled: boolean
}

export interface PaymentIn {
  payer_id: string
  payee_id: string
  amount: number
  t: number
  obligation_id?: string | null
}

export interface PlannedActionView {
  key: string
  source: "recommended" | "offer"
  merchant_id: string
  action_type: string
  analytic_amount_inr: string
  executed: MoneyView
  mapping: string | null
  recipient: RecipientView | null
  executable: boolean
  reason: string | null
}

export interface PredictRequest {
  shock_id: string
  predictor?: PredictorKind
  horizon_hours?: number
  threshold?: number
  max_hops?: number
  estimator?: string | null
}

export interface PredictionResponse {
  prediction_id: string
  shock_id: string | null
  predictor: string
  model_version: string
  horizon_hours: number
  threshold: number
  inference_ms: number | null
  predicted_affected_ids: string[]
  exposures: NodeExposureView[]
}

/** Which contagion predictor produced a prediction. */
export type PredictorKind = "simulation_oracle" | "linear_threshold" | "hawkes_cascade" | "temporal_gnn"

/** What the shock does to the network if nothing is done. */
export interface ProjectedImpact {
  n_affected: number
  n_defaulted: number
  disrupted_value: number
  disruption_index: number
  network_disruption_index: number
  max_cascade_depth: number
  systemic_exposure: number
}

export interface ProofProvenance {
  kind: "replayed_offline"
  label: string
  snapshot_id: string
  speed_artifact_id: string
  code_version: string
}

export interface ProofView {
  artifact_id: string
  content_hash: string
  label: string
  provenance: ProofProvenance
  leakage: LeakageRow[]
  recovery: RecoveryView
  audit: AuditLine[]
  results: MeasuredResult[]
  failed: FailedItem[]
}

export interface PropagationEventView {
  event_id: string
  sequence: number
  t: number
  type: string
  merchant_id: string
  counterparty_id: string | null
  amount: number
  hop: number
  caused_by: string | null
  status_after: string | null
}

/** What produced a number, and how to reproduce it. */
export interface Provenance {
  run_id: string
  scenario_id?: string | null
  dataset_id: string
  dataset_version: string
  seed: number
  config_hash: string
  model_version?: string | null
  feature_schema_version?: string | null
  simulator_config_hash: string
  optimizer: string
  code_version: string
  created_at: string
}

export interface PullAffected {
  merchant_id: string
  cascade_depth: number | null
  hours_to_constraint: number | null
  disrupted_value_inr: number
}

export interface PullPlan {
  action_type: string
  merchant_id: string
  amount_inr: number
  disruption_cut_pct: number
  payments_kept_on_time_inr: number
}

/** What each figure moves over when the shock is 1.5x or 2.5x instead of 2x. */
export interface PullRange {
  multiples: number[]
  n_affected: number[]
  first_constraint_hours: number[] | null
  disrupted_value_inr: number[]
}

export interface PullRecord {
  merchant_id: string
  tier: string
  sector: string
  magnitude_multiple: number
  magnitude_inr: number
  onset_hours: number
  n_affected: number
  n_defaulted: number
  max_depth: number
  disrupted_value_inr: number
  first_constraint_hours: number | null
  affected: PullAffected[]
  range: PullRange
  plan: PullPlan | null
}

export interface PullsProvenance {
  kind: "replayed_offline"
  label: string
  snapshot_id: string
  shock: string
  multiples: number[]
  central_multiple: number
  limits: Record<string, number>
  code_version: string
}

export interface PullsView {
  artifact_id: string
  content_hash: string
  label: string
  provenance: PullsProvenance
  records: PullRecord[]
}

export interface Rate {
  value: number
  low: number
  high: number
  numerator: number
  denominator: number
}

export interface ReadinessResponse {
  ready: boolean
  detail?: Record<string, unknown>
}

export interface RecipientView {
  recipient_type: string
  receiver: string
  label: string
}

/** ``POST /api/v1/interventions/recommend``. */
export interface RecommendRequest {
  network: NetworkStateIn
  shock: ShockIn
  observation_cutoff?: number
  horizon_hours?: number
  constraints?: ConstraintsIn
  objective?: ObjectiveIn
  max_candidates?: number
  robust?: boolean
  n_scenarios?: number
  seed?: number
}

export interface RecommendResponse {
  model_version: string
  selected: InterventionOut[]
  ranked: Record<string, unknown>[]
  baseline_disruption: number
  residual_disruption: number
  expected_disruption_reduction: number
  cost: number
  capital_efficiency: number | null
  robustness: Record<string, unknown>
  feasibility: Record<string, unknown>
  candidates: Record<string, unknown>
  solver: Record<string, unknown>
  latency_ms: number
}

/** Which links exist is read from observed payments; their strengths are estimated. */
export interface RecoveryView {
  n_true: number
  n_estimated: number
  n_matched: number
  precision: Rate
  recall: Rate
  parameters: ParameterRecovery[]
}

/** The plan the bounded analysis proposes for the new shock. Never executed */
export interface ReentryPlanView {
  action_type: string
  merchant_id: string
  analytic_amount_inr: string
}

/** A failed payout, re-entered as a shock and analysed within fixed bounds. */
export interface ReentryView {
  analysis_id: string
  record_id: string
  scenario_id: string
  merchant_id: string
  trigger: string
  status: "analysed" | "skipped"
  reason: string | null
  missing_inr: string | null
  snapshot_id: string | null
  computed_in_ms: number | null
  adhoc_scenario_id: string | null
  merchants_reached: number | null
  disrupted_value_inr: string | null
  new_plan: ReentryPlanView[]
  created_at: string
}

export interface RepaymentTerms {
  structure: "bullet" | "instalments"
  n_instalments: number
  instalment_amount: number
  first_due_hours: number
  cadence_hours: number
}

/** ``POST /api/v1/scenarios/replay``. */
export interface ReplayRequest {
  network: NetworkStateIn
  shock: ShockIn
  interventions?: InterventionIn[]
  horizon_hours?: number
  seed?: number
}

export interface ReplayResponse {
  model_version: string
  before: Record<string, unknown>
  after: Record<string, unknown>
  disruption_prevented: number
  disruption_reduction_pct: number
  commerce_preserved: number
  cost: number
  capital_efficiency: number | null
  n_interventions: number
  latency_ms: number
}

/** What a persisted run represents. */
export type RunKind = "generation" | "simulation" | "counterfactual" | "training" | "prediction" | "optimization" | "evaluation"

export type RunStatus = "pending" | "running" | "completed" | "failed"

export interface RunView {
  run_id: string
  kind: string
  status: string
  name: string
  dataset_version: string | null
  model_version: string | null
  shock_id: string | null
  plan_id: string | null
  seed: number
  config_hash: string | null
  metrics?: Record<string, unknown>
  duration_ms: number | null
  created_at: string | null
  git_sha: string | null
}

/** One analysed shock. A result, not a session. */
export interface ScenarioSnapshot {
  scenario_id: string
  family: string
  shock: ShockView
  projected_impact: ProjectedImpact
  confidence: Confidence
  affected_merchants: AffectedMerchant[]
  time_to_constraint: TimeToConstraintBucket[]
  recommended_intervention: InterventionOption | null
  alternatives: InterventionOption[]
  counterfactual: CounterfactualView
  offer: OfferContract | null
  provenance: Provenance
  computed_in_ms: number
}

export interface ScenarioSpeed {
  scenario_id: string
  family: string
  seed: number
  dataset_version: string
  shock_onset_hours: number
  in_snapshot: boolean
  has_plan: boolean
  unplanned: ObjectiveTerms
  payments_late_inr: number
  merchants_affected: number
  defaults: number
  delays: DelayResult[]
  monotonicity: Monotonicity
}

/** Enough to render a scenario in a list without fetching the whole thing. */
export interface ScenarioSummary {
  scenario_id: string
  family: string
  headline: string
  n_affected: number
  disrupted_value: number
  max_cascade_depth: number
  recommended_action: string | null
  cost: number
  disruption_reduction_pct: number
  capital_efficiency: number | null
}

export interface ShockComponentIn {
  merchant_id: string
  magnitude: number
  t: number
  kind?: ShockKind
}

export interface ShockComponentRequest {
  merchant_id: string
  magnitude: number
  t?: number
  kind?: ShockKind
  duration_hours?: number
  target_obligation_id?: string | null
}

export interface ShockIn {
  name?: string
  components: ShockComponentIn[]
}

/** What kind of liquidity shock is applied at a node. */
export type ShockKind = "missed_inbound" | "cash_withdrawal" | "credit_line_cut" | "demand_collapse" | "counterparty_default"

export interface ShockResponse {
  shock_id: string
  name: string
  description: string
  total_magnitude: number
  onset_t: number
  origin_ids: string[]
  components: Record<string, unknown>[]
}

/** What went wrong, in business terms. */
export interface ShockView {
  description: string
  origin_merchants: string[]
  magnitude: number
  onset_hours: number
  kind: string
  family: string
}

export interface SimulateRequest {
  shock_id?: string | null
  plan_id?: string | null
  horizon_hours?: number | null
  tick_hours?: number | null
  seed?: number | null
  store_events?: boolean
  estimator?: string | null
}

export interface SpeedAction {
  action_type: string
  merchant_id: string
  lands_at_hours: number
  amount_inr: number
  shift_hours: number
  cost_inr: number
}

export interface SpeedPoint {
  delay_hours: number
  n_scenarios: number
  frozen_cut_pct: Interval
  replanned_cut_pct: Interval
  replanning_gain_pp: Interval
  frozen_kept_inr: Interval
  replanned_kept_inr: Interval
  replanned_mix: Record<string, number>
  replanned_cost_inr: Interval
}

export interface SpeedProvenance {
  kind: "replayed_offline"
  label: string
  delay_note: string
  replay_note: string
  snapshot_id: string
  seeds: number[]
  scale: string
  dataset_versions: string[]
  delays_hours: number[]
  optimiser: string
  objective: Record<string, number>
  replay_config_hash: string
  budget: Record<string, unknown>
  bootstrap: BootstrapSpec
  code_version: string
}

export interface SpeedScenarioRef {
  scenario_id: string
  family: string
  seed: number
  in_snapshot: boolean
  has_plan: boolean
}

export interface SpeedScenarioView {
  artifact_id: string
  content_hash: string
  label: string
  scenario: ScenarioSpeed
}

/** The headline: value of speed aggregated over every scenario with a plan. */
export interface SpeedSummary {
  artifact_id: string
  content_hash: string
  label: string
  provenance: SpeedProvenance
  n_scenarios: number
  n_with_plan: number
  points: SpeedPoint[]
  monotone_scenarios: number
  scenarios: SpeedScenarioRef[]
}

/** A merchant's systemic importance, with the baselines it must not be. */
export interface SystemicEntry {
  merchant_id: string
  rank: number
  importance: number
  marginal_disruption: number
  scale_normalised: number
  downstream_affected: number
  downstream_delayed_value: number
  cascade_depth: number
  time_to_impact_hours: number | null
  throughput: number
  structural_centrality: number
}

export interface SystemicImportanceRequest {
  shock_fraction?: number
  limit?: number | null
  estimator?: string | null
}

export interface SystemicImportanceResponse {
  dataset_id: string
  baseline_disruption: number
  shock_fraction: number
  ranking: Record<string, unknown>[]
}

/** The ranking, plus the correlations that say what it is not. */
export interface SystemicRankingView {
  entries: SystemicEntry[]
  n_sampled: number
  n_merchants: number
  shock_fraction: number
  baseline_rank_correlation: Record<string, number>
  method: string
  provenance?: Provenance | null
}

/** One bar of the time-to-constraint distribution. */
export interface TimeToConstraintBucket {
  from_hours: number
  to_hours: number
  n_merchants: number
  disrupted_value: number
  cumulative_share: number
}

export interface ValidationError {
  loc: (string | number)[]
  msg: string
  type: string
}

export interface WebhookAck {
  status: string
  event_id: string
  ingested?: number
  reason?: string | null
}

/** A merchant with the relationships it sits between. */
export interface lce__api__routers__analytics__MerchantDetail {
  merchant: MerchantView
  upstream: DependencyView[]
  downstream: DependencyView[]
  scenarios_affected_in: string[]
}

export interface lce__api__schemas__MerchantDetail {
  merchant_id: string
  name: string
  sector: string
  tier: string
  opening_balance: number
  operating_floor: number
  credit_limit: number
  initial_buffer: number
  systemic_weight: number
  payment_discipline: number
  exogenous_inflow_rate: number
  operating_burn_rate: number
  autonomy_hours: number | null
  n_suppliers: number
  n_buyers: number
  payables_in_horizon: number
  receivables_in_horizon: number
  metadata?: Record<string, unknown>
}
