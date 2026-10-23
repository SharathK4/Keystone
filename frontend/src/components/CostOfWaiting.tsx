/**
 * The cost of waiting.
 *
 * The same plan with the money landing later. Each stop on the rail is a
 * scenario parameter, not a production timing: the hours between the moment a
 * shock is known and the moment the settlement lands. Two lines: the plan as
 * chosen at the shock, delayed; and the plan the optimiser would choose knowing
 * it lands late. The hatched gap between them is what re-planning recovers.
 *
 * At long delays the cut that remains is mostly payments made less late rather
 * than payments made on time, so the two figures always sit side by side.
 */

import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { ApiError, api } from '../api/client'
import type { SpeedScenarioView, SpeedSummary } from '../api/schema'
import { count, hours, idx, inr, pct } from '../lib/format'
import { Seal } from './Seal'
import './cost-of-waiting.css'

interface Props {
  scenarioId: string
}

const W = 640
const H = 250
const PAD = { l: 44, r: 16, t: 18, b: 26 }

export function CostOfWaiting({ scenarioId }: Props) {
  const [summary, setSummary] = useState<SpeedSummary | null>(null)
  const [view, setView] = useState<SpeedScenarioView | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [di, setDi] = useState<number | null>(null)
  const stops = useRef<(HTMLButtonElement | null)[]>([])

  useEffect(() => {
    let live = true
    api
      .speed()
      .then((s) => live && setSummary(s))
      .catch((e: unknown) => live && setError(e instanceof ApiError ? e : null))
    return () => {
      live = false
    }
  }, [])

  const known = summary?.scenarios.find((s) => s.scenario_id === scenarioId) ?? null
  useEffect(() => {
    if (!known) return
    let live = true
    api
      .speedScenario(scenarioId)
      .then((v) => live && setView(v))
      .catch((e: unknown) => live && setError(e instanceof ApiError ? e : null))
    return () => {
      live = false
    }
  }, [known, scenarioId])

  if (error) {
    return (
      <p className="cw__missing">
        {error.status === 404
          ? 'The value-of-speed artifact has not been built on this server, so nothing is shown in its place.'
          : error.message}
      </p>
    )
  }
  if (!summary) return <p className="cw__missing">Reading the delay schedule…</p>
  if (!known) return <p className="cw__missing">This scenario is not part of the delay schedule.</p>
  if (!known.has_plan) {
    return <p className="cw__missing">There is no plan for this scenario, so there is nothing for a delay to cost.</p>
  }
  if (!view || view.scenario.scenario_id !== scenarioId) return <p className="cw__missing">Reading this scenario’s schedule…</p>

  const sc = view.scenario
  const delays = sc.delays.map((d) => d.delay_hours)
  const sel = di ?? Math.max(0, delays.indexOf(48))
  const d = sc.delays[sel]
  const fz = sc.delays.map((r) => r.frozen.disruption_cut_pct)
  const rp = sc.delays.map((r) => r.replanned.disruption_cut_pct)
  const top = Math.max(10, Math.ceil(Math.max(...fz, ...rp) / 10) * 10)
  const x = (i: number) => PAD.l + (i * (W - PAD.l - PAD.r)) / (delays.length - 1)
  const y = (v: number) => PAD.t + (H - PAD.t - PAD.b) * (1 - v / top)
  const path = (vs: number[]) => vs.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('')
  const gap = `${path(rp)}${fz
    .map((v, i) => [i, v] as const)
    .reverse()
    .map(([i, v]) => `L${x(i).toFixed(1)},${y(v).toFixed(1)}`)
    .join('')}Z`
  const ticks = Array.from({ length: top / 10 + 1 }, (_, i) => i * 10)

  const onKey = (e: KeyboardEvent) => {
    const step = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0
    if (!step) return
    e.preventDefault()
    const next = Math.min(delays.length - 1, Math.max(0, sel + step))
    setDi(next)
    stops.current[next]?.focus()
  }

  const r = d.frozen.reduction
  const parts = [
    { key: 'delay', label: 'Payments made less late', v: r.delay },
    { key: 'default', label: 'Defaults avoided', v: r.default },
    { key: 'deficit', label: 'Less time below the floor', v: r.deficit },
  ]
  const total = Math.max(1, parts.reduce((s, p) => s + Math.max(0, p.v), 0))
  const keptNothing = d.frozen.payments_kept_on_time_inr < 1 && d.frozen.disruption_cut_pct > 0
  const point = summary.points[sel]

  return (
    <div className="cw">
      <div className="cw__main">
        <div className="cw__rail" role="radiogroup" aria-label="Delay before the money lands" onKeyDown={onKey}>
          {delays.map((h, i) => (
            <button
              key={h}
              ref={(el) => {
                stops.current[i] = el
              }}
              type="button"
              role="radio"
              aria-checked={i === sel}
              tabIndex={i === sel ? 0 : -1}
              className={`cw__stop${i === sel ? ' is-on' : ''}`}
              style={{ left: `${(x(i) / W) * 100}%` }}
              onClick={() => setDi(i)}
            >
              {hours(h)}
            </button>
          ))}
        </div>

        <svg className="cw__chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Network disruption cut against delay, for the plan as chosen and the plan re-planned for the delay.">
          <defs>
            <pattern id="cw-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">
              <line x1="0" y1="0" x2="0" y2="6" className="cw__hatch" />
            </pattern>
          </defs>
          {ticks.map((t) => (
            <g key={t}>
              <line x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} className="cw__grid" />
              <text x={PAD.l - 10} y={y(t) + 4} textAnchor="end" className="cw__tick">
                {t}%
              </text>
            </g>
          ))}
          <line x1={x(sel)} x2={x(sel)} y1={PAD.t} y2={H - PAD.b} className="cw__now" />
          <path d={gap} fill="url(#cw-hatch)" />
          <path d={path(fz)} className="cw__frozen" />
          <path d={path(rp)} className="cw__replanned" />
          {fz.map((v, i) => (
            <circle key={`f${i}`} cx={x(i)} cy={y(v)} r={i === sel ? 5.5 : 3.5} className="cw__dot" />
          ))}
          {rp.map((v, i) => (
            <circle key={`r${i}`} cx={x(i)} cy={y(v)} r={i === sel ? 5 : 3} className="cw__ring" />
          ))}
        </svg>

        <p className="cw__legend">
          <span className="cw__key cw__key--frozen" aria-hidden="true" /> the plan as chosen at the shock
          <span className="cw__key cw__key--replanned" aria-hidden="true" /> re-planned for the delay
          <span className="cw__key cw__key--gap" aria-hidden="true" /> what re-planning recovers
        </p>
        <p className="cw__caveat">
          Delays are scenario parameters, not production timings. {sc.monotonicity.rises.length ? 'Where the cut rises with delay it is reported, not smoothed.' : ''}
        </p>
      </div>

      <aside className="cw__card" aria-live="polite">
        <p className="cw__when">
          Money landing <b className="num">{hours(d.delay_hours)}</b> after the shock
        </p>
        <dl className="cw__figs">
          <div>
            <dt>Disruption cut</dt>
            <dd className="num">{pct(d.frozen.disruption_cut_pct)}</dd>
            <span className="num">re-planned {pct(d.replanned.disruption_cut_pct)}</span>
          </div>
          <div>
            <dt>Kept on time</dt>
            <dd className={`num${keptNothing ? ' is-zero' : ''}`}>{inr(d.frozen.payments_kept_on_time_inr)}</dd>
            <span className="num">
              {count(d.frozen.merchants_protected)} protected · {count(d.frozen.defaults_avoided)} defaults avoided
            </span>
          </div>
        </dl>

        <p className="cw__sub">Where the cut comes from</p>
        <div className="cw__bar" aria-hidden="true">
          {parts.map((p) => (
            <span key={p.key} className={`cw__seg cw__seg--${p.key}`} style={{ width: `${(100 * Math.max(0, p.v)) / total}%` }} />
          ))}
        </div>
        <ul className="cw__parts">
          {parts.map((p) => (
            <li key={p.key}>
              <span className={`cw__swatch cw__seg--${p.key}`} aria-hidden="true" />
              {p.label}
              <span className="num">{idx(p.v)}</span>
            </li>
          ))}
        </ul>

        {keptNothing ? (
          <p className="cw__note">
            Nothing is kept on time here, yet the cut is real: every payment the money could have saved has already gone late,
            and the money still makes those payments less late.
          </p>
        ) : null}

        {point ? (
          <p className="cw__bench num">
            Across the {count(point.n_scenarios)} benchmark scenarios with a plan, the mean cut at {hours(point.delay_hours)} is{' '}
            {pct(point.frozen_cut_pct.mean)} [{pct(point.frozen_cut_pct.low)}, {pct(point.frozen_cut_pct.high)}].
          </p>
        ) : null}
        <Seal src={`GET /structure/speed/scenarios/${scenarioId}`} refId={view.artifact_id}>
          {count(summary.n_scenarios)} scenarios replayed
        </Seal>
      </aside>
    </div>
  )
}
