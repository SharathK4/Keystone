/**
 * Card 06 - the rule lab.
 *
 * The last card on the page, built like the others: a title, one line, and a
 * tile. The tile holds the standing result on this server, each a mean over the
 * same replayed scenarios: the best rule people wrote, Keystone's own search,
 * and the best rule a language model has written so far. Until a model has
 * written one, that row says so instead of showing a number.
 */

import { useEffect, useState } from 'react'
import { Panel } from '../components/Panel'
import { TexturedTile } from '../components/TexturedTile'
import { lab, modelName, type Setup, type Tree } from './api'
import { Formula } from './Formula'
import './rule-lab-panel.css'

export function RuleLabPanel({ onOpen }: { onOpen: () => void }) {
  const [setup, setSetup] = useState<Setup | null>(null)
  const [tree, setTree] = useState<Tree | null>(null)

  useEffect(() => {
    let live = true
    const load = () =>
      lab
        .setup()
        .then((s) => {
          if (!live) return
          setSetup(s)
          const formula = s.board?.best?.formula
          if (formula) lab.check(formula, []).then((c) => live && c.tree && setTree(c.tree)).catch(() => undefined)
        })
        .catch(() => undefined)
    void load()
    // The board fills in while the server warms up; look once more, shortly.
    const timer = window.setTimeout(load, 20000)
    return () => {
      live = false
      window.clearTimeout(timer)
    }
  }, [])

  const board = setup?.board ?? null
  const rows: { label: string; value: number | null; empty: string; tone: string }[] = [
    { label: 'Best rule people wrote', value: board?.people_best?.mean ?? null, empty: 'scoring…', tone: 'people' },
    { label: 'Keystone’s search', value: board?.search_mean ?? null, empty: 'scoring…', tone: 'search' },
    { label: 'Best rule the AI wrote', value: board?.best?.mean ?? null, empty: 'open the lab', tone: 'ai' },
  ]
  const max = Math.max(1, ...rows.map((r) => r.value ?? 0))
  const writer = setup?.writer

  return (
    <Panel
      index={6}
      title="The rule lab"
      subtitle="A language model writes the rule for who to fund first. Keystone checks every one."
      reveals="Open the lab"
      onOpen={onOpen}
      className="card--wide"
    >
      <TexturedTile tone="tide" seed="rule-lab" className="lp">
        <div className="lp__body">
          <svg className="lp__tree" viewBox="0 0 200 200" aria-hidden="true">
            <path className="lp__branch" d="M34 150 C70 150 70 112 100 112 M34 150 C70 150 70 170 100 170 M100 112 C128 112 128 76 160 76 M100 112 C128 112 128 128 160 128 M100 170 C128 170 128 182 160 182 M160 76 C176 76 176 44 188 44" />
            <circle className="lp__node" cx="34" cy="150" r="6" />
            <circle className="lp__node" cx="100" cy="112" r="6" />
            <circle className="lp__node" cx="100" cy="170" r="6" />
            <circle className="lp__node" cx="160" cy="128" r="6" />
            <circle className="lp__node" cx="160" cy="182" r="6" />
            <circle className="lp__node is-best" cx="160" cy="76" r="7" />
            <circle className="lp__node is-best" cx="188" cy="44" r="8" />
          </svg>

          <div className="lp__board">
            <p className="lp__head">Share of the week’s damage prevented · same scenarios</p>
            <ul>
              {rows.map((r) => (
                <li key={r.label} className={`is-${r.tone}`}>
                  <span className="lp__label">{r.label}</span>
                  <span className="lp__track">{r.value !== null ? <i style={{ width: `${(r.value / max) * 100}%` }} /> : null}</span>
                  <span className="lp__val num">{r.value !== null ? `${r.value.toFixed(1)}%` : <em>{r.empty}</em>}</span>
                </li>
              ))}
            </ul>
            <p className="lp__foot">
              {board?.best ? (
                <>
                  <span className="lp__formula">{tree ? <Formula tree={tree} size="sm" /> : board.best.formula}</span>
                  <span>written by {modelName(board.best.author)}</span>
                </>
              ) : writer?.mode === 'model' ? (
                `Rules are written by ${modelName(writer.model)}, then replayed on every scenario.`
              ) : (
                'No model on this server: rules are written by random mutation.'
              )}
            </p>
          </div>
        </div>
      </TexturedTile>
    </Panel>
  )
}
