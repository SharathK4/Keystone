/**
 * Card 02 — what breaks.
 *
 * The dot matrix and the scenario's own words. The blocks are sized by their
 * share, so the shape of the answer is visible before any figure is — and no
 * figure appears at this layer.
 */

import type { MerchantView, ScenarioSnapshot, ScenarioSummary } from '../api/types'
import { Panel } from './Panel'
import { ExposureDotMatrix } from './ExposureDotMatrix'
import { TexturedTile } from './TexturedTile'
import { scenarioLabel } from '../lib/format'
import './exposure-panel.css'

interface Props {
  merchants: MerchantView[]
  scenario: ScenarioSnapshot | null
  scenarios: ScenarioSummary[]
  busy: boolean
  onOpen: () => void
}

export function ExposurePanel({ merchants, scenario, scenarios, busy, onOpen }: Props) {
  return (
    <Panel
      index={2}
      title="What breaks"
      subtitle="Follow one late payment through the chain and see who it reaches."
      reveals="Open the scenario analysis"
      onOpen={onOpen}
    >
      <div className={`xp${busy ? ' is-busy' : ''}`}>
        <TexturedTile tone="slate" seed={scenario?.scenario_id ?? 'exposure'}>
          <div className="xp__inner">
            <span className="xp__eyebrow">
              {scenario ? scenarioLabel(scenario.family) : 'Scenario'}
            </span>

            <ExposureDotMatrix
              merchants={merchants}
              affected={scenario?.affected_merchants ?? []}
              origins={scenario?.shock.origin_merchants ?? []}
              compact
              onDark
            />

            <span className="xp__more">
              +{Math.max(scenarios.length - 1, 0)} more scenarios inside
            </span>
          </div>
        </TexturedTile>
      </div>
    </Panel>
  )
}
