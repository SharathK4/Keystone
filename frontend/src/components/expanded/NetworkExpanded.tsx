/**
 * 01 expanded — the dependency structure.
 *
 * The figure at full size with hover and selection, a profile for whichever
 * merchant is under the pointer, and the systemic ranking beside it with the
 * centrality correlations that say what that ranking is *not*.
 *
 * The annotations are generated from the ranking, not written: "load-bearing"
 * is rank 1, "unusually central" is a merchant whose measured importance
 * outruns its structural centrality. If the data stops supporting a phrase, the
 * phrase disappears.
 */

import { useMemo, useState } from 'react'
import type {
  DependencyView,
  MerchantDetail,
  MerchantView,
  NetworkOverview,
  SystemicRankingView,
} from '../../api/types'
import { ExpandedAnalysis, XSection } from '../ExpandedAnalysis'
import { NetworkVisualization } from '../NetworkVisualization'
import { Annotation } from '../Annotation'
import { count, hours, inr, multiple } from '../../lib/format'
import { layoutNetwork } from '../../lib/networkLayout'
import './network-expanded.css'

interface Props {
  network: NetworkOverview
  merchants: MerchantView[]
  dependencies: DependencyView[]
  systemic: SystemicRankingView
  detail: MerchantDetail | null
  selectedId: string | null
  onSelect: (id: string | null) => void
  onClose: () => void
}

export function NetworkExpanded({
  network,
  merchants,
  dependencies,
  systemic,
  detail,
  selectedId,
  onSelect,
  onClose,
}: Props) {
  const [hoverId, setHoverId] = useState<string | null>(null)

  const layout = useMemo(
    () => layoutNetwork(merchants, dependencies, { size: 560, maxEdges: 216 }),
    [merchants, dependencies],
  )

  const leader = systemic.entries[0]

  // "Unusually central": measured marginal disruption well ahead of the
  // structural centrality that would predict it. Computed, not asserted.
  const outlier = useMemo(() => {
    const scored = systemic.entries
      .filter((e) => e.structural_centrality > 0)
      .map((e) => ({ e, ratio: e.importance / e.structural_centrality }))
      .sort((a, b) => b.ratio - a.ratio)
    const top = scored[0]
    return top && top.e.merchant_id !== leader?.merchant_id ? top.e : null
  }, [systemic.entries, leader])

  const focusId = hoverId ?? selectedId ?? leader?.merchant_id ?? null
  const focus = focusId ? merchants.find((m) => m.merchant_id === focusId) : undefined
  const focusDetail = detail && detail.merchant.merchant_id === focusId ? detail : null

  return (
    <ExpandedAnalysis
      title="Where payment dependence actually lives"
      subtitle="Merchants on a ring ordered by counterparty tier, importance leading each band. Chords are estimated pass-through between merchants, recovered from transaction history alone."
      onClose={onClose}
    >
      <div className="nx">
        <figure className="nx__plot">
          <NetworkVisualization
            layout={layout}
            ringed={leader?.merchant_id ?? null}
            selected={selectedId}
            onHover={setHoverId}
            onSelect={onSelect}
          />
          <Annotation className="nx__note nx__note--lead" delay={500}>
            load-bearing — highest measured marginal disruption
          </Annotation>
          {outlier ? (
            <Annotation className="nx__note nx__note--outlier" delay={700}>
              <span className="num">{outlier.merchant_id}</span> — unusually central for its
              position
            </Annotation>
          ) : null}
          <p className="nx__hint">
            Hover to inspect · click to pin · {count(layout.edges.length)} of{' '}
            {count(network.n_relationships)} relationships drawn
          </p>
        </figure>

        <div className="nx__rail">
          {focus ? (
            <section className="nx__profile" key={focus.merchant_id}>
              <header className="nx__profile-head">
                <span className="nx__id num">{focus.merchant_id}</span>
                <span className="nx__tier">
                  {focus.tier} · {focus.sector}
                </span>
              </header>

              <dl className="nx__rows">
                <Row
                  label="Systemic importance"
                  value={
                    focus.systemic_importance !== null
                      ? focus.systemic_importance.toFixed(3)
                      : 'not sampled'
                  }
                  sub={focus.systemic_rank !== null ? `rank ${focus.systemic_rank}` : undefined}
                />
                <Row
                  label="Inbound dependencies"
                  value={count(focusDetail?.upstream.length ?? focus.in_degree)}
                />
                <Row
                  label="Outbound dependencies"
                  value={count(focusDetail?.downstream.length ?? focus.out_degree)}
                />
                <Row label="Payables in horizon" value={inr(focus.payables_in_horizon)} />
                <Row label="Receivables in horizon" value={inr(focus.receivables_in_horizon)} />
                <Row
                  label="Cover ratio"
                  value={focus.cover_ratio !== null ? multiple(focus.cover_ratio) : 'no payables'}
                  sub={focus.vulnerable ? 'below 1.0×' : undefined}
                  tone={focus.vulnerable ? 'accent' : undefined}
                />
              </dl>

              {focusDetail && focusDetail.downstream.length > 0 ? (
                <div className="nx__deps">
                  <span className="nx__deps-label">Downstream exposure — pass-through and lag</span>
                  {[...focusDetail.downstream]
                    .sort((a, b) => b.pass_through - a.pass_through)
                    .slice(0, 6)
                    .map((d) => (
                      <div className="nx__dep" key={d.target_id}>
                        <span className="num">{d.target_id}</span>
                        <span className="nx__dep-bar" aria-hidden="true">
                          <i style={{ width: `${Math.min(d.pass_through, 1) * 100}%` }} />
                        </span>
                        <span className="nx__dep-val num">
                          {(d.pass_through * 100).toFixed(0)}%
                        </span>
                        <span className="nx__dep-lag num">{hours(d.lag_mean_hours)}</span>
                      </div>
                    ))}
                </div>
              ) : focusDetail ? (
                <p className="nx__isolated">
                  No estimated dependency either way. This merchant transacts, but the estimator
                  found no reliable pass-through link to a counterparty.
                </p>
              ) : null}
            </section>
          ) : null}
        </div>
      </div>

      <XSection
        label="Systemic importance"
        note={`bar is measured importance · figure is downstream payment value delayed · ${count(
          systemic.n_sampled,
        )} of ${count(systemic.n_merchants)} merchants probed`}
      >
        <div className="nx__ranking">
          {systemic.entries.slice(0, 8).map((e) => {
            const share = e.importance / (systemic.entries[0]?.importance || 1)
            return (
              <button
                key={e.merchant_id}
                type="button"
                className={`nx__rank${selectedId === e.merchant_id ? ' is-on' : ''}`}
                onClick={() => onSelect(selectedId === e.merchant_id ? null : e.merchant_id)}
                onPointerEnter={() => setHoverId(e.merchant_id)}
                onPointerLeave={() => setHoverId(null)}
              >
                <span className="nx__rank-n num">{e.rank}</span>
                <span className="nx__rank-id num">{e.merchant_id}</span>
                <span className="nx__rank-bar" aria-hidden="true">
                  <i style={{ width: `${share * 100}%` }} />
                </span>
                <span className="nx__rank-val num">{inr(e.downstream_delayed_value)}</span>
                <span className="nx__rank-meta num">
                  {count(e.downstream_affected)} downstream · {hours(e.time_to_impact_hours)}
                </span>
              </button>
            )
          })}
        </div>

        <p className="nx__correlations">
          Rank correlation against simpler orderings —{' '}
          {Object.entries(systemic.baseline_rank_correlation).map(([k, v], i) => (
            <span key={k}>
              {i > 0 ? ', ' : ''}
              {k.replace(/_/g, ' ')} <b className="num">{v.toFixed(2)}</b>
            </span>
          ))}
          . Correlated but not equivalent: ranking by size alone picks different merchants.
        </p>
      </XSection>
    </ExpandedAnalysis>
  )
}

function Row({
  label,
  value,
  sub,
  tone,
}: {
  label: string
  value: string
  sub?: string
  tone?: 'accent'
}) {
  return (
    <div className={`nx__row${tone ? ` nx__row--${tone}` : ''}`}>
      <dt>{label}</dt>
      <dd>
        <span className="num">{value}</span>
        {sub ? <em>{sub}</em> : null}
      </dd>
    </div>
  )
}
