/**
 * Card 01 — the network.
 *
 * The chord figure and one handwritten note naming the merchant the ranking
 * puts first. No counts, no metrics: the shape is the argument at this layer,
 * and the numbers are one click away.
 *
 * The note is handed to the figure rather than positioned over it, so its
 * leader is drawn in the same user units as the node and always reaches it.
 */

import { useMemo } from 'react'
import type { DependencyView, MerchantView, SystemicRankingView } from '../api/types'
import { Panel } from './Panel'
import { NetworkVisualization } from './NetworkVisualization'
import { TexturedTile } from './TexturedTile'
import { layoutNetwork } from '../lib/networkLayout'
import './network-panel.css'

interface Props {
  merchants: MerchantView[]
  dependencies: DependencyView[]
  systemic: SystemicRankingView
  onOpen: () => void
}

export function NetworkPanel({ merchants, dependencies, systemic, onOpen }: Props) {
  // The miniature draws half the overlay: enough to show the structure is
  // bundled, few enough lines to read at this size.
  const layout = useMemo(
    () => layoutNetwork(merchants, dependencies, { size: 560, maxEdges: 110 }),
    [merchants, dependencies],
  )

  const leader = systemic.entries[0]

  return (
    <Panel
      index={1}
      title="The network"
      subtitle="See which merchants the rest of the chain is quietly leaning on."
      reveals="Open the dependency structure"
      onOpen={onOpen}
    >
      <figure className="np">
        <TexturedTile
          tone="ink"
          seed={leader?.merchant_id ?? 'network'}
          figure={
            <NetworkVisualization
              layout={layout}
              compact
              onDark
              ringed={leader?.merchant_id ?? null}
              ringNote={leader ? `load-bearing — ${leader.merchant_id}` : undefined}
            />
          }
        />


      </figure>
    </Panel>
  )
}
