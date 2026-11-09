/**
 * Card 04 — the rule lab.
 *
 * A picture, like the cards around it: a family tree of rules drawn in fine
 * line across the tile, most branches faint, one lineage lit - and at its tip,
 * set in the display face, the rule a language model wrote that beat every rule
 * people wrote and Keystone's own search. The formula is the standing best on
 * this server (re-scored by replay at every start); until there is one, the lit
 * branch ends in an empty leaf. Everything else is one click away.
 */

import { useEffect, useMemo, useState } from 'react'
import { Panel } from '../components/Panel'
import { TexturedTile } from '../components/TexturedTile'
import { lab, type Tree } from './api'
import { Formula } from './Formula'
import './rule-lab-panel.css'

interface Branch {
  d: string
  lit: boolean
}

interface Leaf {
  x: number
  y: number
  lit: boolean
}

/** A seeded branching tree, left to right; one path from the root is lit. */
function grow(): { branches: Branch[]; leaves: Leaf[]; tip: Leaf } {
  let seed = 11
  const rand = () => {
    seed = (seed * 16807) % 2147483647
    return seed / 2147483647
  }
  const xs = [56, 150, 238, 318]
  const branches: Branch[] = []
  const leaves: Leaf[] = []
  let tip: Leaf = { x: xs[3], y: 120, lit: true }
  const walk = (depth: number, x: number, y: number, spread: number, lit: boolean, litChild: number) => {
    if (depth === xs.length - 1) {
      const leaf = { x, y, lit }
      leaves.push(leaf)
      if (lit) tip = leaf
      return
    }
    const n = depth === 0 ? 3 : 2
    for (let i = 0; i < n; i += 1) {
      const nx = xs[depth + 1] + (rand() - 0.5) * 10
      const ny = y + (i - (n - 1) / 2) * spread + (rand() - 0.5) * spread * 0.35
      const childLit = lit && i === Math.min(litChild, n - 1)
      const mx = (x + nx) / 2
      branches.push({ d: `M${x},${y} C${mx},${y} ${mx},${ny} ${nx},${ny}`, lit: childLit })
      walk(depth + 1, nx, ny, spread * 0.5, childLit, 0)
    }
  }
  walk(0, xs[0], 196, 112, true, 0)
  return { branches, leaves, tip }
}

export function RuleLabPanel({ onOpen }: { onOpen: () => void }) {
  const [tree, setTree] = useState<Tree | null>(null)
  const art = useMemo(() => grow(), [])

  useEffect(() => {
    let live = true
    const load = () =>
      lab
        .setup()
        .then((s) => {
          const formula = s.board?.best?.formula
          if (live && formula) {
            lab.check(formula, []).then((c) => live && c.tree && setTree(c.tree)).catch(() => undefined)
          }
        })
        .catch(() => undefined)
    void load()
    const timer = window.setTimeout(load, 20000) // the board fills while the server warms
    return () => {
      live = false
      window.clearTimeout(timer)
    }
  }, [])

  const { tip } = art

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
          <div className="lp">
            <svg className="lp__tree" viewBox="0 0 600 392" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
              {art.branches
                .filter((b) => !b.lit)
                .map((b, i) => (
                  <path key={`b${i}`} d={b.d} className="lp__branch" />
                ))}
              {art.branches
                .filter((b) => b.lit)
                .map((b, i) => (
                  <path key={`l${i}`} d={b.d} className="lp__branch is-lit" />
                ))}
              {art.leaves
                .filter((l) => !l.lit)
                .map((l, i) => (
                  <circle key={`f${i}`} cx={l.x} cy={l.y} r={3.2} className="lp__leaf" />
                ))}
              <circle cx={56} cy={196} r={5} className="lp__root" />
              <path d={`M${tip.x},${tip.y} L${tip.x + 26},${tip.y}`} className="lp__branch is-lit" />
              <circle cx={tip.x} cy={tip.y} r={9} className="lp__halo" />
              <circle cx={tip.x} cy={tip.y} r={5.5} className="lp__leaf is-lit" />
            </svg>
            <div className="lp__formula" style={{ top: `${(tip.y / 392) * 100}%` }}>
              {tree ? <Formula tree={tree} size="md" /> : <span className="lp__blank" />}
            </div>
          </div>
        }
      />
    </Panel>
  )
}
