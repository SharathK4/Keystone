/**
 * Card 03 — where capital works.
 *
 * A roster: every candidate the bounded search evaluated, set as a quiet column
 * of identifiers, with the one it selected sitting among them on a solid plate.
 * It says "several were considered and one was chosen" without printing a
 * rupee, a ratio or a percentage — those are the whole substance of the
 * expanded view and giving them away here would leave nothing to open.
 *
 * The identifiers are real. Where the chosen row sits in the column is a layout
 * decision, not a ranking: the alternatives arrive in candidate-generation
 * order, and putting the plate in the middle of them is what makes the picture
 * read as a choice out of a field rather than a header over a list.
 */

import type { ScenarioSnapshot } from '../api/types'
import { Panel } from './Panel'
import { TexturedTile } from './TexturedTile'
import { humanise } from '../lib/format'
import './intervention-panel.css'

interface Props {
  scenario: ScenarioSnapshot | null
  busy: boolean
  onOpen: () => void
}

export function InterventionPanel({ scenario, busy, onOpen }: Props) {
  const chosen = scenario?.recommended_intervention ?? null
  const others = (scenario?.alternatives ?? []).slice(0, 4)
  const split = Math.min(2, others.length)
  const above = others.slice(0, split)
  const below = others.slice(split)

  return (
    <Panel
      index={3}
      title="Where capital works"
      subtitle="Put one rupee where it holds the most payments together."
      reveals="Open the intervention analysis"
      onOpen={onOpen}
    >
      <div className={`ip${busy ? ' is-busy' : ''}`}>
        <TexturedTile tone="clay" seed={chosen?.merchant_id ?? 'capital'}>
          <div className="ip__roster">
            {above.map((o) => (
              <Candidate key={o.intervention_id} id={o.merchant_id} />
            ))}

            {chosen ? (
              <div className="ip__plate">
                <span className="ip__plate-dot" aria-hidden="true" />
                <span className="ip__plate-id num">{chosen.merchant_id}</span>
                <span className="ip__plate-type">{humanise(chosen.type)}</span>
                <span className="ip__plate-tag">selected</span>
              </div>
            ) : (
              <div className="ip__plate ip__plate--none">
                <span className="ip__plate-type">no feasible action here</span>
              </div>
            )}

            {below.map((o) => (
              <Candidate key={o.intervention_id} id={o.merchant_id} />
            ))}
          </div>
        </TexturedTile>
      </div>
    </Panel>
  )
}

/** One considered-and-set-aside candidate: an identifier and a spent rule. */
function Candidate({ id }: { id: string }) {
  return (
    <div className="ip__cand">
      <span className="ip__cand-id num">{id}</span>
      <span className="ip__cand-rule" aria-hidden="true" />
    </div>
  )
}
