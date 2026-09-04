/**
 * The 2x2 composition.
 *
 * Its only job is to keep the four panels geometrically identical and to hand
 * each one the slice of state it needs. Everything about how a panel looks
 * lives in the panel; everything about how they sit together lives here.
 */

import type { ScenarioSnapshot } from '../api/types'
import type { Structure } from '../api/useKeystoneData'
import type { PanelId } from './ExpandedAnalysis'
import { NetworkPanel } from './NetworkPanel'
import { ExposurePanel } from './ExposurePanel'
import { InterventionPanel } from './InterventionPanel'
import { OfferPanel } from './OfferPanel'

interface Props {
  structure: Structure
  scenario: ScenarioSnapshot | null
  scenarioBusy: boolean
  onOpen: (id: PanelId) => void
}

export function FourPanelGrid({ structure, scenario, scenarioBusy, onOpen }: Props) {
  return (
    <div className="ks__grid">
      <NetworkPanel
        merchants={structure.merchants}
        dependencies={structure.dependencies}
        systemic={structure.systemic}
        onOpen={() => onOpen('network')}
      />

      <ExposurePanel
        merchants={structure.merchants}
        scenario={scenario}
        scenarios={structure.scenarios}
        busy={scenarioBusy}
        onOpen={() => onOpen('exposure')}
      />

      <InterventionPanel
        scenario={scenario}
        busy={scenarioBusy}
        onOpen={() => onOpen('intervention')}
      />

      <OfferPanel
        scenario={scenario}
        execution={structure.execution}
        busy={scenarioBusy}
        onOpen={() => onOpen('offer')}
      />
    </div>
  )
}
