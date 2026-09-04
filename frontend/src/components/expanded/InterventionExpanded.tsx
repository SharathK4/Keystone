/**
 * 03 expanded — where capital works.
 *
 * The comparison this view exists to make is a money comparison: capital
 * committed, commerce protected, and the ratio between them. All three come
 * from the replayed counterfactual.
 *
 * A note on units, because it is the one place this product could quietly lie.
 * The backend's objective D(G,S) sums three deliberately different terms — a
 * value-weighted delay term in INR-days, a default count, and a liquidity
 * deficit-time term in INR-hours — reconciled by configured weights. So
 * `disruption_prevented` and `capital_efficiency` are *index* quantities, not
 * rupees, and printing them with a rupee sign would claim a return the model
 * never measured. They appear here as the ranking score they are. The rupee
 * story is `commerce_preserved`: payment value that stayed on its original
 * dates when the action was replayed.
 *
 * The plan is also not always one action, and its actions are not all the same
 * shape. `counterfactual.cost` is the whole plan's committed capital; the
 * recommendation is one action inside it, and where the contract is written for
 * a different merchant that is another action of the same plan, listed rather
 * than hidden. Note that an action's `amount` and its `cost` are different
 * numbers: a supplier term extension can be worth lakhs while committing almost
 * nothing, because it moves dates rather than money. Both columns are shown, so
 * the rows never look like they should sum to the plan total when they do not.
 */

import { useState, type ReactNode } from 'react'
import type {
  ExecutionStatus,
  InterventionOption,
  MerchantView,
  ScenarioSnapshot,
  SystemicRankingView,
} from '../../api/types'
import { ExpandedAnalysis, XSection } from '../ExpandedAnalysis'
import { count, hours, humanise, idx, inr, inrExact, multiple, pct, ratio } from '../../lib/format'
import './intervention-expanded.css'

interface Props {
  scenario: ScenarioSnapshot | null
  merchants: MerchantView[]
  systemic: SystemicRankingView
  execution: ExecutionStatus
  busy: boolean
  onClose: () => void
}

export function InterventionExpanded({
  scenario,
  merchants,
  systemic,
  execution,
  busy,
  onClose,
}: Props) {
  const recommended = scenario?.recommended_intervention ?? null
  const cf = scenario?.counterfactual ?? null
  const offer = scenario?.offer ?? null

  const candidates: InterventionOption[] = scenario
    ? [...(recommended ? [recommended] : []), ...scenario.alternatives]
    : []

  // The inspection is stored with the scenario it belongs to rather than reset
  // by an effect, so switching scenario falls back to that scenario's own
  // recommendation without a second render pass.
  const [inspect, setInspect] = useState<{ scenario: string; id: string } | null>(null)
  const inspectedId =
    inspect && inspect.scenario === scenario?.scenario_id
      ? inspect.id
      : (recommended?.intervention_id ?? null)
  const setInspectedId = (id: string) => {
    if (scenario) setInspect({ scenario: scenario.scenario_id, id })
  }

  const inspected = candidates.find((c) => c.intervention_id === inspectedId) ?? recommended

  // The contract is written for the plan's first action. When that is a
  // different merchant from the recommendation on show, the plan has more than
  // one action, and both belong on screen.
  const secondAction =
    offer && recommended && offer.merchant_id !== recommended.merchant_id ? offer : null

  const leverage = cf && cf.cost > 0 ? cf.commerce_preserved / cf.cost : null
  const evidenceFor = inspected?.merchant_id ?? offer?.merchant_id ?? null
  const subject = evidenceFor ? merchants.find((m) => m.merchant_id === evidenceFor) : undefined
  const probe = evidenceFor
    ? systemic.entries.find((e) => e.merchant_id === evidenceFor)
    : undefined

  return (
    <ExpandedAnalysis
      title="Where one rupee does the most work"
      subtitle="Candidates are generated from measurable network properties, then replayed against the simulator. The model proposes; the simulator scores. Protected value is measured, never predicted."
      onClose={onClose}
    >
      <div className={`ix${busy ? ' is-busy' : ''}`}>
        {scenario && recommended && cf ? (
          <>
            {/* ---------------------------------------- the money comparison */}
            <div className="ix__ledger">
              <Cell
                label="Capital committed"
                value={inr(cf.cost)}
                note="placed as a facility and repaid at term, not spent"
              />
              <Arrow />
              <Cell
                label="Commerce protected"
                value={inr(cf.commerce_preserved)}
                note="payment value that stayed on its original dates"
                accent
              />
              <Arrow />
              <Cell
                label="Rupee leverage"
                value={leverage === null ? '—' : ratio(leverage)}
                note="protected per rupee placed"
                lead
              />
            </div>

            {cf.commerce_preserved <= 0 ? (
              <p className="ix__caveat">
                No payment value moved out of the delayed set in this replay. The plan still
                reduced the objective by {pct(cf.disruption_reduction_pct)} — it shortened how late
                payments were, rather than making them on time.
              </p>
            ) : null}

            <div className="ix__facts">
              <Fact label="Network disruption removed" value={pct(cf.disruption_reduction_pct)} />
              <Fact label="Merchants protected" value={count(cf.merchants_protected)} />
              <Fact
                label="Optimality gap"
                value={cf.optimality_gap === null ? 'not reported' : pct(cf.optimality_gap * 100)}
                note="against the best plan the search found"
              />
              <Fact
                label="Constraints"
                value={recommended.feasible ? 'satisfied' : 'violated'}
                note={
                  recommended.feasible
                    ? 'amount within buffer and horizon'
                    : recommended.constraint_violations.join(', ')
                }
              />
            </div>

            {/* --------------------------------------------------- the plan */}
            <XSection
              label="Actions in the selected plan"
              note={`${inr(cf.cost)} of capital committed across the plan`}
            >
              <table className="ix__table ix__table--plan">
                <thead>
                  <tr>
                    <th scope="col">Merchant</th>
                    <th scope="col">Action</th>
                    <th scope="col" className="is-num">
                      Size
                    </th>
                    <th scope="col" className="is-num">
                      Capital committed
                    </th>
                    <th scope="col" className="is-num">
                      Applied at
                    </th>
                    <th scope="col" className="is-num">
                      Held for
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <tr className="is-lead">
                    <td className="num">{recommended.merchant_id}</td>
                    <td>{humanise(recommended.type)}</td>
                    <td className="num is-num">{inr(recommended.amount)}</td>
                    <td className="num is-num">{inr(recommended.cost)}</td>
                    <td className="num is-num">{hours(recommended.apply_at_hours)}</td>
                    <td className="num is-num">{hours(recommended.duration_hours)}</td>
                  </tr>
                  {secondAction ? (
                    <tr className="is-lead">
                      <td className="num">{secondAction.merchant_id}</td>
                      <td>
                        {humanise(secondAction.intervention_type)}
                        <em className="ix__tag">contract written here</em>
                      </td>
                      <td className="num is-num">{inr(secondAction.proposed_amount)}</td>
                      <td className="num is-num">—</td>
                      <td className="num is-num">—</td>
                      <td className="num is-num">{hours(secondAction.duration_hours)}</td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
              <p className="ix__foot">
                Size is how large the action is; capital committed is what it consumes against the
                plan&apos;s budget, and the two differ where an action moves dates rather than
                money. The contract on the fourth card is written for one of these actions — the
                row it is written against is marked.
              </p>
            </XSection>

            {/* --------------------------------------------- the candidates */}
            <XSection
              label="Every candidate the search evaluated"
              note={`${count(candidates.length)} generated · ${count(
                candidates.filter((c) => c.disruption_prevented > 0).length,
              )} moved the objective`}
            >
              <table className="ix__table">
                <thead>
                  <tr>
                    <th scope="col">Merchant</th>
                    <th scope="col" className="is-num">
                      Capital required
                    </th>
                    <th scope="col" className="is-num">
                      Downstream obligations
                    </th>
                    <th scope="col" className="is-num">
                      Objective moved
                    </th>
                    <th scope="col">Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {candidates.map((c) => {
                    const moved = c.disruption_prevented > 0
                    const on = c.intervention_id === inspected?.intervention_id
                    return (
                      <tr
                        key={c.intervention_id}
                        className={`${on ? 'is-on ' : ''}${moved ? 'is-lead' : ''}`}
                        tabIndex={0}
                        onClick={() => setInspectedId(c.intervention_id)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault()
                            setInspectedId(c.intervention_id)
                          }
                        }}
                      >
                        <td className="num">{c.merchant_id}</td>
                        <td className="num is-num">{inr(c.liquidity_required)}</td>
                        <td className="num is-num">{inr(c.predicted_downstream_disruption)}</td>
                        <td className="num is-num">{moved ? idx(c.disruption_prevented) : '—'}</td>
                        <td>
                          {c.selected ? (
                            <span className="ix__badge is-on">replayed · selected</span>
                          ) : (
                            <span className="ix__badge">scored, not selected</span>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>

              <p className="ix__foot">
                Only the selected plan is replayed end to end, so only it carries a measured
                objective movement. The rest were generated and scored against the same network,
                then set aside — the column is empty because nothing was measured there, not
                because a measurement came back zero.
              </p>
            </XSection>

            {/* ----------------------------------------------- the evidence */}
            {subject ? (
              <XSection
                label="Why this merchant"
                note={`${subject.merchant_id} · ${subject.tier} · ${subject.sector}`}
              >
                <div className="ix__evidence">
                  <Group title="Network">
                    <E
                      k="Downstream dependencies"
                      v={count(subject.out_degree)}
                      s="merchants paid by this one"
                    />
                    <E
                      k="Inbound dependencies"
                      v={count(subject.in_degree)}
                      s="merchants paying into it"
                    />
                    <E
                      k="Systemic importance"
                      v={
                        subject.systemic_importance !== null
                          ? subject.systemic_importance.toFixed(3)
                          : 'not sampled'
                      }
                      s={
                        subject.systemic_rank !== null
                          ? `rank ${subject.systemic_rank} of ${count(systemic.n_sampled)} probed`
                          : 'outside the sampled set'
                      }
                    />
                    <E
                      k="Downstream merchants reached"
                      v={probe ? count(probe.downstream_affected) : '—'}
                      s="under a standardised shock at this merchant"
                    />
                    <E
                      k="Downstream value delayed"
                      v={probe ? inr(probe.downstream_delayed_value) : '—'}
                      s="in that same probe"
                    />
                  </Group>

                  <Group title="Liquidity">
                    <E
                      k="Cover ratio"
                      v={
                        subject.cover_ratio !== null ? multiple(subject.cover_ratio) : 'no payables'
                      }
                      s={subject.vulnerable ? 'owes more than it can cover' : 'clears its own book'}
                      tone={subject.vulnerable ? 'accent' : undefined}
                    />
                    <E k="Obligations in horizon" v={inr(subject.payables_in_horizon)} />
                    <E k="Receivables in horizon" v={inr(subject.receivables_in_horizon)} />
                    <E
                      k="Liquidity buffer"
                      v={inr(subject.liquidity_buffer)}
                      s="opening balance plus credit line"
                    />
                    <E
                      k="Net position"
                      v={inr(subject.net_position)}
                      s="receivables less payables"
                    />
                  </Group>

                  <Group title="Contagion">
                    <E
                      k="Cascade depth"
                      v={probe ? count(probe.cascade_depth) : '—'}
                      s="hops reached from this merchant"
                    />
                    <E
                      k="Time to first impact"
                      v={probe ? hours(probe.time_to_impact_hours) : '—'}
                      s="from shock onset"
                    />
                    <E
                      k="Exposure scores"
                      v={scenario.confidence.calibrated ? 'calibrated' : 'uncalibrated'}
                      s={`${scenario.confidence.source} — used to rank, not read as probabilities`}
                    />
                    <E
                      k="Stable under uncertainty"
                      v={
                        scenario.confidence.recommendation_stable_under_uncertainty === null
                          ? 'not tested'
                          : scenario.confidence.recommendation_stable_under_uncertainty
                            ? 'yes'
                            : 'no'
                      }
                      s={`${count(scenario.confidence.n_scenarios_considered)} perturbed worlds`}
                    />
                  </Group>

                  <Group title="Intervention">
                    <E
                      k="Amount"
                      v={inspected ? inr(inspected.amount) : inr(offer?.proposed_amount)}
                      s={inspected ? inrExact(inspected.amount) : undefined}
                    />
                    <E
                      k="Commerce protected"
                      v={inr(cf.commerce_preserved)}
                      s="measured by replay"
                    />
                    <E
                      k="Objective movement"
                      v={idx(cf.disruption_prevented)}
                      s="index units, not rupees"
                    />
                    <E
                      k="Model ranking score"
                      v={cf.capital_efficiency === null ? '—' : cf.capital_efficiency.toFixed(1)}
                      s="objective units moved per rupee — what the search ranks on"
                    />
                    <E
                      k="Regret"
                      v={cf.regret === null ? 'not reported' : idx(cf.regret)}
                      s="objective left on the table against the best plan found"
                    />
                  </Group>
                </div>
              </XSection>
            ) : null}

            {/* ------------------------------------------------- the method */}
            <XSection label="How this was measured" note={scenario.provenance.optimizer}>
              <p className="ix__method">
                Candidates are generated from observable network properties, then each plan is
                replayed in the liquidity simulator against the same shock. What is reported is the
                difference between the two runs, not a forecast. The objective sums a
                value-weighted delay term, a default count and a liquidity deficit-time term, so
                its units are an index rather than currency — which is why the money figures above
                come from payment value, and the index figures are labelled as index.
                {execution.mode === 'test' ? ' Razorpay Test Mode; no funds move.' : ''}
              </p>
              <dl className="ix__prov">
                <ProvRow k="Run" v={scenario.provenance.run_id} />
                <ProvRow
                  k="Simulator config"
                  v={scenario.provenance.simulator_config_hash.slice(0, 16)}
                />
                <ProvRow k="Seed" v={String(scenario.provenance.seed)} />
                <ProvRow
                  k="Computed in"
                  v={
                    scenario.computed_in_ms !== null
                      ? `${(scenario.computed_in_ms / 1000).toFixed(1)}s`
                      : '—'
                  }
                />
              </dl>
            </XSection>
          </>
        ) : (
          <div className="ix__empty">
            <p className="ix__empty-title">No feasible intervention for this scenario.</p>
            <p className="ix__empty-body">
              The bounded search generated {count(candidates.length)} candidates and replayed them
              against the simulator. None both satisfied the constraints and reduced disruption, so
              the backend returns no recommendation rather than the least-bad one.
            </p>
            {candidates.length > 0 ? (
              <table className="ix__table ix__table--muted">
                <thead>
                  <tr>
                    <th scope="col">Merchant</th>
                    <th scope="col" className="is-num">
                      Capital required
                    </th>
                    <th scope="col" className="is-num">
                      Downstream obligations
                    </th>
                    <th scope="col">Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {candidates.map((c) => (
                    <tr key={c.intervention_id}>
                      <td className="num">{c.merchant_id}</td>
                      <td className="num is-num">{inr(c.liquidity_required)}</td>
                      <td className="num is-num">{inr(c.predicted_downstream_disruption)}</td>
                      <td>
                        <span className="ix__badge">did not reduce disruption</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : null}
          </div>
        )}
      </div>
    </ExpandedAnalysis>
  )
}

function Cell({
  label,
  value,
  note,
  accent,
  lead,
}: {
  label: string
  value: string
  note?: string
  accent?: boolean
  lead?: boolean
}) {
  return (
    <div className={`ix__cell${accent ? ' is-accent' : ''}${lead ? ' is-lead' : ''}`}>
      <span className="ix__cell-label">{label}</span>
      <span className="ix__cell-value num">{value}</span>
      {note ? <span className="ix__cell-note">{note}</span> : null}
    </div>
  )
}

function Arrow() {
  return (
    <span className="ix__arrow" aria-hidden="true">
      <svg viewBox="0 0 40 12" preserveAspectRatio="none">
        <path
          d="M0 6h32M27 1.5L33 6l-6 4.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.1"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  )
}

function Fact({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="ix__fact">
      <span className="ix__fact-value num">{value}</span>
      <span className="ix__fact-label">{label}</span>
      {note ? <span className="ix__fact-note">{note}</span> : null}
    </div>
  )
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="ix__group">
      <h4 className="ix__group-title">{title}</h4>
      <dl className="ix__group-rows">{children}</dl>
    </div>
  )
}

function E({ k, v, s, tone }: { k: string; v: string; s?: string; tone?: 'accent' }) {
  return (
    <div className={`ix__e${tone ? ` is-${tone}` : ''}`}>
      <dt>{k}</dt>
      <dd>
        <span className="num">{v}</span>
        {s ? <em>{s}</em> : null}
      </dd>
    </div>
  )
}

function ProvRow({ k, v }: { k: string; v: string }) {
  return (
    <div className="ix__prov-row">
      <dt>{k}</dt>
      <dd className="num">{v}</dd>
    </div>
  )
}
