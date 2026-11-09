/**
 * Who the AI's rule funds, on the network from card 01.
 *
 * The same ring of merchants and the links learned between them. The merchant
 * that misses its payment is crossed out; the merchants the AI's rule funds are
 * terracotta; the ones Keystone's search funds with the same money are ringed in
 * sage. Where they differ is the rule's idea, drawn.
 */

import { useMemo } from 'react'
import type { DependencyView, MerchantView } from '../api/types'
import { layoutNetwork } from '../lib/networkLayout'

interface Props {
  merchants: MerchantView[]
  dependencies: DependencyView[]
  failing: string
  rule: string[]
  search: string[]
}

const SIZE = 420

export function WinnerRing({ merchants, dependencies, failing, rule, search }: Props) {
  const layout = useMemo(
    () => layoutNetwork(merchants, dependencies, { size: SIZE, maxEdges: 120 }),
    [merchants, dependencies],
  )
  const picked = new Set([...rule, ...search, failing])
  const touching = layout.edges.filter((e) => picked.has(e.source) || picked.has(e.target))

  // Labels sit just outside the ring; neighbours that would collide step out.
  const labels = new Map<string, { x: number; y: number; anchor: 'start' | 'end' | 'middle' }>()
  const placed: { x: number; y: number }[] = []
  const ids = [...new Set([...rule, ...search, failing])].filter((id) => layout.byId.has(id))
  ids.sort((a, b) => layout.byId.get(a)!.angle - layout.byId.get(b)!.angle)
  for (const id of ids) {
    const n = layout.byId.get(id)!
    const x = SIZE / 2 + Math.cos(n.angle) * (layout.radius + 24)
    let y = SIZE / 2 + Math.sin(n.angle) * (layout.radius + 24) + 4
    const step = Math.sin(n.angle) < 0 ? -15 : 15 // stack away from the ring
    for (let tries = 0; tries < 8; tries += 1) {
      if (placed.every((p) => Math.abs(p.x - x) > 52 || Math.abs(p.y - y) > 14)) break
      y += step
    }
    placed.push({ x, y })
    const c = Math.cos(n.angle)
    labels.set(id, { x, y, anchor: c > 0.2 ? 'start' : c < -0.2 ? 'end' : 'middle' })
  }
  const label = (id: string) => labels.get(id) ?? null

  return (
    <svg className="wr" viewBox={`-60 -64 ${SIZE + 120} ${SIZE + 100}`} role="img"
      aria-label={`The rule funds ${rule.join(', ')}; the search funds ${search.join(', ')}`}>
      <circle cx={SIZE / 2} cy={SIZE / 2} r={layout.radius} className="wr__rim" />
      {layout.edges.map((e, i) => (
        <path key={i} d={`M${e.x1},${e.y1} Q${e.cx},${e.cy} ${e.x2},${e.y2}`} className="wr__edge" />
      ))}
      {touching.map((e, i) => (
        <path
          key={`t${i}`}
          d={`M${e.x1},${e.y1} Q${e.cx},${e.cy} ${e.x2},${e.y2}`}
          className={`wr__edge ${rule.includes(e.source) || rule.includes(e.target) ? 'is-rule' : 'is-near'}`}
        />
      ))}
      {layout.nodes.map((n) => (
        <circle key={n.id} cx={n.x} cy={n.y} r={2.2} className="wr__node" />
      ))}
      {search.map((id) => {
        const n = layout.byId.get(id)
        return n ? <circle key={`s${id}`} cx={n.x} cy={n.y} r={11} className="wr__search" /> : null
      })}
      {rule.map((id) => {
        const n = layout.byId.get(id)
        return n ? <circle key={`r${id}`} cx={n.x} cy={n.y} r={6.5} className="wr__rule" /> : null
      })}
      {(() => {
        const n = layout.byId.get(failing)
        return n ? (
          <g className="wr__fail" transform={`translate(${n.x} ${n.y})`}>
            <path d="M-6,-6 L6,6 M6,-6 L-6,6" />
          </g>
        ) : null
      })()}
      {[...new Set([...rule, ...search, failing])].map((id) => {
        const l = label(id)
        if (!l) return null
        const kind = id === failing ? 'is-fail' : rule.includes(id) ? 'is-rule' : 'is-search'
        return (
          <text key={`l${id}`} x={l.x} y={l.y} textAnchor={l.anchor} className={`wr__label ${kind}`}>
            {id}
          </text>
        )
      })}
    </svg>
  )
}
