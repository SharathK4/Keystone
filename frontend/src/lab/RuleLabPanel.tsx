/**
 * Card 04 — the rule lab.
 *
 * A tree of life for rules. The lab is an evolution: rules people wrote at the
 * root, each round of the AI's rules branching outward, most lineages ending
 * where they were beaten. Drawn as a circular phylogeny filling the tile -
 * radial strokes for each generation, arcs joining siblings - with one lineage
 * lit from the root to the rim: the rule that beat the rest. No words on the
 * tile; the rule itself is one click away.
 */

import { useMemo } from 'react'
import { Panel } from '../components/Panel'
import { TexturedTile } from '../components/TexturedTile'
import './rule-lab-panel.css'

const S = 400
const C = S / 2
const LEVELS = [0, 34, 70, 108, 146, 182] // radius of each generation

interface Seg {
  d: string
  depth: number
  lit: boolean
}

function point(r: number, a: number): [number, number] {
  return [C + r * Math.cos(a), C + r * Math.sin(a)]
}

function arc(r: number, a0: number, a1: number): string {
  const [x0, y0] = point(r, a0)
  const [x1, y1] = point(r, a1)
  const large = Math.abs(a1 - a0) > Math.PI ? 1 : 0
  return `M${x0.toFixed(2)},${y0.toFixed(2)} A${r},${r} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)}`
}

/** A seeded circular phylogeny: each node splits into 2–3 children per ring. */
function grow() {
  let seed = 7
  const rand = () => {
    seed = (seed * 16807) % 2147483647
    return seed / 2147483647
  }
  const segs: Seg[] = []
  const leaves: { x: number; y: number; lit: boolean }[] = []
  let tip: [number, number] = [C, C]
  // The lit lineage ends near the upper right.
  const target = -Math.PI / 4

  const walk = (depth: number, a0: number, a1: number, lit: boolean) => {
    const mid = (a0 + a1) / 2
    const r0 = LEVELS[depth]
    const r1 = LEVELS[depth + 1]
    // radial stroke for this node
    const [x0, y0] = point(r0, mid)
    const [x1, y1] = point(r1, mid)
    segs.push({ d: `M${x0.toFixed(2)},${y0.toFixed(2)} L${x1.toFixed(2)},${y1.toFixed(2)}`, depth, lit })
    if (depth + 1 === LEVELS.length - 1) {
      leaves.push({ x: x1, y: y1, lit })
      if (lit) tip = [x1, y1]
      return
    }
    const n = depth === 0 ? 5 : rand() > 0.4 ? 2 : 3
    const span = a1 - a0
    const cuts = [a0]
    for (let i = 1; i < n; i += 1) cuts.push(a0 + (span * i) / n + (rand() - 0.5) * span * 0.12)
    cuts.push(a1)
    const mids = cuts.slice(0, -1).map((c, i) => (c + cuts[i + 1]) / 2)
    if (n > 1) segs.push({ d: arc(r1, mids[0], mids[mids.length - 1]), depth, lit: false })
    let litChild = -1
    if (lit) {
      let best = Infinity
      mids.forEach((m, i) => {
        const dist = Math.abs(Math.atan2(Math.sin(m - target), Math.cos(m - target)))
        if (dist < best) {
          best = dist
          litChild = i
        }
      })
      if (depth === 0) {
        // from the root straight out to the lit child
        const [lx, ly] = point(r1, mids[litChild])
        segs.push({ d: `M${C},${C} L${lx.toFixed(2)},${ly.toFixed(2)}`, depth: 1, lit: true })
      } else {
        // the lit arc runs from this node's angle to the chosen child's
        segs.push({ d: arc(r1, Math.min(mid, mids[litChild]), Math.max(mid, mids[litChild])), depth, lit: true })
      }
    }
    for (let i = 0; i < n; i += 1) walk(depth + 1, cuts[i], cuts[i + 1], i === litChild)
  }
  const start = -Math.PI / 2
  walk(0, start, start + 2 * Math.PI, true)
  return { segs, leaves, tip }
}

export function RuleLabPanel({ onOpen }: { onOpen: () => void }) {
  const art = useMemo(() => grow(), [])
  const plain = art.segs.filter((s) => !s.lit)
  const lit = art.segs.filter((s) => s.lit && s.depth > 0)

  return (
    <Panel
      index={4}
      title="The rule lab"
      subtitle="The AI writes its own rule for who to fund first."
      reveals="Open the lab"
      onOpen={onOpen}
    >
      <TexturedTile
        tone="tide"
        seed="rule-lab"
        figure={
          <svg className="lp__tree" viewBox={`0 0 ${S} ${S}`} preserveAspectRatio="xMidYMid meet" aria-hidden="true">
            <circle cx={C} cy={C} r={LEVELS[LEVELS.length - 1] + 8} className="lp__rim" />
            {plain.map((s, i) => (
              <path key={`p${i}`} d={s.d} className={`lp__branch d${s.depth}`} />
            ))}
            {lit.map((s, i) => (
              <path key={`l${i}`} d={s.d} className="lp__branch is-lit" />
            ))}
            {art.leaves.filter((l) => !l.lit).map((l, i) => (
              <circle key={`f${i}`} cx={l.x} cy={l.y} r={1.9} className="lp__leaf" />
            ))}
            <circle cx={C} cy={C} r={5} className="lp__root" />
            <circle cx={art.tip[0]} cy={art.tip[1]} r={11} className="lp__halo" />
            <circle cx={art.tip[0]} cy={art.tip[1]} r={5.5} className="lp__leaf is-lit" />
          </svg>
        }
      />
    </Panel>
  )
}
