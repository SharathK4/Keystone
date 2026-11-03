/**
 * The hundred merchants as a quiet field of dots, in id order.
 *
 * Three states, never by colour alone: idle (small, pale), hit (large, filled)
 * and reached (ringed). Only the dots that changed can take focus, and each
 * names itself and its state, so the field has no legend and needs none.
 */

import type { MerchantView } from '../api/types'

export interface Mark {
  state: 'hit' | 'reached'
  /** Read on hover and focus: "m0059, short four days later, one step out". */
  label: string
}

interface Props {
  merchants: MerchantView[]
  marks: Map<string, Mark>
  summary: string
}

const COLS = 10
const STEP = 10

export function DotField({ merchants, marks, summary }: Props) {
  const ordered = [...merchants].sort((a, b) => a.merchant_id.localeCompare(b.merchant_id))
  const rows = Math.ceil(ordered.length / COLS)
  return (
    <svg
      className="q-field"
      viewBox={`0 0 ${COLS * STEP} ${rows * STEP}`}
      role="img"
      aria-label={summary}
    >
      {ordered.map((m, i) => {
        const cx = (i % COLS) * STEP + STEP / 2
        const cy = Math.floor(i / COLS) * STEP + STEP / 2
        const mark = marks.get(m.merchant_id)
        if (!mark) {
          return <circle key={m.merchant_id} className="q-dot" cx={cx} cy={cy} r={1.7} />
        }
        return (
          <g
            key={m.merchant_id}
            className={`q-dot-mark is-${mark.state}`}
            tabIndex={0}
            role="img"
            aria-label={mark.label}
          >
            <title>{mark.label}</title>
            <circle className="q-dot-hit" cx={cx} cy={cy} r={4.6} />
            <circle className="q-dot-shape" cx={cx} cy={cy} r={mark.state === 'hit' ? 3.3 : 2.9} />
          </g>
        )
      })}
    </svg>
  )
}
