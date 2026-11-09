/**
 * The rule lab.
 *
 * Keystone's problem as a puzzle a machine can attack: one merchant misses a
 * payment and there is a fixed fund - in what order do you fund the merchants
 * who are short? People wrote six rules. A language model writes more, reading
 * every rule's score on every scenario; Keystone replays the week to score each
 * one; the best go back to the model. You choose what it trains on, which
 * clues it may use, which rules to breed - and you can enter your own rule.
 *
 * Nothing on this stage is precomputed: every dot is a rule that was written
 * for this lab and replayed on every scenario, and the timings are measured.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { createPortal } from 'react-dom'
import { money } from '../lib/format'
import { lab, modelName, viaWhom, type Check, type Clue, type Setup } from './api'
import { Formula } from './Formula'
import { Lineage } from './Lineage'
import { ScenarioGrid } from './ScenarioGrid'
import { UseSheet } from './UseSheet'
import { runningMean, useLab, type LiveRule } from './useLab'
import './rule-lab.css'

interface Props {
  onClose: () => void
}

const TONE = { people: 'silver', you: 'white', model: 'ember', mutation: 'sand' } as const

export function RuleLab({ onClose }: Props) {
  const { state, start, evolve, add, test, reset } = useLab()
  const [setup, setSetup] = useState<Setup | null>(null)
  const [setupError, setSetupError] = useState<string | null>(null)
  const [training, setTraining] = useState<string[]>([])
  const [budgets, setBudgets] = useState<number[]>([])
  const [k, setK] = useState(3)
  const [banned, setBanned] = useState<string[]>([])
  const [yourRule, setYourRule] = useState('')
  const [checked, setCheck] = useState<Check | null>(null)
  const [deep, setDeep] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [partner, setPartner] = useState<string | null>(null)
  const [breeding, setBreeding] = useState(false)
  const [hover, setHover] = useState<{ id: string; x: number; y: number } | null>(null)
  const [activeCell, setActiveCell] = useState<string | null>(null)
  const [paying, setPaying] = useState(false)
  const [auto, setAuto] = useState(0)
  const shell = useRef<HTMLDivElement>(null)

  useEffect(() => {
    lab
      .setup()
      .then((s) => {
        setSetup(s)
        setTraining(s.pool.filter((p) => p.training).map((p) => p.merchant_id))
        setBudgets(s.budgets)
        setK(s.k)
      })
      .catch((e: unknown) => setSetupError(e instanceof Error ? e.message : 'The lab is unavailable.'))
  }, [])

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (paying) setPaying(false)
      else onClose()
    }
    document.addEventListener('keydown', onKey)
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    shell.current?.focus({ preventScroll: true })
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [onClose, paying])

  // Your rule is checked as you type: the same parser the server runs.
  useEffect(() => {
    const text = yourRule.trim()
    if (!text) return
    const timer = window.setTimeout(() => {
      lab.check(text, banned).then(setCheck).catch(() => setCheck(null))
    }, 260)
    return () => window.clearTimeout(timer)
  }, [yourRule, banned])

  const check = yourRule.trim() ? checked : null
  const rules = useMemo(() => state.order.map((id) => state.rules[id]).filter(Boolean), [state.order, state.rules])
  const scored = rules.filter((r) => r.status === 'scored')
  const best = useMemo(
    () => [...scored].sort((a, b) => b.mean - a.mean || a.size - b.size)[0] ?? null,
    [scored],
  )
  const peopleBest = useMemo(() => {
    const p = scored.filter((r) => r.origin === 'people')
    return p.length ? Math.max(...p.map((r) => r.mean)) : null
  }, [scored])
  const focus: LiveRule | null =
    (selected && state.rules[selected]) ||
    rules.find((r) => r.status === 'testing') ||
    best ||
    null

  const busy = state.phase === 'busy' || state.phase === 'starting'
  const started = Boolean(state.header)
  const clues: Clue[] = setup?.clues ?? []
  const writer = state.writer ?? setup?.writer ?? null

  // Several rounds in a row, each waiting for the last.
  useEffect(() => {
    if (auto <= 0 || busy || !started) return
    const timer = window.setTimeout(() => {
      setAuto((n) => n - 1)
      void evolve('evolve', [], deep)
    }, 650)
    return () => window.clearTimeout(timer)
  }, [auto, busy, started, evolve, deep])

  const begin = () => {
    if (!setup) return
    setSelected(null)
    setPartner(null)
    void start({
      training,
      budgets,
      k,
      banned,
      your_rule: check?.ok ? yourRule.trim() : null,
    })
  }

  const pick = useCallback(
    (id: string, e: MouseEvent) => {
      if (breeding || e.shiftKey) {
        if (selected && id !== selected) {
          setPartner(id)
          setBreeding(false)
          return
        }
      }
      setSelected(id)
      setPartner(null)
      setActiveCell(null)
    },
    [breeding, selected],
  )

  const toggle = <T,>(list: T[], item: T) => (list.includes(item) ? list.filter((x) => x !== item) : [...list, item])

  const scenarios = state.header?.scenarios ?? []
  const focusValues = focus ? (focus.status === 'scored' ? Object.fromEntries(focus.cells.map((c) => [c.key, c.pct])) : focus.live) : {}
  const focusCell = focus && activeCell ? focus.cells.find((c) => c.key === activeCell) : null
  const searchCell = activeCell ? state.search[activeCell] : null
  const focusMean = focus ? runningMean(focus) : null
  const round = focus ? state.rounds.find((r) => r.generation === focus.generation && r.admitted.includes(focus.id)) : undefined
  const totalScenarios = scenarios.length

  return createPortal(
    <div className="rl" role="dialog" aria-modal="true" aria-label="The rule lab">
      <button className="rl__scrim" onClick={onClose} aria-label="Close" tabIndex={-1} />
      <div className="rl__shell" ref={shell} tabIndex={-1}>
      {/* ------------------------------------------------------------ top */}
      <header className="rl__top">
        <div className="rl__heading">
          <h2 className="rl__title">The rule lab</h2>
          <p className="rl__subtitle">
            A merchant misses a payment and there is a fixed fund. A language model writes rules for who to fund
            first; Keystone replays the week to score each one.
          </p>
        </div>
        <div className="rl__status">
          <p className="rl__writer">
            <i className={`rl__led${writer?.mode === 'model' ? ' is-on' : ''}`} aria-hidden="true" />
            {writer?.mode === 'model' ? (
              <span>
                Rules written by <b>{modelName(writer.model)}</b> {viaWhom(writer.model)}
              </span>
            ) : (
              <span title={writer?.reason ?? ''}>
                No model on this server: rules written by <b>random mutation</b>
              </span>
            )}
          </p>
          <p className="rl__stats num" aria-live="polite">
            {started ? (
              <>
                <span><b>{state.generation}</b> round{state.generation === 1 ? '' : 's'}</span>
                <span><b>{scored.length}</b> rules tested</span>
                <span><b>{state.replays}</b> replays</span>
                <span><b>{state.refused.length}</b> refused</span>
              </>
            ) : (
              <span>nothing precomputed: every score is a replay</span>
            )}
            {started && !busy ? (
              <button
                type="button"
                className="rl__new"
                onClick={() => {
                  reset()
                  setSelected(null)
                  setPartner(null)
                }}
              >
                New lab
              </button>
            ) : null}
          </p>
        </div>
        <button className="rl__close" onClick={onClose} aria-label="Close the lab">
          <span aria-hidden="true" />
        </button>
      </header>

      <div className="rl__main">
        {/* ------------------------------------------------------ left: the test */}
        <aside className="rl__left">
          <section className="rl__sec">
            <h3><em>1</em> Failures to learn from</h3>
            <p className="rl__hint">The rest are kept back for the final test.</p>
            <ul className="rl__pool">
              {setup?.pool.map((p) => {
                const on = training.includes(p.merchant_id)
                const max = Math.max(...setup.pool.map((x) => x.missed))
                return (
                  <li key={p.merchant_id}>
                    <button
                      type="button"
                      className={`rl__fail${on ? ' is-on' : ''}`}
                      disabled={started || (!on && training.length >= 5) || (on && training.length <= 1)}
                      onClick={() => setTraining((t) => toggle(t, p.merchant_id))}
                      aria-pressed={on}
                    >
                      <span className="rl__failid">{p.merchant_id}</span>
                      <span className="rl__failbar"><span style={{ width: `${Math.max(4, (p.missed / max) * 100)}%` }} /></span>
                      <span className="rl__failamt num">{money(p.missed)}</span>
                    </button>
                  </li>
                )
              })}
            </ul>
          </section>

          <section className="rl__sec">
            <h3><em>2</em> Size of the fund</h3>
            <div className="rl__chips">
              {setup?.budgets.map((b) => (
                <button
                  key={b}
                  type="button"
                  className={`rl__chip${budgets.includes(b) ? ' is-on' : ''}`}
                  disabled={started || (budgets.includes(b) && budgets.length <= 1)}
                  onClick={() => setBudgets((x) => toggle(x, b).sort((p, q) => p - q))}
                >
                  {money(b)}
                </button>
              ))}
            </div>
            <div className="rl__seg" role="group" aria-label="Fund at most">
              <span>fund at most</span>
              {[1, 2, 3, 4].map((n) => (
                <button key={n} type="button" className={k === n ? 'is-on' : ''} disabled={started} onClick={() => setK(n)}>
                  {n}
                </button>
              ))}
              <span>merchants</span>
            </div>
          </section>

          <section className="rl__sec">
            <h3><em>3</em> Clues the AI may read</h3>
            <p className="rl__hint">Strike one out and the model must win without it.</p>
            <div className="rl__clues">
              {clues.map((c) => (
                <button
                  key={c.name}
                  type="button"
                  title={c.words}
                  className={`rl__clue${banned.includes(c.name) ? ' is-banned' : ''}${c.group === 'this failure' ? ' is-failure' : ''}`}
                  disabled={started}
                  onClick={() => setBanned((b) => toggle(b, c.name))}
                >
                  {c.name.replace('_', ' ')}
                </button>
              ))}
            </div>
          </section>

          <section className="rl__sec rl__yours">
            <h3><em>4</em> Your rule</h3>
            <p className="rl__hint">Write one. It competes with the AI’s.</p>
            <input
              className={`rl__input${check ? (check.ok ? ' is-ok' : ' is-bad') : ''}`}
              value={yourRule}
              onChange={(e) => setYourRule(e.target.value)}
              placeholder="e.g. need * importance"
              spellCheck={false}
              maxLength={240}
              aria-label="Your rule"
            />
            <div className="rl__check" aria-live="polite">
              {check?.ok && check.tree ? <Formula tree={check.tree} clues={clues} size="sm" /> : null}
              {check && !check.ok ? <span className="rl__err">{check.error}</span> : null}
            </div>
            {started ? (
              <button type="button" className="rl__ghost" disabled={busy || !check?.ok} onClick={() => void add(yourRule.trim())}>
                Add my rule to the lab
              </button>
            ) : null}
          </section>
        </aside>

        {/* ---------------------------------------------------------- centre */}
        <section className="rl__centre">
          {!started ? (
            <div className="rl__intro">
              <p className="rl__eyebrow">A puzzle for a machine</p>
              <h2>Who do you fund first?</h2>
              <p className="rl__lede">
                People wrote six rules. None wins everywhere. A language model writes new ones, Keystone replays the
                week to score every one on every failure and fund size, and the best go back to the model.
              </p>
              <ol className="rl__how">
                <li><b>Writes</b><span>a model proposes formulas over {clues.length || 14} clues</span></li>
                <li><b>Replays</b><span>Keystone scores each on {training.length * budgets.length || 12} scenarios</span></li>
                <li><b>Breeds</b><span>the best rules return to the model</span></li>
              </ol>
              <button type="button" className="rl__go" disabled={!setup || busy} onClick={begin}>
                {busy ? 'Opening the lab…' : 'Start the lab'}
                <span aria-hidden="true">→</span>
              </button>
              {setupError ? <p className="rl__err">{setupError}</p> : null}
              {state.error ? <p className="rl__err">{state.error}</p> : null}
            </div>
          ) : (
            <>
              <div className="rl__chart">
                <Lineage
                  rules={rules}
                  searchMean={state.searchMean}
                  generation={state.generation}
                  thinking={state.thinking}
                  refused={state.refused}
                  selected={focus?.id ?? null}
                  partner={partner}
                  bestId={best?.id ?? null}
                  onPick={pick}
                  onHover={(id, at) => setHover(id && at ? { id, ...at } : null)}
                />
                {hover && state.rules[hover.id] ? <Tip rule={state.rules[hover.id]} at={hover} clues={clues} /> : null}
                {state.thinking ? <Writing thinking={state.thinking} /> : null}
              </div>

              <div className="rl__bar">
                <div className="rl__depth" role="group" aria-label="How the model writes">
                  <button type="button" className={!deep ? 'is-on' : ''} onClick={() => setDeep(false)}>
                    Quick ideas <i>~3 s</i>
                  </button>
                  <button type="button" className={deep ? 'is-on' : ''} onClick={() => setDeep(true)} disabled={writer?.mode !== 'model'}>
                    Think deeply <i>~45 s</i>
                  </button>
                </div>
                <button type="button" className="rl__evolve" disabled={busy} onClick={() => void evolve('evolve', [], deep)}>
                  {state.busyWith === 'evolve' ? 'Writing…' : 'Write the next round'}
                </button>
                <button type="button" className="rl__ghost" disabled={busy || auto > 0} onClick={() => setAuto(3)}>
                  {auto > 0 ? `${auto} more…` : 'Run 3 rounds'}
                </button>
                <span className="rl__barsep" aria-hidden="true" />
                {partner && focus ? (
                  <button type="button" className="rl__ghost is-ember" disabled={busy} onClick={() => void evolve('breed', [focus.id, partner], deep)}>
                    Breed {focus.id} × {partner}
                  </button>
                ) : (
                  <button
                    type="button"
                    className={`rl__ghost${breeding ? ' is-armed' : ''}`}
                    disabled={busy || !focus}
                    onClick={() => setBreeding((b) => !b)}
                    title="Choose a second rule on the chart"
                  >
                    {breeding ? `Now click a rule to breed with ${focus?.id}` : 'Breed two rules'}
                  </button>
                )}
                <button type="button" className="rl__ghost" disabled={busy || !focus || focus.status !== 'scored'} onClick={() => focus && void evolve('push', [focus.id], deep)}>
                  Improve {focus?.id ?? 'a rule'}
                </button>
              </div>

              <Ticker lines={state.log} notice={state.notice} error={state.error} />
            </>
          )}
        </section>

        {/* --------------------------------------------------- right: the rule */}
        <aside className="rl__right">
          {focus ? (
            <div className="rl__rule" key={focus.id}>
              <p className={`rl__origin is-${focus.origin}`}>
                <i aria-hidden="true" />
                {focus.origin === 'people'
                  ? 'Written by people'
                  : focus.origin === 'you'
                    ? 'Your rule'
                    : focus.origin === 'model'
                      ? `Written by ${modelName(focus.author)} · round ${focus.generation}`
                      : `Random mutation · round ${focus.generation}`}
                <span className="rl__rid">{focus.id}</span>
              </p>

              <div className="rl__formula">
                {focus.status === 'proposed' ? (
                  <code className="rl__raw">{focus.formula}</code>
                ) : (
                  <Formula tree={focus.tree} clues={clues} size={focus.size > 18 ? 'md' : 'lg'} />
                )}
              </div>
              {focus.idea && focus.origin !== 'you' ? <p className="rl__idea">“{focus.idea}”</p> : null}

              <div className="rl__score">
                <span className="rl__big num">{focusMean === null ? '—' : focusMean.toFixed(1)}<small>%</small></span>
                <span className="rl__scorewords">
                  {focus.status === 'scored' ? 'of the week’s damage prevented, on average' : `testing · ${Object.keys(focus.live).length} of ${totalScenarios} scenarios`}
                  {focus.status === 'scored' && peopleBest !== null && focus.origin !== 'people' ? (
                    <b className={focus.mean > peopleBest ? 'is-up' : 'is-down'}>
                      {focus.mean > peopleBest ? '+' : ''}
                      {(focus.mean - peopleBest).toFixed(1)} points vs the best rule people wrote
                    </b>
                  ) : null}
                  {focus.status === 'scored' && totalScenarios ? (
                    <b className="is-neutral">beats Keystone’s search in {focus.wins} of {totalScenarios}</b>
                  ) : null}
                </span>
              </div>

              <ScenarioGrid
                scenarios={scenarios}
                budgets={state.header?.budgets ?? []}
                values={focusValues}
                search={state.search}
                tone={TONE[focus.origin]}
                active={activeCell}
                onPick={(key) => setActiveCell((c) => (c === key ? null : key))}
              />
              <p className="rl__legend"><i className="is-bar" /> this rule <i className="is-tick" /> Keystone’s search, same money</p>

              {focusCell ? (
                <div className="rl__picks">
                  <p>
                    With {money(scenarios.find((s) => s.key === focusCell.key)?.budget)}, the rule funds
                  </p>
                  <ul>
                    {focusCell.picks.map(([m, a]) => (
                      <li key={m}><b>{m}</b><span className="num">{money(a)}</span></li>
                    ))}
                  </ul>
                  {searchCell ? (
                    <p className="rl__vs">
                      The search funded {searchCell.picks.length ? searchCell.picks.map(([m, a]) => `${m} (${money(a)})`).join(', ') : 'nobody'} and
                      prevented {searchCell.pct.toFixed(1)}%, in {(searchCell.ms / 1000).toFixed(1)} s.
                    </p>
                  ) : null}
                </div>
              ) : null}

              {focus.parents.length ? (
                <p className="rl__parents">
                  written from{' '}
                  {focus.parents.map((p) => (
                    <button key={p} type="button" onClick={() => setSelected(p)}>{p}</button>
                  ))}
                </p>
              ) : null}

              {round?.reasoning ? (
                <details className="rl__mind">
                  <summary>What the model was thinking in round {round.generation}</summary>
                  <p>{round.reasoning}</p>
                </details>
              ) : null}

              {focus.test ? <Verdict rule={focus} /> : null}

              {focus.status === 'scored' ? (
                <div className="rl__acts">
                  <button type="button" className="rl__ghost" disabled={busy} onClick={() => void test([focus.id, ...(best && best.id !== focus.id ? [best.id] : [])])}>
                    {state.busyWith === 'test' ? 'Testing…' : 'Final test: failures it never saw'}
                  </button>
                  <button type="button" className="rl__pay" disabled={busy} onClick={() => setPaying(true)}>
                    Use this rule · pay with PayPal <span aria-hidden="true">→</span>
                  </button>
                </div>
              ) : null}
            </div>
          ) : (
            <div className="rl__empty">
              <p className="rl__eyebrow">The rules people wrote</p>
              <ul className="rl__people">
                {setup?.people.map((p) => (
                  <li key={p.formula}>
                    <code>{p.formula}</code>
                    <span>{p.idea}</span>
                  </li>
                ))}
              </ul>
              <p className="rl__hint">Each will be replayed on every scenario before the model writes a single rule.</p>
            </div>
          )}
        </aside>
      </div>

      </div>

      {paying && focus && state.header && setup ? (
        <UseSheet
          labId={state.header.lab_id}
          rule={focus}
          clues={clues}
          pool={setup.pool}
          budgets={state.header.budgets}
          onClose={() => setPaying(false)}
        />
      ) : null}

    </div>,
    document.body,
  )
}

/* ------------------------------------------------------------------ parts */

function Tip({ rule, at, clues }: { rule: LiveRule; at: { x: number; y: number }; clues: Clue[] }) {
  const mean = runningMean(rule)
  return (
    <div className="rl__tip" style={{ left: at.x, top: at.y }}>
      <span className="rl__tipid">{rule.id} · {rule.origin === 'model' ? modelName(rule.author) : rule.origin === 'mutation' ? 'mutation' : rule.origin === 'you' ? 'yours' : 'people'}</span>
      {rule.status === 'proposed' ? <code>{rule.formula}</code> : <Formula tree={rule.tree} clues={clues} size="sm" />}
      {rule.idea ? <em>{rule.idea}</em> : null}
      <b className="num">{mean === null ? 'testing…' : `${mean.toFixed(1)}%`}</b>
    </div>
  )
}

function Writing({ thinking }: { thinking: NonNullable<ReturnType<typeof useLab>['state']['thinking']> }) {
  const who = thinking.writer === 'model' ? modelName(thinking.model) : 'Random mutation'
  const what =
    thinking.mode === 'breed'
      ? `is breeding ${thinking.parents.join(' and ')}`
      : thinking.mode === 'push'
        ? `is improving ${thinking.parents[0]}`
        : `is reading ${thinking.parents.length} rules’ scores on every scenario`
  return (
    <div className="rl__writing">
      <i aria-hidden="true" />
      <span>
        <b>{who}</b> {what}
        {thinking.deep ? ' and thinking it through' : ''}…
      </span>
    </div>
  )
}

function Ticker({ lines, notice, error }: { lines: { at: number; text: string; tone?: string }[]; notice: string | null; error: string | null }) {
  const shown = lines.slice(-3)
  return (
    <div className="rl__ticker" aria-live="polite">
      {error ? <p className="is-warn">{error}</p> : notice ? <p className="is-warn">{notice}</p> : null}
      {shown.map((l, i) => (
        <p key={`${l.at}-${i}`} className={l.tone ? `is-${l.tone}` : ''}>
          <time className="num">{new Date(l.at).toLocaleTimeString([], { hour12: false })}</time>
          {l.text}
        </p>
      ))}
    </div>
  )
}

function Verdict({ rule }: { rule: LiveRule }) {
  const t = rule.test!
  const speed = t.decide_ms > 0 ? Math.round(t.search_ms / t.decide_ms) : null
  const max = Math.max(t.mean, t.search_mean, 1)
  return (
    <div className="rl__verdict">
      <p className="rl__eyebrow">Final test · {t.n} scenarios no rule was trained on</p>
      <div className="rl__vrow">
        <span>{rule.id}</span>
        <span className="rl__vbar is-rule"><i style={{ width: `${(t.mean / max) * 100}%` }} /></span>
        <b className="num">{t.mean.toFixed(1)}%</b>
      </div>
      <div className="rl__vrow">
        <span>search</span>
        <span className="rl__vbar"><i style={{ width: `${(t.search_mean / max) * 100}%` }} /></span>
        <b className="num">{t.search_mean.toFixed(1)}%</b>
      </div>
      <p className="rl__vnote num">
        decides in {t.decide_ms < 0.01 ? '<0.01' : t.decide_ms.toFixed(2)} ms · the search takes {(t.search_ms / 1000).toFixed(1)} s
        {speed ? ` · ${speed.toLocaleString('en-US')}× faster` : ''} · beats it in {t.wins} of {t.n}
      </p>
    </div>
  )
}
