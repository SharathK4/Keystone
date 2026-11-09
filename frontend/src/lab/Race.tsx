/**
 * The race: every rule, people's and the AI's, ranked by the share of the
 * week's damage it prevents.
 *
 * Each rule is one line - the formula, typeset, and a thin bar for its score.
 * A rule the AI has just written enters at the foot, its bar growing as each
 * scenario's replay lands, then slides to its place. The dashed line is
 * Keystone's own search at the same money; a bar that crosses it is a rule that
 * beats the search.
 */

import type { Clue } from './api'
import { Formula } from './Formula'
import { runningMean, type LiveRule } from './useLab'

interface Props {
  rules: LiveRule[]
  searchMean: number | null
  clues: Clue[]
  bestId: string | null
  /** The AI is writing: its next rules are drawn as faint strokes at the foot. */
  writing: boolean
}

const ROW = 64
const SHOWN = 9

export function Race({ rules, searchMean, clues, bestId, writing }: Props) {
  const scored = rules
    .filter((r) => r.status === 'scored')
    .sort((a, b) => b.mean - a.mean || a.size - b.size)
  const moving = rules.filter((r) => r.status !== 'scored')
  const ghosts = writing && moving.length === 0 ? 3 : 0
  const visible = [...scored.slice(0, Math.max(SHOWN - moving.length - ghosts, 4)), ...moving]
  const top = Math.max(30, searchMean ?? 0, ...rules.map((r) => runningMean(r) ?? 0))
  const scale = Math.ceil((top + 4) / 10) * 10
  const at = (v: number) => `${Math.min(100, (v / scale) * 100)}%`

  return (
    <div className="race">
      <div className="race__head">
        <span className="race__legend">
          <i className="is-people" /> written by people
          <i className="is-ai" /> written by the AI
        </span>
        <span className="race__axis">share of the week’s damage prevented</span>
      </div>

      <div className="race__rows" style={{ height: Math.max(visible.length + ghosts, 4) * ROW }}>
        {searchMean !== null ? (
          <div className="race__search" style={{ left: `calc(var(--track-left) + (100% - var(--track-left) - var(--value-w)) * ${searchMean / scale})` }}>
            <span>Keystone’s search · {searchMean.toFixed(1)}%</span>
          </div>
        ) : null}

        {visible.map((rule, i) => {
          const value = runningMean(rule)
          const machine = rule.origin === 'model' || rule.origin === 'mutation'
          const beats = value !== null && searchMean !== null && value > searchMean + 0.05
          return (
            <div
              key={rule.id}
              className={[
                'race__row',
                machine ? 'is-ai' : 'is-people',
                `is-${rule.status}`,
                rule.id === bestId ? 'is-best' : '',
                beats ? 'is-beats' : '',
              ].join(' ')}
              style={{ transform: `translateY(${i * ROW}px)` }}
            >
              <span className="race__rank num">{rule.status === 'scored' ? String(i + 1).padStart(2, '0') : '··'}</span>
              <i className="race__mark" aria-hidden="true" />
              <span className="race__formula">
                {rule.status === 'proposed' ? (
                  <code className="race__typing">{rule.formula}</code>
                ) : (
                  <Formula tree={rule.tree} clues={clues} size="sm" />
                )}
              </span>
              <span className="race__track">
                <span className="race__bar" style={{ width: value === null ? 0 : at(value) }} />
                <span className="race__dot" style={{ left: value === null ? 0 : at(value) }} />
              </span>
              <span className="race__value num">{value === null ? '' : `${value.toFixed(1)}%`}</span>
            </div>
          )
        })}

        {Array.from({ length: ghosts }, (_, g) => (
          <div key={`ghost${g}`} className="race__row race__ghost is-ai" style={{ transform: `translateY(${(visible.length + g) * ROW}px)`, animationDelay: `${g * 160}ms` }}>
            <span className="race__rank num">··</span>
            <i className="race__mark" aria-hidden="true" />
            <span className="race__stroke" style={{ animationDelay: `${g * 260}ms` }} />
            <span className="race__track" />
            <span className="race__value" />
          </div>
        ))}
      </div>
    </div>
  )
}
