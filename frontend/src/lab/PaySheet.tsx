/**
 * Pay the AI's rescue.
 *
 * The rule has already decided whom to fund; this sheet shows that plan beside
 * the search's, then the same PayPal approval desk as every plan - priced at the
 * declared demo scale, signed by a person, paid at most once.
 */

import { Settlement } from '../components/Settlement'
import { money } from '../lib/format'
import type { Clue, Tree, UseResult } from './api'
import { Formula } from './Formula'

interface Props {
  result: UseResult
  tree: Tree
  clues: Clue[]
  onClose: () => void
}

export function PaySheet({ result, tree, clues, onClose }: Props) {
  return (
    <div className="ps" role="dialog" aria-modal="true" aria-label="Pay this rescue with PayPal">
      <button className="ps__scrim" onClick={onClose} aria-label="Close" />
      <div className="ps__sheet">
        <header className="ps__head">
          <div>
            <p className="ps__eyebrow">Pay with PayPal</p>
            <h2>The AI’s rescue</h2>
          </div>
          <button className="ps__close" onClick={onClose} aria-label="Close">
            <span aria-hidden="true" />
          </button>
        </header>

        <div className="ps__rule">
          <Formula tree={tree} clues={clues} size="md" />
        </div>

        <p className="ps__lede">
          {result.failing} misses {money(result.missed)}; the fund is {money(result.budget)}. The rule funds:
        </p>
        <ul className="ps__picks">
          {result.picks.map((p) => (
            <li key={p.merchant_id}>
              <b>{p.merchant_id}</b>
              <span className="num">{money(p.amount)}</span>
            </li>
          ))}
        </ul>
        <p className="ps__vs num">
          Prevents {result.pct.toFixed(1)}% of the week’s damage. Keystone’s search, with the same money, prevents{' '}
          {result.search.pct.toFixed(1)}%.
        </p>

        <div className="ps__desk">
          <Settlement scenarioId={result.plan_ref} />
        </div>
      </div>
    </div>
  )
}
