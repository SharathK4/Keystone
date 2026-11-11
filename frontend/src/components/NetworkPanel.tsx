/**
 * Card 01 — the network.
 *
 * An orrery of merchants: one orbit per size class, anchors at the centre and
 * micro merchants on the outside, each merchant a dot sized by how much the
 * network leans on it. Only the learned links of the three most load-bearing
 * merchants are drawn, and the first is lit and named. The picture says "a few
 * merchants hold this together" without a single figure; every link and number
 * is one click away.
 */

import { useMemo } from 'react'
import type { DependencyView, MerchantView, SystemicRankingView } from '../api/types'
import { Panel } from './Panel'
import { TexturedTile } from './TexturedTile'
import './network-panel.css'

interface Props {
  merchants: MerchantView[]
  dependencies: DependencyView[]
  systemic: SystemicRankingView
  onOpen: () => void
}

const TIERS = ['anchor', 'large', 'medium', 'small', 'micro']
const SIZE = 440
const C = SIZE / 2

interface Dot {
  id: string
  x: number
  y: number
  r: number
}

export function NetworkPanel({ merchants, dependencies, systemic, onOpen }: Props) {
  const ranked = systemic.entries.map((e) => e.merchant_id)
  const leader = ranked[0] ?? null
  const keystones = useMemo(() => new Set(ranked.slice(0, 3)), [ranked])

  const art = useMemo(() => {
    const maxImp = Math.max(...merchants.map((m) => m.systemic_importance ?? 0), 1e-9)
    const tiers = TIERS.map((t) => merchants.filter((m) => m.tier === t))
    const used = tiers.filter((t) => t.length)
    const dots = new Map<string, Dot>()
    const orbits: number[] = []
    used.forEach((members, i) => {
      const radius = 44 + i * ((C - 52) / Math.max(used.length - 1, 1))
      orbits.push(radius)
      const sorted = [...members].sort((a, b) => a.merchant_id.localeCompare(b.merchant_id))
      sorted.forEach((m, j) => {
        const angle = -Math.PI / 2 + (j / sorted.length) * 2 * Math.PI + i * 0.37
        const imp = (m.systemic_importance ?? 0) / maxImp
        dots.set(m.merchant_id, {
          id: m.merchant_id,
          x: C + radius * Math.cos(angle),
          y: C + radius * Math.sin(angle),
          r: 1.8 + 5.2 * Math.sqrt(Math.max(imp, 0)),
        })
      })
    })
    const links = dependencies
      .filter((d) => keystones.has(d.source_id) || keystones.has(d.target_id))
      .map((d) => {
        const a = dots.get(d.source_id)
        const b = dots.get(d.target_id)
        if (!a || !b) return null
        // A gentle arc to one side, like a flight path on a chart.
        const mx = (a.x + b.x) / 2
        const my = (a.y + b.y) / 2
        const dx = b.x - a.x
        const dy = b.y - a.y
        const cx = mx - dy * 0.28
        const cy = my + dx * 0.28
        const lit = d.source_id === leader || d.target_id === leader
        return { d: `M${a.x.toFixed(1)},${a.y.toFixed(1)} Q${cx.toFixed(1)},${cy.toFixed(1)} ${b.x.toFixed(1)},${b.y.toFixed(1)}`, lit }
      })
      .filter((l): l is { d: string; lit: boolean } => l !== null)
    return { dots: [...dots.values()], orbits, links }
  }, [merchants, dependencies, keystones, leader])

  const lead = leader ? art.dots.find((d) => d.id === leader) : undefined

  return (
    <Panel
      index={1}
      title="The network"
      subtitle="Who pays whom, learned from payments alone."
      reveals="See the network"
      onOpen={onOpen}
    >
      <figure className="np">
        <TexturedTile
          tone="ink"
          seed={leader ?? 'network'}
          figure={
            <svg className="np__orrery" viewBox={`0 0 ${SIZE} ${SIZE}`} preserveAspectRatio="xMidYMid meet" aria-hidden="true">
              {art.orbits.map((r) => (
                <circle key={r} cx={C} cy={C} r={r} className="np__orbit" />
              ))}
              {art.links.filter((l) => !l.lit).map((l, i) => (
                <path key={`k${i}`} d={l.d} className="np__link" />
              ))}
              {art.links.filter((l) => l.lit).map((l, i) => (
                <path key={`l${i}`} d={l.d} className="np__link is-lit" />
              ))}
              {art.dots.map((d) => (
                <circle
                  key={d.id}
                  cx={d.x}
                  cy={d.y}
                  r={d.r}
                  className={`np__dot${keystones.has(d.id) ? ' is-key' : ''}${d.id === leader ? ' is-lead' : ''}`}
                />
              ))}
              {lead ? (
                <>
                  <circle cx={lead.x} cy={lead.y} r={lead.r + 7} className="np__halo" />
                  <text x={SIZE + 2} y={SIZE - 4} textAnchor="end" className="np__note">
                    load-bearing — {lead.id}
                  </text>
                </>
              ) : null}
            </svg>
          }
        />
      </figure>
    </Panel>
  )
}
