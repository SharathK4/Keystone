/**
 * Card 04 — the offer.
 *
 * The contract card itself, small, sitting in its own light. It is the only
 * saturated colour in the composition, which is what makes the fourth card read
 * as the thing the other three were building toward.
 *
 * The amount is on the card because an offer with no amount is not an offer —
 * but nothing else is: no cost, no eligibility, no benefit. Those are the
 * contract, and the contract is one click away.
 */

import type { ExecutionStatus, ScenarioSnapshot } from '../api/types'
import { Panel } from './Panel'
import { RazorpayContractCard } from './RazorpayContractCard'
import { repaymentSummary, resolveOfferCopy } from '../lib/offerCopy'
import './offer-panel.css'

interface Props {
  scenario: ScenarioSnapshot | null
  execution: ExecutionStatus
  busy: boolean
  onOpen: () => void
}

export function OfferPanel({ scenario, execution, busy, onOpen }: Props) {
  const offer = scenario?.offer ?? null
  const copy = offer ? resolveOfferCopy(offer.intervention_type, scenario?.family) : null

  return (
    <Panel
      index={4}
      title="The offer"
      subtitle={
        copy ? copy.subtitle : 'A liquidity offer written around one merchant’s payment cycle.'
      }
      reveals={offer ? 'Open the contract' : 'See why no offer was written'}
      onOpen={onOpen}
    >
      <div className={`op${busy ? ' is-busy' : ''}`}>
        {offer ? (
          <RazorpayContractCard
            merchantId={offer.merchant_id}
            amount={offer.proposed_amount}
            durationHours={offer.duration_hours}
            terms={repaymentSummary(offer.repayment.structure, offer.repayment.n_instalments)}
            compact
            fill
          />
        ) : (
          <div className="op__empty">
            <p className="op__empty-title">Nothing to offer here.</p>
            <p className="op__empty-body">
              An offer is only written where the search found an action that measurably held the
              chain together. For this scenario it did not.
            </p>
            <span className={`op__mode${execution.api_reachable ? ' is-live' : ''}`}>
              {execution.provider.replace('_', ' ')} · {execution.mode}
            </span>
          </div>
        )}
      </div>
    </Panel>
  )
}
