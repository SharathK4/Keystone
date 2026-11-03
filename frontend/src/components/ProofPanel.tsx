/**
 * Card 05 — the proof.
 *
 * The other four cards say what the system found; this one says why to believe
 * it, and where not to. It makes three claims a reader would otherwise have to
 * take on trust, each backed by checks the backend stamped when the proof
 * artifact was built. The count beside a claim is those checks, read from the
 * artifact, never typed here; a failed or missing check shows as one. The
 * certificate, with the weaknesses listed first, is one click away.
 */

import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { ProofView } from '../api/schema'
import { Panel } from './Panel'
import { TexturedTile } from './TexturedTile'
import './proof-panel.css'

interface Claim {
  title: string
  /** Audit lines from the proof artifact that back the claim. */
  checks: string[]
  /** Also count the leakage barrier, one check per scenario. */
  leakage?: boolean
}

const CLAIMS: Claim[] = [
  {
    title: 'PayPal is fenced: sandbox only, paid at most once, unverified webhooks refused',
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
    title: 'Every plan is scored by replaying the same week, not by the model that proposed it',
    checks: [
      'on_demand_analysis_is_deterministic',
      'on_demand_analysis_is_bounded',
      'every_scenario_has_provenance',
      'speed_artifact_verified',
      'speed_zero_delay_matches_snapshot',
    ],
  },
  {
    title: 'Dependencies are estimated from history that ends before the shock they are tested on',
    checks: ['snapshot_carries_no_payment_history', 'snapshot_overlay_is_estimated'],
    leakage: true,
  },
]

function tally(proof: ProofView, claim: Claim) {
  const byName = new Map(proof.audit.map((a) => [a.name, a.passed]))
  let total = claim.checks.length
  let passed = claim.checks.filter((n) => byName.get(n) === true).length
  if (claim.leakage) {
    total += proof.leakage.length
    passed += proof.leakage.filter((r) => r.holds).length
  }
  return { passed, total }
}

export function ProofPanel({ onOpen }: { onOpen: () => void }) {
  const [proof, setProof] = useState<ProofView | null>(null)
  useEffect(() => {
    let live = true
    api
      .proof()
      .then((p) => live && setProof(p))
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [])

  return (
    <Panel
      index={5}
      title="The proof"
      subtitle="What was checked and measured, and what failed, before any number above is trusted."
      reveals="Open the certificate"
      onOpen={onOpen}
      className="card--wide"
    >
      <TexturedTile tone="graphite" seed={proof?.artifact_id ?? 'proof'} className="pp">
        <div className="pp__body">
          <svg className="pp__seal" viewBox="0 0 200 200" aria-hidden="true">
            <defs>
              <path id="pp-ring" d="M100,100 m-74,0 a74,74 0 1,1 148,0 a74,74 0 1,1 -148,0" />
            </defs>
            <circle cx="100" cy="100" r="92" className="pp__ring" />
            <circle cx="100" cy="100" r="58" className="pp__ring pp__ring--inner" />
            <text className="pp__ringtext">
              <textPath href="#pp-ring" startOffset="0">
                KEYSTONE · INSPECTED · CONTENT ADDRESSED ·
              </textPath>
            </text>
            <path d="M78 101 l15 15 l31 -34" className="pp__tick" />
          </svg>
          {proof ? (
            <ul className="pp__claims">
              {CLAIMS.map((c) => {
                const { passed, total } = tally(proof, c)
                const held = passed === total
                return (
                  <li key={c.title} className={held ? '' : 'is-failed'}>
                    <i aria-hidden="true">{held ? '✓' : '†'}</i>
                    <span className="pp__claim">{c.title}</span>
                    <span className="pp__count num">
                      {passed} of {total} checks {held ? 'pass' : 'pass — see the certificate'}
                    </span>
                  </li>
                )
              })}
              <li className="pp__more">and what failed, listed first inside</li>
            </ul>
          ) : null}
        </div>
      </TexturedTile>
    </Panel>
  )
}
