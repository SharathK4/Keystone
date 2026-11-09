/**
 * From a rule to money moving.
 *
 * Choose a failure and a fund; the rule decides whom to fund in well under a
 * millisecond, the plan is replayed against Keystone's search on the same
 * money, and the payouts go to the same PayPal approval desk as every plan:
 * priced at the declared demo scale, signed by a person, paid at most once.
 */

import { useState } from 'react'
import { ApiError } from '../api/client'
import { Settlement } from '../components/Settlement'
import { money } from '../lib/format'
import { lab, type Clue, type PoolMerchant, type UseResult } from './api'
import { Formula } from './Formula'
import type { LiveRule } from './useLab'

interface Props {
  labId: string
  rule: LiveRule
  clues: Clue[]
  pool: PoolMerchant[]
  budgets: number[]
  onClose: () => void
}

export function UseSheet({ labId, rule, clues, pool, budgets, onClose }: Props) {
  const [failing, setFailing] = useState(pool[0]?.merchant_id ?? '')
  const [budget, setBudget] = useState(budgets[budgets.length - 1] ?? 0)
  const [result, setResult] = useState<UseResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const decide = () => {
    setBusy(true)
    setError(null)
    setResult(null)
    lab
      .use(labId, rule.id, failing, budget)
      .then(setResult)
      .catch((e: unknown) => setError(e instanceof ApiError ? e.message : 'Could not reach the lab.'))
      .finally(() => setBusy(false))
  }

  const better = result ? result.pct - result.search.pct : 0

  return (
    <div className="us" role="dialog" aria-modal="true" aria-label="Use this rule with PayPal">
      <button className="us__scrim" onClick={onClose} aria-label="Close" />
      <div className="us__sheet">
        <header className="us__head">
          <div>
            <p className="us__eyebrow">From rule to payout</p>
            <h2>Let {rule.id} decide a rescue</h2>
          </div>
          <button className="us__close" onClick={onClose} aria-label="Close">×</button>
        </header>

        <div className="us__rule">
          <Formula tree={rule.tree} clues={clues} size="md" />
        </div>

        <div className="us__pick">
          <label>
            <span>Who misses a payment</span>
            <select value={failing} onChange={(e) => { setFailing(e.target.value); setResult(null) }}>
              {pool.map((p) => (
                <option key={p.merchant_id} value={p.merchant_id}>
                  {p.merchant_id} · misses {money(p.missed)}
                </option>
              ))}
            </select>
          </label>
          <div>
            <span>Rescue fund</span>
            <div className="us__chips">
              {budgets.map((b) => (
                <button key={b} type="button" className={b === budget ? 'is-on' : ''} onClick={() => { setBudget(b); setResult(null) }}>
                  {money(b)}
                </button>
              ))}
            </div>
          </div>
          <button type="button" className="us__go" disabled={busy || !failing} onClick={decide}>
            {busy ? 'Replaying the week…' : 'Let the rule decide'}
          </button>
        </div>

        {error ? <p className="us__err">{error}</p> : null}

        {result ? (
          <>
            <div className="us__result">
              <div className="us__col">
                <p className="us__label">{rule.id} funds <em>· decided in {result.decide_ms < 0.01 ? '<0.01' : result.decide_ms.toFixed(2)} ms</em></p>
                <ul>
                  {result.picks.map((p) => (
                    <li key={p.merchant_id}><b>{p.merchant_id}</b><span className="num">{money(p.amount)}</span></li>
                  ))}
                </ul>
                <p className="us__pct num">{result.pct.toFixed(1)}% <small>of the week’s damage prevented</small></p>
              </div>
              <div className="us__col is-search">
                <p className="us__label">Keystone’s search <em>· {(result.search.ms / 1000).toFixed(1)} s</em></p>
                <ul>
                  {result.search.picks.length ? (
                    result.search.picks.map((p) => (
                      <li key={p.merchant_id}><b>{p.merchant_id}</b><span className="num">{money(p.amount)}</span></li>
                    ))
                  ) : (
                    <li><b>nobody</b><span>no whole gap fits the fund</span></li>
                  )}
                </ul>
                <p className="us__pct num">{result.search.pct.toFixed(1)}% <small>same money</small></p>
              </div>
            </div>
            <p className={`us__delta${better > 0.05 ? ' is-up' : better < -0.05 ? ' is-down' : ''}`}>
              {better > 0.05
                ? `The rule prevents ${better.toFixed(1)} points more than the search, with the same money.`
                : better < -0.05
                  ? `Here the search does ${(-better).toFixed(1)} points better; the rule is still yours to pay.`
                  : 'The rule and the search do the same here.'}
            </p>
            <div className="us__desk">
              <p className="us__eyebrow">PayPal · a person approves, PayPal pays</p>
              <Settlement scenarioId={result.plan_ref} />
            </div>
          </>
        ) : null}
      </div>
    </div>
  )
}
