/**
 * How well the structure was inferred.
 *
 * Which merchants are linked is read from the payments themselves; what the
 * estimator infers is how strongly each link carries a shortfall. Each strength
 * is scored against the generator's own truth, link by link, as a rank
 * correlation with its bootstrap interval. The weak one is shown as weak.
 */

import { useEffect, useState } from 'react'
import { ApiError, api } from '../api/client'
import type { ProofView } from '../api/schema'
import { count, hours } from '../lib/format'
import { Seal } from './Seal'
import './inference-quality.css'

export function InferenceQuality() {
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

  if (error) {
    return (
      <p className="iq__missing">
        {error.status === 404 ? 'Recovery has not been measured on this server: the proof artifact is not built.' : error.message}
      </p>
    )
  }
  if (!proof) return <p className="iq__missing">Reading the recovery measurements…</p>
  const rec = proof.recovery

  return (
    <div className="iq">
      <p className="iq__lead">
        All <b className="num">{count(rec.n_matched)}</b> of the generator’s {count(rec.n_true)} links appear in the payment
        history, so finding them is not the hard part. What is inferred is how strongly each one carries a shortfall:
      </p>
      <ul className="iq__rows">
        {rec.parameters.map((p) => {
          const at = (v: number) => `${Math.max(0, Math.min(1, v)) * 100}%`
          const weak = p.rank_correlation < 0.5
          return (
            <li key={p.name} className={`iq__row${weak ? ' is-weak' : ''}`}>
              <span className="iq__label">{p.label}</span>
              <span className="iq__axis" aria-hidden="true">
                <i className="iq__band" style={{ left: at(p.low), width: `calc(${at(p.high)} - ${at(p.low)})` }} />
                <i className="iq__point" style={{ left: at(p.rank_correlation) }} />
              </span>
              <span className="iq__value num">
                {p.rank_correlation.toFixed(2)}
                <em>
                  {p.low.toFixed(2)}–{p.high.toFixed(2)}
                </em>
              </span>
              <span className="iq__err num">
                off by {p.unit === 'h' ? hours(p.mean_abs_error) : p.mean_abs_error.toFixed(2)} on average
              </span>
            </li>
          )
        })}
      </ul>
      <p className="iq__foot">
        Rank correlation with the truth over {count(rec.parameters[0]?.n_edges ?? 0)} links, 0 to 1; bands are 95 % bootstrap
        intervals over links.
      </p>
      <div className="iq__seals">
        <Seal src="GET /network/dependencies" refId={proof.provenance.snapshot_id} />
        <Seal src="GET /structure/proof" refId={proof.artifact_id}>
          scored against the generator’s truth
        </Seal>
      </div>
    </div>
  )
}
