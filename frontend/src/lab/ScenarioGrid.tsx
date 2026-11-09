/**
 * One rule on every test scenario: failures down, budgets across.
 *
 * Each cell is a small bullet chart. The bar is the share of the week's damage
 * the rule prevents there; the tick is Keystone's search at the same money. A
 * cell lights up the moment its replay lands, so a rule under test fills in
 * while you watch. Choosing a cell shows whom the rule funded.
 */

import { money } from '../lib/format'
import type { Scenario } from './api'

interface Props {
  scenarios: Scenario[]
  budgets: number[]
  values: Record<string, number>
  search: Record<string, { pct: number }>
  tone: 'ember' | 'silver' | 'white' | 'sand'
  active: string | null
  onPick: (key: string) => void
  scale?: number
}

export function ScenarioGrid({ scenarios, budgets, values, search, tone, active, onPick, scale = 50 }: Props) {
  const failures = [...new Map(scenarios.map((s) => [s.failing, s])).values()]
  return (
    <div className={`sg sg--${tone}`} style={{ gridTemplateColumns: `minmax(92px, 1.1fr) repeat(${budgets.length}, minmax(0, 1fr))` }}>
      <span className="sg__corner">fund of</span>
      {budgets.map((b) => (
        <span key={b} className="sg__budget num">{money(b)}</span>
      ))}
      {failures.map((f) => (
        <div key={f.failing} className="sg__row">
          <span className="sg__fail">
            <b>{f.failing}</b>
            <i className="num">misses {money(f.missed)}</i>
          </span>
          {budgets.map((b) => {
            const key = `${f.failing}@${b.toFixed(0)}`
            const v = values[key]
            const s = search[key]?.pct
            const known = v !== undefined
            const beats = known && s !== undefined && v > s + 0.05
            const loses = known && s !== undefined && v < s - 0.05
            return (
              <button
                key={key}
                type="button"
                className={`sg__cell${known ? ' is-known' : ''}${beats ? ' is-beats' : ''}${loses ? ' is-loses' : ''}${active === key ? ' is-active' : ''}`}
                onClick={() => onPick(key)}
                style={{ ['--v' as string]: known ? Math.min(1, v / scale) : 0 }}
                aria-label={`${f.failing}, fund of ${money(b)}: ${known ? `${v.toFixed(1)}%` : 'not yet tested'}`}
              >
                <span className="sg__val num">{known ? v.toFixed(1) : '·'}</span>
                <span className="sg__track" aria-hidden="true">
                  <span className="sg__bar" />
                  {s !== undefined ? <span className="sg__tick" style={{ left: `${Math.min(100, (s / scale) * 100)}%` }} /> : null}
                </span>
              </button>
            )
          })}
        </div>
      ))}
    </div>
  )
}
