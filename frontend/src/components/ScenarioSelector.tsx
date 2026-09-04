/**
 * Scenario selection.
 *
 * A short row of named analytical states - not a timeline, not a slider, and
 * with nothing to press play on. Each option is a scenario the backend has
 * already analysed; choosing one requests that analysis. The distinction the
 * product depends on is that the user picks a *question*, not a moment in a
 * simulation.
 *
 * Scenarios the backend produced no recommendation for stay selectable and are
 * marked, because "the optimiser found nothing feasible here" is a real result
 * and hiding those options would make the panel look better than the system is.
 */

import type { ScenarioSummary } from '../api/types'
import { scenarioLabel } from '../lib/format'
import './scenario-selector.css'

interface Props {
  scenarios: ScenarioSummary[]
  selected: string | null
  busy: boolean
  onSelect: (id: string) => void
  /** Landing state: tighter, so it fits the panel head in two rows. */
  compact?: boolean
}

export function ScenarioSelector({ scenarios, selected, busy, onSelect, compact }: Props) {
  return (
    <div
      className={`scenarios${compact ? ' scenarios--compact' : ''}`}
      role="group"
      aria-label="Scenario"
    >
      {scenarios.map((s) => {
        const active = s.scenario_id === selected
        return (
          <button
            key={s.scenario_id}
            type="button"
            className={`scenarios__item${active ? ' is-active' : ''}`}
            aria-pressed={active}
            disabled={busy && !active}
            onClick={() => onSelect(s.scenario_id)}
            title={s.headline}
          >
            <span className="scenarios__label">{scenarioLabel(s.family)}</span>
            {s.recommended_action === null ? (
              <span className="scenarios__flag" title="No feasible intervention found">
                ·
              </span>
            ) : null}
          </button>
        )
      })}
    </div>
  )
}
