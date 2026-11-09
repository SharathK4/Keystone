/**
 * The rule lab.
 *
 * One question - a merchant misses a payment and there is a fixed fund: who do
 * you fund first? - and one race to answer it. The six rules people wrote are
 * replayed first. Then the AI writes its own, each is replayed on every test
 * scenario as you watch, and the board re-ranks. The AI's best rule is shown in
 * full: what it says, what it prevents, how it does on failures it never saw,
 * whom it funds on the network, and a button that pays that rescue with PayPal.
 *
 * Nothing here is precomputed. Every score is a replay of the week, run for
 * this lab or reused from an identical one.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { DependencyView, MerchantView } from '../api/types'
import { money } from '../lib/format'
import { lab, type Clue, type UseResult } from './api'
import { Formula } from './Formula'
import { PaySheet } from './PaySheet'
import { Race } from './Race'
import { useLab } from './useLab'
import { WinnerRing } from './WinnerRing'
import './rule-lab.css'

interface Props {
  merchants: MerchantView[]
  dependencies: DependencyView[]
  onClose: () => void
}

export function RuleLab({ merchants, dependencies, onClose }: Props) {
  const { state, start, evolve, test } = useLab()
  const [clues, setClues] = useState<Clue[]>([])
  const [used, setUsed] = useState<UseResult | null>(null)
  const [paying, setPaying] = useState(false)
  const [pending, setPending] = useState(0)
  const begun = useRef(false)
  const asked = useRef<Set<string>>(new Set())
  const shell = useRef<HTMLDivElement>(null)
  const fit = useRef<HTMLDivElement>(null)

  // Open straight into the race: the people's rules are replayed first.
  useEffect(() => {
    lab
      .setup()
      .then((s) => {
        setClues(s.clues)
        if (begun.current) return
        begun.current = true
        void start({
          training: s.pool.filter((p) => p.training).map((p) => p.merchant_id),
          budgets: s.budgets,
          k: s.k,
          banned: [],
          your_rule: null,
        })
      })
      .catch(() => undefined)
  }, [start])

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

  const rules = useMemo(() => state.order.map((id) => state.rules[id]).filter(Boolean), [state.order, state.rules])
  const scored = rules.filter((r) => r.status === 'scored')
  const machine = scored
    .filter((r) => r.origin === 'model' || r.origin === 'mutation')
    .sort((a, b) => b.mean - a.mean || a.size - b.size)
  const best = machine[0] ?? null
  const people = scored.filter((r) => r.origin === 'people')
  const peopleBest = people.length ? Math.max(...people.map((r) => r.mean)) : null
  const leader = [...scored].sort((a, b) => b.mean - a.mean || a.size - b.size)[0] ?? null
  const ready = state.phase === 'ready'
  const busy = state.phase === 'busy' || state.phase === 'starting'
  const labId = state.header?.lab_id ?? null
  const failing = state.header?.training[0] ?? null
  const fund = state.header ? Math.max(...state.header.budgets) : null

  // One click writes up to three rounds, and stops early once the AI's best
  // rule clearly leads both people and Keystone's search.
  const leads = best !== null && state.searchMean !== null && peopleBest !== null
    && best.mean >= Math.max(state.searchMean, peopleBest) + 3
  const more = leads ? 0 : pending
  useEffect(() => {
    if (!ready || more <= 0) return
    const timer = window.setTimeout(() => {
      setPending((n) => n - 1)
      void evolve('evolve', [], false)
    }, 500)
    return () => window.clearTimeout(timer)
  }, [ready, more, evolve])

  // Once the AI has a best rule: test it on failures it never saw, then let it
  // decide one real rescue (the picture and the PayPal plan).
  useEffect(() => {
    if (!ready || more > 0 || !best || !labId || !failing || !fund) return
    const key = best.id
    if (!best.test && !asked.current.has(`test:${key}`)) {
      asked.current.add(`test:${key}`)
      void test([key])
      return
    }
    if (best.test && used?.rule_id !== key && !asked.current.has(`use:${key}`)) {
      asked.current.add(`use:${key}`)
      lab.use(labId, key, failing, fund).then(setUsed).catch(() => undefined)
    }
  }, [ready, more, best, labId, failing, fund, test, used])

  // A long rule is scaled to the column rather than clipped.
  useLayoutEffect(() => {
    const box = fit.current
    const inner = box?.firstElementChild as HTMLElement | null
    if (!box || !inner) return
    inner.style.transform = ''
    const ratio = box.clientWidth / inner.scrollWidth
    if (ratio < 1) inner.style.transform = `scale(${ratio.toFixed(3)})`
  }, [best?.id])

  const lastRound = state.rounds[state.rounds.length - 1]
  const mutation = lastRound?.author === 'random mutation'
  const testing = rules.some((r) => r.status !== 'scored')
  const status = state.thinking
    ? 'The AI is writing new rules…'
    : testing
      ? 'Replaying the week for every new rule…'
      : state.busyWith === 'test'
        ? 'Testing the best rule on failures it never saw…'
        : state.generation > 0
          ? `${state.generation} round${state.generation === 1 ? '' : 's'} written`
          : state.searchMean === null
            ? 'Replaying the week for each rule people wrote…'
            : 'Six rules people wrote. Can the AI beat them?'

  const beatsAll = best && peopleBest !== null && state.searchMean !== null
    && best.mean > peopleBest && best.mean > state.searchMean

  return createPortal(
    <div className="rl" role="dialog" aria-modal="true" aria-label="The rule lab">
      <button className="rl__scrim" onClick={onClose} aria-label="Close" tabIndex={-1} />
      <div className="rl__shell" ref={shell} tabIndex={-1}>
        <header className="rl__head">
          <div>
            <h2 className="rl__title">The rule lab</h2>
            <p className="rl__sub">
              A merchant misses a payment, and there is a fixed fund. Who do you fund first?
            </p>
          </div>
          <button className="rl__close" onClick={onClose} aria-label="Close the lab">
            <span aria-hidden="true" />
          </button>
        </header>

        <section className="rl__race">
          <Race rules={rules} searchMean={state.searchMean} clues={clues} bestId={leader?.id ?? null} writing={Boolean(state.thinking)} />
          <div className="rl__go">
            <button type="button" className="rl__write" disabled={busy || more > 0 || !state.header}
              onClick={() => {
                setPending(2)
                void evolve('evolve', [], false)
              }}
            >
              {state.generation === 0 ? 'Let the AI write its rules' : 'Let it write another round'}
              <span aria-hidden="true">→</span>
            </button>
            <span className={`rl__status${busy ? ' is-busy' : ''}`}>{status}</span>
          </div>
          {mutation ? (
            <p className="rl__note">No language model is connected here, so random mutation wrote this round.</p>
          ) : null}
          {state.error ? <p className="rl__note">{state.error}</p> : null}
        </section>

        {best ? (
          <section className={`rl__win${beatsAll ? ' is-winning' : ''}`} key={best.id}>
            <div className="rl__wintext">
              <p className="rl__eyebrow">{beatsAll ? 'The rule the AI wrote' : 'The AI’s best rule so far'}</p>
              <div className="rl__winformula" ref={fit}>
                <Formula tree={best.tree} clues={clues} size={best.size <= 10 ? 'xl' : best.size <= 18 ? 'lg' : 'md'} />
              </div>
              {best.idea ? <p className="rl__idea">“{best.idea.replace(/\.$/, '')}.”</p> : null}

              <dl className="rl__nums">
                <div>
                  <dt>of the week’s damage prevented</dt>
                  <dd className="num is-ai">{best.mean.toFixed(1)}%</dd>
                </div>
                {state.searchMean !== null ? (
                  <div>
                    <dt>Keystone’s own search, same money</dt>
                    <dd className="num">{state.searchMean.toFixed(1)}%</dd>
                  </div>
                ) : null}
                {peopleBest !== null ? (
                  <div>
                    <dt>best rule people wrote</dt>
                    <dd className="num">{peopleBest.toFixed(1)}%</dd>
                  </div>
                ) : null}
              </dl>

              <p className="rl__unseen">
                {best.test ? (
                  <>
                    On failures it never saw: <b className="num">{best.test.mean.toFixed(1)}%</b> against the search’s{' '}
                    <b className="num">{best.test.search_mean.toFixed(1)}%</b>, better in {best.test.wins} of {best.test.n}.
                    It decides in under a millisecond; the search takes {(best.test.search_ms / 1000).toFixed(1)} s.
                  </>
                ) : (
                  'Testing it on failures it never saw…'
                )}
              </p>

              <button type="button" className="rl__pay" disabled={!used} onClick={() => setPaying(true)}>
                Pay this rescue with PayPal <span aria-hidden="true">→</span>
              </button>
            </div>

            <figure className="rl__ring">
              {used ? (
                <>
                  <WinnerRing
                    merchants={merchants}
                    dependencies={dependencies}
                    failing={used.failing}
                    rule={used.picks.map((p) => p.merchant_id)}
                    search={used.search.picks.map((p) => p.merchant_id)}
                  />
                  <figcaption>
                    When <b>{used.failing}</b> misses {money(used.missed)} and there is {money(used.budget)}: the rule funds{' '}
                    <span className="is-ai">{used.picks.map((p) => p.merchant_id).join(', ')}</span>; the search funds{' '}
                    <span className="is-search">{used.search.picks.map((p) => p.merchant_id).join(', ') || 'nobody'}</span>.
                  </figcaption>
                </>
              ) : (
                <div className="rl__ringwait" />
              )}
            </figure>
          </section>
        ) : null}
      </div>

      {paying && used && best ? (
        <PaySheet result={used} tree={best.tree} clues={clues} onClose={() => setPaying(false)} />
      ) : null}
    </div>,
    document.body,
  )
}
