/**
 * 04 expanded — the contract.
 *
 * Laid out as a product page rather than a report: a line of context, a
 * headline, the card in its own light with the action under it, and beside it
 * the three things that are true about this offer — each one a backend figure,
 * not a marketing claim.
 *
 * Everything on this screen is specific to one merchant. The card's surface is
 * seeded from their id, the amount is what the optimiser sized for them, the
 * rationale is the backend's own sentence about their book, and the eligibility
 * criteria are the booleans it actually evaluated.
 *
 * Nothing here implies a credit decision: the backend ships its own disclaimer
 * and it is shown verbatim. Money moves only through the settlement below the
 * card: the exact plan is reviewed, signed by a person, and paid in the PayPal
 * sandbox, and the ledger shows what PayPal said.
 */

import { useRef, useState } from 'react'
import type { ExecutionStatus, ScenarioSnapshot } from '../../api/types'
import { Settlement } from '../Settlement'
import { ExpandedAnalysis, XSection } from '../ExpandedAnalysis'
import { ContractCard } from '../ContractCard'
import { SpecularButton } from '../reactbits/SpecularButton'
import { count, hours, idx, inr, inrExact, pct } from '../../lib/format'
import { repaymentSummary, resolveOfferCopy } from '../../lib/offerCopy'
import './offer-expanded.css'

interface Props {
  scenario: ScenarioSnapshot | null
  execution: ExecutionStatus
  busy: boolean
  onClose: () => void
}

export function OfferExpanded({ scenario, execution, busy, onClose }: Props) {
  const offer = scenario?.offer ?? null
  const copy = offer ? resolveOfferCopy(offer.intervention_type, scenario?.family) : null
  const benefit = offer?.expected_network_benefit

  const [reviewing, setReviewing] = useState(false)
  const desk = useRef<HTMLDivElement>(null)
  const review = () => {
    setReviewing((v) => !v)
    if (!reviewing) {
      window.requestAnimationFrame(() => desk.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
    }
  }

  return (
    <ExpandedAnalysis
      title={copy ? copy.title : 'No offer for this scenario'}
      subtitle={
        copy
          ? copy.occasion
          : 'An offer is only written where the optimiser found a feasible action that measurably reduced disruption.'
      }
      bare={Boolean(offer)}
      onClose={onClose}
    >
      <div className={`ox${busy ? ' is-busy' : ''}`}>
        {offer && copy ? (
          <>
            <div className="ox__pitch">
              <p className="ox__kicker">
                Written for <b className="num">{offer.merchant_id}</b> against{' '}
                <b className="num">{inr(offer.eligibility.constraints.obligations_in_horizon)}</b>{' '}
                of obligations falling due
              </p>
              <h3 className="ox__headline">{copy.title}</h3>
              <p className="ox__sub">{copy.purpose}</p>
            </div>

            <div className="ox__product">
              <div className="ox__left">
                <ContractCard
                  merchantId={offer.merchant_id}
                  amount={offer.proposed_amount}
                  durationHours={offer.duration_hours}
                  terms={repaymentSummary(offer.repayment.structure, offer.repayment.n_instalments)}
                  status={offer.status}
                />

                <div className="ox__cta">
                  <SpecularButton onClick={review}>
                    {reviewing ? 'Close the settlement' : 'Review and approve'}
                  </SpecularButton>
                  <span className={`ox__cap${execution.configured ? ' is-live' : ''}`}>
                    <i aria-hidden="true" />
                    PayPal sandbox · nothing moves without a signature on this exact plan
                  </span>
                </div>
              </div>

              <ul className="ox__points">
                <Point title={`${inr(offer.proposed_amount)} for ${hours(offer.duration_hours)}`}>
                  {repaymentSummary(offer.repayment.structure, offer.repayment.n_instalments)}
                  {offer.repayment.n_instalments === 1
                    ? `, due at ${hours(offer.repayment.first_due_hours)}`
                    : `, ${offer.repayment.n_instalments} × ${inr(
                        offer.repayment.instalment_amount,
                      )}`}
                  . Sized against {inr(offer.eligibility.constraints.obligations_in_horizon)} of
                  obligations and a {inr(offer.eligibility.constraints.liquidity_buffer)} buffer.
                </Point>

                <Point
                  title={
                    offer.indicative_cost !== null
                      ? `${inr(offer.indicative_cost)} indicative cost`
                      : 'Indicative cost unavailable'
                  }
                >
                  {offer.indicative_rate_annual_pct !== null
                    ? `${pct(
                        offer.indicative_rate_annual_pct,
                      )} a year, computed from a declared assumption rather than quoted. Not a credit approval and not an underwriting decision.`
                    : 'No indicative rate was produced for this offer.'}
                </Point>

                <Point title={`${inr(benefit?.commerce_preserved)} kept on schedule`}>
                  Replaying this plan with the same random draws kept that much payment value on its original
                  dates, protected {count(benefit?.merchants_protected)} merchant
                  {(benefit?.merchants_protected ?? 0) === 1 ? '' : 's'} and reduced modelled
                  network disruption by {pct(benefit?.disruption_reduction_pct)}. Measured, not
                  predicted.
                </Point>

                <Point title={offer.eligibility.eligible ? 'Eligibility met' : 'Eligibility not met'}>
                  {Object.entries(offer.eligibility.criteria)
                    .map(([k, ok]) => `${k.replace(/_/g, ' ')} ${ok ? '✓' : '✗'}`)
                    .join(' · ')}
                  . Facility capped at {inr(offer.eligibility.constraints.max_amount)} over a
                  maximum {hours(offer.eligibility.constraints.max_duration_hours)} term.
                </Point>
              </ul>
            </div>

            {reviewing && scenario ? (
              <div ref={desk}>
                <XSection label="Approve and pay" note="the plan behind this offer, paid in the PayPal sandbox">
                  <Settlement scenarioId={scenario.scenario_id} />
                </XSection>
              </div>
            ) : null}

            {reviewing ? (
              <XSection label="The terms" note={offer.offer_id}>
                <dl className="ox__terms">
                  <Term k="Principal" v={inrExact(offer.proposed_amount)} />
                  <Term k="Currency" v={offer.currency} />
                  <Term k="Term" v={hours(offer.duration_hours)} />
                  <Term
                    k="Repayment"
                    v={repaymentSummary(offer.repayment.structure, offer.repayment.n_instalments)}
                  />
                  <Term k="First due" v={hours(offer.repayment.first_due_hours)} />
                  <Term
                    k="Instalment"
                    v={inrExact(offer.repayment.instalment_amount)}
                  />
                  <Term
                    k="Indicative cost"
                    v={offer.indicative_cost !== null ? inrExact(offer.indicative_cost) : '—'}
                  />
                  <Term
                    k="Indicative rate"
                    v={
                      offer.indicative_rate_annual_pct !== null
                        ? `${pct(offer.indicative_rate_annual_pct)} a year`
                        : '—'
                    }
                  />
                  <Term k="Status" v={offer.status} />
                  <Term
                    k="Disruption cut"
                    v={
                      benefit?.disruption_prevented !== undefined
                        ? `${idx(benefit.disruption_prevented)} index units`
                        : '—'
                    }
                  />
                </dl>
                <p className="ox__disclaimer">{offer.disclaimer}</p>
              </XSection>
            ) : null}


          </>
        ) : (
          <div className="ox__empty">
            <p className="ox__empty-body">
              For this scenario the bounded search found no action that both satisfied the
              constraints and reduced disruption, so there is nothing to propose.
            </p>
          </div>
        )}
      </div>
    </ExpandedAnalysis>
  )
}

function Point({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <li className="ox__point">
      <span className="ox__tick" aria-hidden="true">
        <svg viewBox="0 0 14 14">
          <path
            d="M2.5 7.4l3.2 3.2L11.6 3.9"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </span>
      <div>
        <h4 className="ox__point-title">{title}</h4>
        <p className="ox__point-body">{children}</p>
      </div>
    </li>
  )
}

function Term({ k, v }: { k: string; v: string }) {
  return (
    <div className="ox__term">
      <dt>{k}</dt>
      <dd className="num">{v}</dd>
    </div>
  )
}
