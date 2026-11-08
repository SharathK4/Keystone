/**
 * 02 expanded — the scenario analysis.
 *
 * The spectrum at full height with its zone rail, then the three things the
 * landing panel deliberately withholds: who was reached and when, how the
 * timing is distributed, and how much of this the backend is willing to stand
 * behind.
 *
 * The confidence block is reported verbatim rather than summarised into a
 * score. `calibrated: false` means the exposure numbers come from the analytic
 * propagator and have not been calibrated against outcomes - printing a
 * confidence percentage over that would be inventing a guarantee.
 */

import type { MerchantView, ScenarioSnapshot, ScenarioSummary } from '../../api/types'
import { ExpandedAnalysis, XSection } from '../ExpandedAnalysis'
import { PullAMerchant } from '../PullAMerchant'
import { ExposureDotMatrix } from '../ExposureDotMatrix'
import { ScenarioSelector } from '../ScenarioSelector'
import { count, hours, money, pct, scenarioLabel } from '../../lib/format'
import './exposure-expanded.css'

interface Props {
  merchants: MerchantView[]
  scenario: ScenarioSnapshot | null
  scenarios: ScenarioSummary[]
  selectedId: string | null
  busy: boolean
  onSelect: (id: string) => void
  onClose: () => void
}

export function ExposureExpanded({
  merchants,
  scenario,
  scenarios,
  selectedId,
  busy,
  onSelect,
  onClose,
}: Props) {
  const impact = scenario?.projected_impact
  const affected = scenario?.affected_merchants ?? []
  const buckets = scenario?.time_to_constraint ?? []
  const peak = Math.max(1, ...buckets.map((b) => b.n_merchants))

  return (
    <ExpandedAnalysis
      title={scenario ? scenarioLabel(scenario.family) : 'Scenario analysis'}
      subtitle={
        scenario
          ? `${scenario.shock.origin_merchants.join(' and ')} is short ${money(scenario.shock.magnitude)} from hour ${Math.round(scenario.shock.onset_hours)}. Figures count only what this failure adds.`
          : 'Figures count only what the failure adds.'
      }
      aside={
        <ScenarioSelector
          scenarios={scenarios}
          selected={selectedId}
          busy={busy}
          onSelect={onSelect}
        />
      }
      onClose={onClose}
    >
      <div className={`xx${busy ? ' is-busy' : ''}`}>
        <div className="xx__figures">
          <Figure
            value={count(impact?.n_affected)}
            label="merchants reached"
            note={
              impact && impact.n_affected === 0
                ? 'no merchant crossed into constraint'
                : `of ${count(merchants.length)} in the network`
            }
            lead
          />
          <Figure value={money(impact?.disrupted_value)} label="payments delayed" />
          <Figure
            value={count(impact?.max_cascade_depth)}
            label="steps out"
            note="from the failure"
          />
          <Figure
            value={impact ? `${(impact.systemic_exposure * 100).toFixed(2)}%` : '—'}
            label="at risk"
            note="of this week’s payments"
          />
          <Figure
            value={count(impact?.n_defaulted)}
            label="defaults"
            note="including ones that happen anyway"
          />
        </div>

        <XSection
          label="Every merchant, thinnest cash cover first"
          note="one dot per merchant · colour shows this failure"
        >
          <div className="xx__spectrum">
            <ExposureDotMatrix
              merchants={merchants}
              affected={affected}
              origins={scenario?.shock.origin_merchants ?? []}
            />
          </div>
        </XSection>

        <XSection label="Make any merchant fail" note="replayed offline, or computed now">
          <PullAMerchant merchants={merchants} />
        </XSection>

        <div className="xx__split">
          <XSection label="When they run short" note="hours after the failure">
            {buckets.some((b) => b.n_merchants > 0) ? (
              <div className="xx__timing">
                {buckets.map((b) => (
                  <div
                    className={`xx__bar${b.n_merchants > 0 ? ' is-hit' : ''}`}
                    key={`${b.from_hours}-${b.to_hours}`}
                    title={`${b.from_hours}–${b.to_hours}h · ${b.n_merchants} merchants · ${money(
                      b.disrupted_value,
                    )}`}
                  >
                    <span className="xx__bar-n num">{b.n_merchants > 0 ? b.n_merchants : ''}</span>
                    <span className="xx__bar-col">
                      <span
                        className="xx__bar-fill"
                        style={{ height: `${(b.n_merchants / peak) * 100}%` }}
                      />
                    </span>
                    <span className="xx__bar-tick num">{b.to_hours}h</span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="xx__none">
                No one else runs short this week.
              </p>
            )}
          </XSection>

        </div>

        {affected.length > 0 ? (
          <XSection label="Merchants reached" note="most payments delayed first">
            <div className="xx__affected">
              {affected.map((a) => (
                <div className="xx__hit" key={a.merchant_id}>
                  <span className="xx__hit-rank num">{a.rank}</span>
                  <span className="xx__hit-id num">{a.merchant_id}</span>
                  <span className="xx__hit-tier">
                    {a.tier} · {a.sector}
                  </span>
                  <span className="xx__hit-depth num">
                    {a.cascade_depth === 0 ? 'direct' : `depth ${a.cascade_depth}`}
                  </span>
                  <span className="xx__hit-time num">
                    {a.time_to_constraint_hours !== null
                      ? hours(a.time_to_constraint_hours)
                      : '—'}
                  </span>
                  <span className="xx__hit-value num">{money(a.disrupted_value)}</span>
                </div>
              ))}
            </div>
          </XSection>
        ) : null}

        <XSection label="Across scenarios" note="the same network, seven different failures">
          <div className="xx__compare">
            {scenarios.map((s) => {
              const on = s.scenario_id === selectedId
              const w = Math.max(...scenarios.map((x) => x.disrupted_value), 1)
              return (
                <button
                  key={s.scenario_id}
                  type="button"
                  className={`xx__row${on ? ' is-on' : ''}`}
                  onClick={() => onSelect(s.scenario_id)}
                >
                  <span className="xx__row-name">{scenarioLabel(s.family)}</span>
                  <span className="xx__row-bar" aria-hidden="true">
                    <i style={{ width: `${(s.disrupted_value / w) * 100}%` }} />
                  </span>
                  <span className="xx__row-val num">{money(s.disrupted_value)}</span>
                  <span className="xx__row-n num">{count(s.n_affected)} reached</span>
                  <span className="xx__row-red num">
                    {s.recommended_action ? pct(s.disruption_reduction_pct) : 'no action'}
                  </span>
                </button>
              )
            })}
          </div>
        </XSection>
      </div>
    </ExpandedAnalysis>
  )
}

function Figure({
  value,
  label,
  note,
  lead,
}: {
  value: string
  label: string
  note?: string
  lead?: boolean
}) {
  return (
    <div className={`xx__fig${lead ? ' is-lead' : ''}`}>
      <span className="xx__fig-value num">{value}</span>
      <span className="xx__fig-label">{label}</span>
      {note ? <span className="xx__fig-note">{note}</span> : null}
    </div>
  )
}

