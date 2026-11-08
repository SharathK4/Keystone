/**
 * 05 expanded — what we checked.
 *
 * Three claims with their pass counts, what we cannot promise, how well the
 * network was learned, and the barrier that keeps the model from seeing the
 * payments it is tested on. Everything comes from the proof artifact built
 * offline; the full list of checks is one click away.
 */

import { useEffect, useState } from 'react'
import { ApiError, api } from '../../api/client'
import type { ProofView } from '../../api/schema'
import { count } from '../../lib/format'
import { CLAIMS, tally } from '../../lib/proofClaims'
import { ExpandedAnalysis, XSection } from '../ExpandedAnalysis'
import { Seal } from '../Seal'
import './proof-expanded.css'

/** The proof artifact's limit titles, said plainly. Unknown ones show as written. */
const PLAIN: Record<string, string> = {
  'Shocks barely cascade on this network': 'On this network a failure rarely spreads past one step.',
  'Some benchmark scenarios have no plan at all': 'Some failures have no rescue worth paying for.',
  "The optimiser's candidate list can miss the best plan": 'The search can miss the best rescue.',
  "One snapshot figure leaves a scenario's own change out":
    'One reference figure leaves out a scenario’s own change.',
  'Links are observed; only their strengths are inferred':
    'Who pays whom is read from payments; only how strongly is learned.',
  'Webhook certificate chain not validated': 'PayPal’s webhook certificate chain is not checked end to end.',
}

function plainLimit(title: string): string {
  return PLAIN[title] ?? title
}

export function ProofExpanded({ onClose }: { onClose: () => void }) {
  const [proof, setProof] = useState<ProofView | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  useEffect(() => {
    let live = true
    api
      .proof()
      .then((p) => live && setProof(p))
      .catch((e: unknown) => live && setError(e instanceof ApiError ? e : null))
    return () => {
      live = false
    }
  }, [])

  const failedChecks = proof ? proof.audit.filter((a) => !a.passed).length : 0
  const recovery = proof?.recovery
  const strength = proof?.recovery.parameters.find((p) => p.name === 'pass_through')
  const timing = proof?.recovery.parameters.find((p) => p.name === 'lag_mean_hours')

  return (
    <ExpandedAnalysis
      title="What we checked"
      subtitle="Every number on this page passed these checks first."
      onClose={onClose}
    >
      {error ? (
        <p className="px__missing">
          {error.status === 404 ? 'The proof has not been built on this server.' : error.message}
        </p>
      ) : !proof ? (
        <p className="px__missing">Reading the checks…</p>
      ) : (
        <div className="px">
          <ul className="px__claims">
            {CLAIMS.map((c) => {
              const { passed, total } = tally(proof, c)
              return (
                <li key={c.title} className={passed === total ? '' : 'is-failed'}>
                  <span className="px__count num">
                    {passed}
                    <em>/{total}</em>
                  </span>
                  <span className="px__claim">{c.title}</span>
                </li>
              )
            })}
          </ul>

          <XSection label="What we can’t promise">
            <ol className="px__failed">
              {proof.failed.map((f, i) => (
                <li key={i} title={f.detail}>
                  {plainLimit(f.title)}
                </li>
              ))}
            </ol>
          </XSection>

          {recovery ? (
            <XSection label="How well it learned the network">
              <ul className="px__numbers">
                <li>
                  <span className="num">
                    {recovery.n_matched}/{recovery.n_true}
                  </span>
                  payment links found
                </li>
                {strength ? (
                  <li>
                    <span className="num">{strength.rank_correlation.toFixed(2)}</span>
                    how much a shortfall passes on, ranked against the truth
                  </li>
                ) : null}
                {timing ? (
                  <li>
                    <span className="num">{timing.rank_correlation.toFixed(2)}</span>
                    how long it takes to arrive, ranked against the truth
                  </li>
                ) : null}
              </ul>
            </XSection>
          ) : null}

          <XSection label="It only saw payments from before each failure">
            <Barrier proof={proof} />
          </XSection>

          <details className="px__all">
            <summary>
              All {count(proof.audit.length)} checks
              {failedChecks ? ` · ${failedChecks} failed` : ' · all passed'}
            </summary>
            <ul className="px__audit">
              {proof.audit.map((a) => (
                <li key={a.name} className={a.passed ? '' : 'is-failed'}>
                  <i aria-hidden="true">{a.passed ? '✓' : '†'}</i>
                  {a.name.replace(/_/g, ' ')}
                  <span className="sr-only">{a.passed ? 'passed' : 'failed'}</span>
                </li>
              ))}
            </ul>
            <Seal src="GET /structure/proof" refId={proof.artifact_id}>
              content hash <span className="num">{proof.content_hash.slice(0, 16)}</span>
            </Seal>
          </details>
        </div>
      )}
    </ExpandedAnalysis>
  )
}

/** The estimator's history ends before each shock. Zoomed onto the cut-off, where the evidence is. */
function Barrier({ proof }: { proof: ProofView }) {
  const rows = proof.leakage
  const lo = Math.min(...rows.map((r) => r.onset_hours)) - 36
  const hi = Math.max(...rows.map((r) => r.onset_hours)) + 12
  const W = 720
  const L = 168
  const rowH = 30
  const H = 18 + rows.length * rowH
  const x = (h: number) => L + ((h - lo) / (hi - lo)) * (W - L - 20)
  return (
    <div className="px__barrier">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="For each scenario, the payment history the estimator saw ends before the shock.">
        {rows.map((r, i) => {
          const y = 10 + i * rowH
          return (
            <g key={r.scenario_id}>
              <text x={L - 12} y={y + 14} textAnchor="end" className="px__blabel">
                {r.family.replace(/_/g, ' ')}
              </text>
              {r.last_observed_hours !== null ? (
                <rect x={L} y={y + 4} width={Math.max(1, x(r.last_observed_hours) - L)} height={12} rx={6} className="px__seen" />
              ) : null}
              <line x1={x(r.onset_hours)} x2={x(r.onset_hours)} y1={y} y2={y + 20} className="px__onset" />
              <text x={x(r.onset_hours) + 7} y={y + 15} className="px__ok">
                {r.holds ? '✓' : '†'}
              </text>
            </g>
          )
        })}
      </svg>
      <p className="px__bnote">Bars are the payments it learned from; the red line is the failure.</p>
    </div>
  )
}
