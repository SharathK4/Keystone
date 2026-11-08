/**
 * The three claims the proof card and the certificate make, each backed by
 * checks the backend stamped when the proof artifact was built. The counts are
 * read from the artifact, never typed here; a failed or missing check shows.
 */

import type { ProofView } from '../api/schema'

export interface Claim {
  title: string
  /** Audit lines from the proof artifact that back the claim. */
  checks: string[]
  /** Also count the before-the-failure barrier, one check per scenario. */
  leakage?: boolean
}

export const CLAIMS: Claim[] = [
  {
    title: 'PayPal only pays a plan a person approved, once',
    checks: [
      'paypal_live_mode_refused',
      'paypal_production_host_refused',
      'paypal_generic_interface_cannot_pay',
      'paypal_unverified_webhook_refused',
      'paypal_terminal_status_never_regresses',
      'paypal_reconcile_never_resubmits',
      'demo_recorded_mode_needs_token',
    ],
  },
  {
    title: 'Every plan is tested by replaying the week, not just predicted',
    checks: [
      'on_demand_analysis_is_deterministic',
      'on_demand_analysis_is_bounded',
      'every_scenario_has_provenance',
      'speed_artifact_verified',
      'speed_zero_delay_matches_snapshot',
    ],
  },
  {
    title: 'The model never saw the payments it is tested on',
    checks: ['snapshot_carries_no_payment_history', 'snapshot_overlay_is_estimated'],
    leakage: true,
  },
]

export function tally(proof: ProofView, claim: Claim): { passed: number; total: number } {
  const byName = new Map(proof.audit.map((a) => [a.name, a.passed]))
  let total = claim.checks.length
  let passed = claim.checks.filter((n) => byName.get(n) === true).length
  if (claim.leakage) {
    total += proof.leakage.length
    passed += proof.leakage.filter((r) => r.holds).length
  }
  return { passed, total }
}
