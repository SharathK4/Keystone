/**
 * Typed mirror of the frozen backend contract.
 *
 * Field-for-field with the Pydantic response models in `lce.snapshot.models`.
 * The backend declares `extra="forbid"`, so if a field here does not exist
 * there it is a bug on this side, not a tolerable difference. Nothing in this
 * file is computed, defaulted or reshaped - it is the wire format.
 */

export interface Provenance {
  run_id: string
  scenario_id: string | null
  dataset_id: string
  dataset_version: string
  seed: number
  config_hash: string
  model_version: string | null
  feature_schema_version: string | null
  simulator_config_hash: string
  optimizer: string
  code_version: string
  created_at: string
}

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
  currency: string
  sectors: Record<string, number>
  tiers: Record<string, number>
}

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
  cover_ratio: number | null
  throughput: number
  in_degree: number
  out_degree: number
  systemic_importance: number | null
  systemic_rank: number | null
  vulnerable: boolean
}

export interface DependencyView {
  source_id: string
  target_id: string
  pass_through: number
  conditional_probability: number
  lag_mean_hours: number
  reliability: number
  observed_value: number
  n_events: number
  estimated: boolean
}

export interface MerchantDetail {
  merchant: MerchantView
  upstream: DependencyView[]
  downstream: DependencyView[]
  scenarios_affected_in: string[]
}

export interface SystemicEntry {
  merchant_id: string
  rank: number
  importance: number
  marginal_disruption: number
  scale_normalised: number
  downstream_affected: number
  downstream_delayed_value: number
  cascade_depth: number
  time_to_impact_hours: number
  throughput: number
  structural_centrality: number
}

export interface SystemicRankingView {
  entries: SystemicEntry[]
  n_sampled: number
  n_merchants: number
  shock_fraction: number
  baseline_rank_correlation: Record<string, number>
  method: string
  provenance: Provenance | null
}

export interface ScenarioSummary {
  scenario_id: string
  family: string
  headline: string
  n_affected: number
  disrupted_value: number
  max_cascade_depth: number
  recommended_action: string | null
  cost: number | null
  disruption_reduction_pct: number
  capital_efficiency: number | null
}

export interface ShockView {
  description: string
  origin_merchants: string[]
  magnitude: number
  onset_hours: number
  kind: string
  family: string
}

export interface ProjectedImpact {
  n_affected: number
  n_defaulted: number
  disrupted_value: number
  /** The shock's own attributable contribution: D(shocked) - D(no shock). */
  disruption_index: number
  /** The whole network's disruption under the shock, background included. */
  network_disruption_index: number
  max_cascade_depth: number
  systemic_exposure: number
}

export interface Confidence {
  source: string
  calibrated: boolean
  model_version: string | null
  robust_mode: boolean
  n_scenarios_considered: number
  disruption_spread: number | null
  recommendation_stable_under_uncertainty: boolean | null
  note: string
}

export interface AffectedMerchant {
  merchant_id: string
  rank: number
  probability_constrained: number | null
  time_to_constraint_hours: number | null
  disrupted_value: number
  cascade_depth: number
  sector: string
  tier: string
}

export interface TimeToConstraintBucket {
  from_hours: number
  to_hours: number
  n_merchants: number
  disrupted_value: number
  cumulative_share: number
}

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
  /** Measured by replaying the action in the simulator. Often exactly 0. */
  disruption_prevented: number
  capital_efficiency: number | null
  confidence: number
  feasible: boolean
  constraint_violations: string[]
  rationale: Record<string, number>
  selected: boolean
}

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
  optimality_gap: number | null
  regret: number | null
}

export interface RepaymentTerms {
  structure: string
  n_instalments: number
  instalment_amount: number
  first_due_hours: number
  cadence_hours: number
}

export interface OfferEligibility {
  eligible: boolean
  criteria: Record<string, boolean>
  constraints: Record<string, number | null>
}

export interface OfferContract {
  offer_id: string
  merchant_id: string
  scenario_id: string
  intervention_type: string
  proposed_amount: number
  currency: string
  duration_hours: number
  repayment: RepaymentTerms
  indicative_cost: number | null
  indicative_rate_annual_pct: number | null
  rationale: string
  expected_network_benefit: Record<string, number>
  eligibility: OfferEligibility
  status: string
  disclaimer: string
  provenance: Provenance
}

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
  computed_in_ms: number | null
  provenance: Provenance
}

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

export interface SnapshotHealth {
  snapshot_id: string
  format_version: number
  dataset_version: string
  scale: string
  seed: number
  n_merchants: number
  n_scenarios: number
  content_hash: string
  created_at: string
  code_version: string
  load_ms: number
  limits: Record<string, number>
  path: string
}
