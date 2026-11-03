/**
 * The cost of waiting: payments kept on time, if the same fix lands later.
 *
 * Two lines on one discrete axis - the plan as chosen, delayed, and the search
 * re-run for each delay - and the delays themselves are the selector. No
 * legend: each line is named where it ends, and the selected point names both
 * values on hover and focus.
 */

import type { WalkSpeedView } from '../api/schema'
import { money } from '../lib/format'
import { delayLabel } from './words'

interface Props {
  speed: WalkSpeedView
  selected: number
  onSelect: (delayHours: number) => void
}

const W = 360
const H = 190
const PAD = { top: 14, right: 74, bottom: 10, left: 8 }

export function WaitingChart({ speed, selected, onSelect }: Props) {
  const rows = speed.delays
  const chosen = rows.map((r) => r.as_chosen.payments_kept_on_time_inr)
  const replanned = rows.map((r) => r.replanned.payments_kept_on_time_inr)
  const top = Math.max(...chosen, ...replanned, 1)
  const x = (i: number) =>
    PAD.left + (rows.length === 1 ? 0 : (i * (W - PAD.left - PAD.right)) / (rows.length - 1))
  const y = (v: number) => PAD.top + (1 - v / top) * (H - PAD.top - PAD.bottom)
  const path = (values: number[]) =>
    values.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const at = Math.max(0, rows.findIndex((r) => r.delay_hours === selected))
  const last = rows.length - 1

  return (
    <div className="q-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={
        `Payments kept on time if the fix lands later: ${rows
          .map((r) => `${delayLabel(r.delay_hours)} ${money(r.as_chosen.payments_kept_on_time_inr)}`)
          .join(', ')}`
      }>
        <line className="q-chart-base" x1={PAD.left} x2={W - PAD.right} y1={y(0)} y2={y(0)} />
        <path className="q-line is-replanned" d={path(replanned)} />
        <path className="q-line is-chosen" d={path(chosen)} />
        <text className="q-line-name" x={x(last) + 8} y={y(chosen[last]) - 6}>as chosen</text>
        <text className="q-line-name" x={x(last) + 8} y={y(replanned[last]) + 12}>re-planned</text>
        <g className="q-chart-at">
          <line x1={x(at)} x2={x(at)} y1={PAD.top} y2={y(0)} />
          <circle cx={x(at)} cy={y(chosen[at])} r={4} />
          <text x={x(at) + (at === last ? -6 : 8)} y={y(chosen[at]) - 8}
                textAnchor={at === last ? 'end' : 'start'}>
            {money(chosen[at])}
          </text>
        </g>
      </svg>
      <div className="q-delays" role="radiogroup" aria-label="When the money lands">
        {rows.map((r, i) => (
          <button
            key={r.delay_hours}
            type="button"
            role="radio"
            aria-checked={i === at}
            className="q-delay"
            style={{ left: `${(x(i) / W) * 100}%` }}
            title={`${delayLabel(r.delay_hours)}: as chosen ${money(r.as_chosen.payments_kept_on_time_inr)}, re-planned ${money(r.replanned.payments_kept_on_time_inr)}`}
            onClick={() => onSelect(r.delay_hours)}
          >
            {delayLabel(r.delay_hours)}
          </button>
        ))}
      </div>
    </div>
  )
}
