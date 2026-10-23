/**
 * 05 expanded — the certificate.
 *
 * Everything here was produced when the proof artifact was built, from the same
 * frozen snapshot the other four views read; none of it is written by hand. The
 * weaknesses come first, each with the number that shows it, because a reader
 * deciding whether to trust the figures above should meet those before the
 * passes.
 */

import { useEffect, useState } from 'react'
import { ApiError, api } from '../../api/client'
import type { ProofView } from '../../api/schema'
import { count, hours } from '../../lib/format'
import { ExpandedAnalysis, XSection } from '../ExpandedAnalysis'
import { InferenceQuality } from '../InferenceQuality'
import { Seal } from '../Seal'
import './proof-expanded.css'

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

  return (
    <ExpandedAnalysis
      title="Certificate of inspection"
      subtitle="What was checked, what was measured, and what failed. Built offline from the frozen snapshot, content addressed, and refused by the server if a byte of it changes."
      onClose={onClose}
    >
      {error ? (
        <p className="px__missing">
          {error.status === 404 ? 'The proof artifact has not been built on this server, so nothing is shown in its place.' : error.message}
        </p>
      ) : !proof ? (
        <p className="px__missing">Reading the certificate…</p>
      ) : (
        <div className="px">
          <div className="px__seals">
            <Seal src="GET /structure/proof" refId={proof.artifact_id}>
              content hash <span className="num">{proof.content_hash.slice(0, 16)}</span>
            </Seal>
            <span className="px__from">
              from snapshot <span className="num">{proof.provenance.snapshot_id}</span> and{' '}
              <span className="num">{proof.provenance.speed_artifact_id}</span>
            </span>
          </div>

          <XSection label="What failed" note="first, before the passes">
            <ol className="px__failed">
              {proof.failed.map((f, i) => (
                <li key={i}>
                  <h4>{f.title}</h4>
                  <p>{f.detail}</p>
                </li>
              ))}
            </ol>
          </XSection>

          <XSection label="History used to estimate ends before every shock" note="the leakage barrier, per scenario">
            <Barrier proof={proof} />
          </XSection>

          <XSection label="How well the links were inferred" note="each strength against the generator’s truth">
            <InferenceQuality />
          </XSection>

          <XSection label="Results, with their intervals" note="an interval is printed wherever one exists">
            <ul className="px__results">
              {proof.results.map((r, i) => {
                const pctUnit = r.unit === '%'
                const f = (v: number) => (pctUnit ? `${v.toFixed(1)}%` : v.toFixed(2))
                return (
                  <li key={i}>
                    <span className="px__rlabel">
                      {r.label}
                      <em>{r.note}</em>
                    </span>
                    <span className="px__rvalue num">
                      {f(r.value)}
                      <em>{r.low !== null && r.high !== null ? `${f(r.low)} – ${f(r.high)}` : 'no interval'}</em>
                    </span>
                  </li>
                )
              })}
            </ul>
          </XSection>

          <XSection label="Checks, as stamped when it was built" note={`${count(proof.audit.length)} checks`}>
            <ul className="px__audit">
              {proof.audit.map((a) => (
                <li key={a.name} className={a.passed ? '' : 'is-failed'}>
                  <i aria-hidden="true">{a.passed ? '✓' : '†'}</i>
                  {a.name.replace(/_/g, ' ')}
                  <span className="sr-only">{a.passed ? 'passed' : 'failed'}</span>
                </li>
              ))}
            </ul>
          </XSection>
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
      <p className="px__bnote">
        Bars are the payment history the estimator saw, clipped on the left: it runs back to hour{' '}
        <span className="num">{hours(Math.min(...rows.map((r) => r.first_observed_hours ?? 0)))}</span>. The red rule is the shock.
        In every scenario the last payment seen comes before it.
      </p>
    </div>
  )
}
