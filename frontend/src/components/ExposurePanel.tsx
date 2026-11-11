/**
 * Card 02 — what breaks.
 *
 * The dot matrix alone: one dot per merchant, the merchant that misses its
 * payment and the ones it reaches lit, the rest by how well they can cover
 * themselves. The shape of the answer, with no words or figures on the tile.
 */

import type { MerchantView, ScenarioSnapshot, ScenarioSummary } from '../api/types'
import { Panel } from './Panel'
import { ExposureDotMatrix } from './ExposureDotMatrix'
import { TexturedTile } from './TexturedTile'
import './exposure-panel.css'

interface Props {
  merchants: MerchantView[]
  scenario: ScenarioSnapshot | null
  scenarios: ScenarioSummary[]
  busy: boolean
  onOpen: () => void
}

export function ExposurePanel({ merchants, scenario, busy, onOpen }: Props) {
  return (
    <Panel
      index={2}
      title="What breaks"
      subtitle="One missed payment, and who else it reaches."
      reveals="See who it reaches"
      onOpen={onOpen}
    >
      <div className={`xp${busy ? ' is-busy' : ''}`}>
        <TexturedTile tone="slate" seed={scenario?.scenario_id ?? 'exposure'}>
          <div className="xp__inner xp__inner--bare">
            <ExposureDotMatrix
              merchants={merchants}
              affected={scenario?.affected_merchants ?? []}
              origins={scenario?.shock.origin_merchants ?? []}
              compact
              onDark
              bare
            />
          </div>
        </TexturedTile>
      </div>
    </Panel>
  )
}
