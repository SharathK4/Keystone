/**
 * Pull any merchant.
 *
 * The same hundred dots as the scenario field, but every dot is a choice: pick
 * one and its cash is withdrawn at the start of the week, a multiple of its own
 * liquidity slack, and the week is run again. What comes back is who else is
 * forced to miss a payment, when, and the settlement the optimiser would draw.
 *
 * The answer is read from an artifact replayed offline for every merchant at
 * three shock sizes, so it is instant and carries the range it moves over. The
 * same bounded analysis can be run again now, on request; that answer is
 * labelled as computed now, with the time it took.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, api } from '../api/client'
import type { PullRecord, PullsView } from '../api/schema'
import type { AffectedMerchant, MerchantView, ScenarioSnapshot } from '../api/types'
import { count, hours, money, multiple, pct } from '../lib/format'
import { ExposureDotMatrix } from './ExposureDotMatrix'
import { Seal } from './Seal'
import './pull-a-merchant.css'

interface Props {
  merchants: MerchantView[]
}

function headline(rec: PullRecord): string {
  if (rec.n_affected === 0) return 'Absorbed. No merchant crosses into constraint.'
  const others = rec.affected.filter((a) => (a.cascade_depth ?? 0) > 0).length
  if (others === 0) return 'Only the pulled merchant is constrained.'
  return `It reaches ${count(others)} more ${others === 1 ? 'merchant' : 'merchants'}, one hop out.`
}

function depthLabel(depth: number | null): string {
  if (!depth) return 'the pulled merchant'
  return depth === 1 ? 'one hop out' : `${count(depth)} hops out`
}

export function PullAMerchant({ merchants }: Props) {
  const [pulls, setPulls] = useState<PullsView | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [picked, setPicked] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    api
      .pulls()
      .then((p) => {
        if (!live) return
        setPulls(p)
        // Open on the pull that reaches furthest, so the first thing seen is a cascade.
        const first = [...p.records].sort(
          (a, b) => b.n_affected - a.n_affected || a.merchant_id.localeCompare(b.merchant_id),
        )[0]
        setPicked((cur) => cur ?? first?.merchant_id ?? null)
      })
      .catch((e: unknown) => live && setError(e instanceof ApiError ? e : null))
    return () => {
      live = false
    }
  }, [])

  const byMerchant = useMemo(() => new Map(merchants.map((m) => [m.merchant_id, m])), [merchants])
  const rec = pulls?.records.find((r) => r.merchant_id === picked) ?? null

  const affected: AffectedMerchant[] = useMemo(
    () =>
      (rec?.affected ?? []).map((a, i) => ({
        merchant_id: a.merchant_id,
        rank: i + 1,
        probability_constrained: null,
        time_to_constraint_hours: a.hours_to_constraint,
        disrupted_value: a.disrupted_value_inr,
        cascade_depth: a.cascade_depth ?? 0,
        sector: byMerchant.get(a.merchant_id)?.sector ?? '',
        tier: byMerchant.get(a.merchant_id)?.tier ?? '',
      })),
    [rec, byMerchant],
  )

  if (error) {
    return (
      <p className="pull__missing">
        {error.status === 404
          ? 'The pulled-merchant artifact has not been built on this server, so nothing is shown in its place.'
          : error.message}
      </p>
    )
  }
  if (!pulls || !rec) return <p className="pull__missing">Reading every merchant’s failure…</p>

  const m = byMerchant.get(rec.merchant_id)
  const entries = [...rec.affected].sort(
    (a, b) => (a.hours_to_constraint ?? 1e9) - (b.hours_to_constraint ?? 1e9),
  )
  const [lo, hi] = rec.range.multiples

  return (
    <div className="pull">
      <div className="pull__field">
        <ExposureDotMatrix
          merchants={merchants}
          affected={affected}
          origins={[rec.merchant_id]}
          onPick={setPicked}
          picked={picked}
          compact
        />
        <p className="pull__hint">Choose any dot. Arrow keys move through the field.</p>
      </div>

      <div className="pull__read" aria-live="polite">
        <p className="pull__kicker">
          Pulled <b className="num">{rec.merchant_id}</b>
          {m ? ` · ${m.tier} · ${m.sector}` : ''} · its cash cut by {multiple(rec.magnitude_multiple)} its slack
        </p>
        <h4 className="pull__headline">{headline(rec)}</h4>

        <dl className="pull__figs">
          <div>
            <dt>Constrained</dt>
            <dd className="num">{count(rec.n_affected)}</dd>
            <span className="pull__range num">
              {count(rec.range.n_affected[0])}–{count(rec.range.n_affected[1])} across shock sizes
            </span>
          </div>
          <div>
            <dt>Payments delayed</dt>
            <dd className="num">{money(rec.disrupted_value_inr)}</dd>
            <span className="pull__range num">
              {money(rec.range.disrupted_value_inr[0])}–{money(rec.range.disrupted_value_inr[1])}
            </span>
          </div>
          <div>
            <dt>First constraint</dt>
            <dd className="num">{rec.first_constraint_hours === null ? '—' : hours(rec.first_constraint_hours)}</dd>
            <span className="pull__range num">
              {rec.range.first_constraint_hours
                ? `${hours(rec.range.first_constraint_hours[0])}–${hours(rec.range.first_constraint_hours[1])} after the shock`
                : 'nobody constrained at any size'}
            </span>
          </div>
        </dl>

        {entries.length ? (
          <ol className="pull__entries" aria-label="Who it reaches, in order">
            {entries.map((a) => (
              <li key={a.merchant_id} className={(a.cascade_depth ?? 0) > 0 ? 'is-hop' : ''}>
                <span className="num pull__when">{a.hours_to_constraint === null ? '—' : hours(a.hours_to_constraint)}</span>
                <span className="num pull__who">{a.merchant_id}</span>
                <span className="pull__how">{depthLabel(a.cascade_depth)}</span>
                <span className="num pull__what">{money(a.disrupted_value_inr)} delayed</span>
              </li>
            ))}
          </ol>
        ) : null}

        <p className="pull__plan">
          {rec.plan ? (
            <>
              The settlement it draws: {rec.plan.action_type.replace(/_/g, ' ')} to{' '}
              <b className="num">{rec.plan.merchant_id}</b>, {money(rec.plan.amount_inr)}, cutting the week’s network
              disruption by {pct(rec.plan.disruption_cut_pct)}.
            </>
          ) : (
            'No settlement inside the bounds improves this week.'
          )}
        </p>

        <div className="pull__foot">
          <Seal src="GET /structure/pulls" refId={pulls.artifact_id}>
            ranges over {multiple(lo)}–{multiple(hi)} its slack
          </Seal>
          <ComputeNow key={rec.merchant_id} rec={rec} />
        </div>
      </div>
    </div>
  )
}

/** Exception E2: the bounded analysis, run again on request, labelled as such. */
function ComputeNow({ rec }: { rec: PullRecord }) {
  const [state, setState] = useState<{ running: boolean; slow?: boolean; data?: ScenarioSnapshot; error?: ApiError }>({
    running: false,
  })
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(timer.current), [])

  const run = () => {
    setState({ running: true })
    timer.current = window.setTimeout(() => setState((s) => (s.running ? { ...s, slow: true } : s)), 4000)
    api
      .analyze([rec.merchant_id], rec.magnitude_multiple, rec.onset_hours)
      .then((data) => setState({ running: false, data }))
      .catch((e: unknown) =>
        setState({ running: false, error: e instanceof ApiError ? e : new ApiError(String(e), null, '') }),
      )
      .finally(() => window.clearTimeout(timer.current))
  }

  return (
    <div className="pull__now">
      {state.data ? (
        <div className="pull__computed">
          <Seal src="POST /scenarios/analyze" refId={state.data.scenario_id}>
            {count(Math.round(state.data.computed_in_ms ?? 0))} ms
          </Seal>
          <span className="num">
            {count(state.data.projected_impact.n_affected)} constrained · {money(state.data.projected_impact.disrupted_value)} delayed ·
            cut {pct(state.data.counterfactual.disruption_reduction_pct)}
          </span>
        </div>
      ) : (
        <button type="button" className="pull__button" onClick={run} disabled={state.running}>
          {state.running ? (state.slow ? 'Still computing on this host…' : 'Computing…') : `Compute ${rec.merchant_id} now`}
        </button>
      )}
      {state.error ? <p className="pull__error">{state.error.message}</p> : null}
    </div>
  )
}
