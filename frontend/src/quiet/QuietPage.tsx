/**
 * The walk: one page, quiet scenes, one idea each.
 *
 * Nothing here moves on its own. A scene changes because something happened -
 * the reader fired the PayPal event, asked a what-if, picked a delay - and
 * every figure in a sentence is read from the response that caused it. The
 * five-card gallery is one link away as "Evidence".
 */

import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { ApiError, api } from '../api/client'
import type {
  DisplayView,
  ProofView,
  ShockRequest,
  WalkAskView,
  WalkFireView,
  WalkSpeedView,
  WalkView,
} from '../api/schema'
import type { MerchantView, ScenarioSnapshot } from '../api/types'
import { money, setMoneyDisplay, type Currency } from '../lib/format'
import { DotField, type Mark } from './DotField'
import { WaitingChart } from './WaitingChart'
import { clock, moreFeelIt, seconds, spokenHours, stepsOut } from './words'
import './quiet.css'

const CURRENCY_KEY = 'keystone.currency'

function storedCurrency(): Currency | null {
  try {
    const v = window.localStorage.getItem(CURRENCY_KEY)
    return v === 'USD' || v === 'INR' ? v : null
  } catch {
    return null
  }
}

interface WhatIf {
  request: ShockRequest
  ask: WalkAskView
  analysis: ScenarioSnapshot | null
  error: string | null
}

export function QuietPage() {
  const [walk, setWalk] = useState<WalkView | null>(null)
  const [merchants, setMerchants] = useState<MerchantView[] | null>(null)
  const [display, setDisplay] = useState<DisplayView | null>(null)
  const [speed, setSpeed] = useState<WalkSpeedView | null>(null)
  const [proof, setProof] = useState<ProofView | null>(null)
  const [failure, setFailure] = useState<ApiError | null>(null)

  const [fired, setFired] = useState<WalkFireView | null>(null)
  const [firing, setFiring] = useState(false)
  const [fireError, setFireError] = useState<string | null>(null)

  const [question, setQuestion] = useState('')
  const [asking, setAsking] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)
  const [whatIf, setWhatIf] = useState<WhatIf | null>(null)

  const [delay, setDelay] = useState(0)
  const [chosenCurrency, setChosenCurrency] = useState<Currency | null>(() => storedCurrency())

  useEffect(() => {
    let live = true
    Promise.all([api.walk(), api.merchants(), api.display().catch(() => null)])
      .then(([w, m, d]) => {
        if (!live) return
        setWalk(w)
        setMerchants(m)
        setDisplay(d)
      })
      .catch((e: unknown) => live && setFailure(e instanceof ApiError ? e : null))
    api.walkSpeed().then((s) => live && setSpeed(s)).catch(() => undefined)
    api.proof().then((p) => live && setProof(p)).catch(() => undefined)
    return () => {
      live = false
    }
  }, [])

  // Money in the reader's currency, set before any scene renders a figure.
  const currency: Currency = display ? (chosenCurrency ?? display.currency) : 'INR'
  if (display) {
    setMoneyDisplay({
      currency,
      usdPerInr: Number(display.usd_per_inr),
      sandboxRatio: display.sandbox_ratio ? Number(display.sandbox_ratio) : null,
    })
  }
  const flipCurrency = () => {
    const next: Currency = currency === 'USD' ? 'INR' : 'USD'
    setChosenCurrency(next)
    try {
      window.localStorage.setItem(CURRENCY_KEY, next)
    } catch {
      // the choice lasts for this visit only
    }
  }

  const fire = () => {
    setFiring(true)
    setFireError(null)
    setWhatIf(null)
    api
      .walkFire()
      .then(setFired)
      .catch((e: unknown) => setFireError(e instanceof ApiError ? e.message : 'The event failed.'))
      .finally(() => setFiring(false))
  }

  const ask = (e: FormEvent) => {
    e.preventDefault()
    const text = question.trim()
    if (!text || asking) return
    setAsking(true)
    setRefusal(null)
    api
      .walkAsk(text)
      .then(async (parsed) => {
        if (!parsed.ok || !parsed.request) {
          setRefusal(parsed.refusal)
          return
        }
        const request = parsed.request
        setWhatIf({ request, ask: parsed, analysis: null, error: null })
        try {
          const analysis = await api.analyze(
            request.merchant_ids,
            request.magnitude_multiple,
            request.onset_hours,
          )
          setWhatIf({ request, ask: parsed, analysis, error: null })
        } catch (err) {
          setWhatIf({
            request,
            ask: parsed,
            analysis: null,
            error: err instanceof ApiError ? err.message : 'The analysis failed.',
          })
        }
      })
      .catch((err: unknown) => setRefusal(err instanceof ApiError ? err.message : null))
      .finally(() => setAsking(false))
  }

  // ---------------------------------------------------------------- derived
  const analysis: ScenarioSnapshot | null =
    whatIf?.analysis ?? ((fired?.analysis as unknown as ScenarioSnapshot | undefined) ?? null)
  const origins = useMemo(() => new Set(analysis?.shock.origin_merchants ?? []), [analysis])
  const others = useMemo(
    () =>
      (analysis?.affected_merchants ?? [])
        .filter((a) => !origins.has(a.merchant_id))
        .sort(
          (a, b) =>
            (a.time_to_constraint_hours ?? Infinity) - (b.time_to_constraint_hours ?? Infinity),
        ),
    [analysis, origins],
  )

  // Not memoised: money() reads the currency on show, so a switch must redraw it.
  const hitMarks = new Map<string, Mark>()
  if (fired) {
    hitMarks.set(fired.shock.merchant_id, {
      state: 'hit',
      label: `${fired.shock.merchant_id}, missed ${money(fired.shock.missing_inr)}`,
    })
  }

  const reachMarks = useMemo(() => {
    const marks = new Map<string, Mark>()
    for (const id of origins) {
      marks.set(id, { state: 'hit', label: `${id}, where the payment failed` })
    }
    for (const a of others) {
      const when = a.time_to_constraint_hours
      marks.set(a.merchant_id, {
        state: 'reached',
        label:
          `${a.merchant_id}, short ${when !== null ? spokenHours(when) : 'later'} later` +
          (a.cascade_depth ? `, ${stepsOut(a.cascade_depth)}` : ''),
      })
    }
    return marks
  }, [origins, others])

  const first = others[0]
  const kept0 = speed?.delays[0]?.as_chosen.payments_kept_on_time_inr
  const atDelay = speed?.delays.find((r) => r.delay_hours === delay)
  const failedFirst = proof?.failed[0]

  if (failure) {
    return (
      <main className="q">
        <section className="q-scene">
          <div className="q-col">
            <h1 className="q-head">The service is not answering.</h1>
            <p className="q-sentence">{failure.message}</p>
            <p className="q-stamps">
              <span className="q-stamp">{failure.path}</span>
            </p>
          </div>
        </section>
      </main>
    )
  }

  return (
    <main className="q">
      <header className="q-top">
        <a className="q-wordmark" href="#s1">
          Keystone
        </a>
        {display ? (
          <button
            type="button"
            className="q-currency"
            onClick={flipCurrency}
            aria-label={currency === 'USD' ? 'Show amounts in rupees' : 'Show amounts in US dollars'}
          >
            <span className={currency === 'USD' ? 'is-on' : ''}>USD</span>
            <span aria-hidden="true"> · </span>
            <span className={currency === 'INR' ? 'is-on' : ''}>INR</span>
          </button>
        ) : null}
      </header>

      <nav className="q-ticks" aria-label="Scenes">
        {['s1', 's2', 's3', 's4', 's5', 's6'].map((id, i) => {
          const built = id !== 's4' && id !== 's5'
          return built ? (
            <a key={id} className="q-tick" href={`#${id}`} aria-label={`Scene ${i + 1}`} />
          ) : (
            <span key={id} className="q-tick is-later" aria-hidden="true" />
          )
        })}
      </nav>

      {/* ------------------------------------------------ 1  one payment fails */}
      <section className="q-scene" id="s1" aria-labelledby="s1-h">
        <div className="q-col">
          <h1 className="q-head" id="s1-h">
            One payment fails.
          </h1>
          <p className="q-sentence" aria-live="polite">
            {fired
              ? `${fired.shock.merchant_id} missed ${money(fired.shock.missing_inr)}.`
              : walk
                ? `${walk.merchant_id} is owed ${money(walk.missing_inr)} through PayPal.`
                : ' '}
          </p>
        </div>
        <div className="q-focal">
          {merchants ? (
            <DotField
              merchants={merchants}
              marks={hitMarks}
              summary={
                fired
                  ? `${merchants.length} merchants; ${fired.shock.merchant_id} missed its payment`
                  : `${merchants.length} merchants, none affected yet`
              }
            />
          ) : null}
        </div>
        <div className="q-control">
          {!fired ? (
            <button
              type="button"
              className="q-btn"
              onClick={fire}
              disabled={firing || !walk || walk.mode === 'unavailable'}
              aria-busy={firing}
            >
              {firing ? 'Processing the PayPal event' : 'Fire a PayPal event'}
            </button>
          ) : null}
          {fireError ? <p className="q-note">{fireError}</p> : null}
        </div>
        <p className="q-stamps">
          {fired ? (
            <>
              <span className="q-stamp">
                {fired.event.stamp} · {clock(fired.event.processed_at)}
              </span>
              <span className="q-stamp">signature {fired.event.verification}</span>
              <span className="q-stamp">computed now, {seconds(fired.computed_in_ms)}</span>
            </>
          ) : walk ? (
            <span className="q-stamp">
              {walk.mode === 'recorded'
                ? 'a recorded PayPal delivery, replayed through the live checks'
                : walk.mode === 'development'
                  ? 'development: events are signed locally, not by PayPal'
                  : walk.mode_reason}
            </span>
          ) : null}
        </p>
      </section>

      {/* ------------------------------------------------- 2  who feels it */}
      <section className="q-scene" id="s2" aria-labelledby="s2-h">
        <div className="q-col">
          <h2 className="q-head" id="s2-h">
            {analysis ? moreFeelIt(others.length) : 'Then who feels it?'}
          </h2>
          <p className="q-sentence" aria-live="polite">
            {!analysis
              ? 'Fire the event above first.'
              : first && first.time_to_constraint_hours !== null
                ? `${first.merchant_id} runs short ${spokenHours(first.time_to_constraint_hours)} later` +
                  (first.cascade_depth ? `, ${stepsOut(first.cascade_depth)}.` : '.')
                : `The shortfall stays with ${analysis.shock.origin_merchants.join(' and ')}.`}
          </p>
        </div>
        <div className="q-focal">
          {merchants && analysis ? (
            <DotField
              merchants={merchants}
              marks={reachMarks}
              summary={`${merchants.length} merchants; ${analysis.shock.origin_merchants.join(', ')} hit; ${others.map((o) => o.merchant_id).join(', ') || 'no one else'} reached`}
            />
          ) : (
            <div className="q-field-placeholder" aria-hidden="true" />
          )}
        </div>
        <form className="q-control" onSubmit={ask}>
          <label className="sr-only" htmlFor="q-ask">
            Ask a what-if about this failure
          </label>
          <input
            id="q-ask"
            className="q-input"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="Ask: what if it were twice as large?"
            maxLength={280}
            disabled={!fired || asking}
            autoComplete="off"
          />
          {whatIf ? (
            <p className="q-chip-row">
              <span className="q-chip">
                {whatIf.request.merchant_ids.join(' + ')} · {whatIf.request.magnitude_multiple}× cushion ·
                hour {whatIf.request.onset_hours}
              </span>
              <button
                type="button"
                className="q-chip-clear"
                onClick={() => {
                  setWhatIf(null)
                  setQuestion('')
                }}
                aria-label="Back to the PayPal event"
              >
                ×
              </button>
            </p>
          ) : null}
          {refusal ? <p className="q-note">{refusal}</p> : null}
          {whatIf?.error ? <p className="q-note">{whatIf.error}</p> : null}
        </form>
        <p className="q-stamps">
          {whatIf ? (
            <>
              <span className="q-stamp">
                {whatIf.analysis
                  ? `computed now, ${seconds(whatIf.analysis.computed_in_ms)}`
                  : 'computing now'}
              </span>
              <span className="q-stamp">
                {whatIf.ask.provider === 'rules'
                  ? 'parsed by rules; no language model configured'
                  : `parsed by ${whatIf.ask.model ?? whatIf.ask.provider}`}
              </span>
            </>
          ) : fired ? (
            <span className="q-stamp">computed now, {seconds(fired.computed_in_ms)}</span>
          ) : null}
        </p>
      </section>

      {/* ---------------------------------------------- 3  every hour costs */}
      <section className="q-scene" id="s3" aria-labelledby="s3-h">
        <div className="q-col">
          <h2 className="q-head" id="s3-h">
            Every hour costs.
          </h2>
          <p className="q-sentence" aria-live="polite">
            {delay === 0 || !atDelay || kept0 === undefined
              ? 'The same fix, landing later, does less.'
              : `Landing ${spokenHours(delay)} late, it keeps ${money(atDelay.as_chosen.payments_kept_on_time_inr)} on time, not ${money(kept0)}.`}
          </p>
        </div>
        <div className="q-focal q-focal--chart">
          {speed ? <WaitingChart speed={speed} selected={delay} onSelect={setDelay} /> : null}
        </div>
        <p className="q-stamps">
          {speed ? (
            <>
              <span className="q-stamp">{speed.label.toLowerCase()}</span>
              <span className="q-stamp">{String(speed.provenance.snapshot_id ?? '')}</span>
            </>
          ) : null}
        </p>
      </section>

      {/* ------------------------------------------ 6  what we cannot promise */}
      <section className="q-scene" id="s6" aria-labelledby="s6-h">
        <div className="q-col">
          <h2 className="q-head" id="s6-h">
            What we cannot promise.
          </h2>
          <p className="q-sentence">{failedFirst ? `${failedFirst.title}.` : ' '}</p>
        </div>
        <div className="q-control">
          <a className="q-link" href="#proof">
            See everything that failed
          </a>
        </div>
        <p className="q-stamps">
          {proof ? <span className="q-stamp">{proof.label.toLowerCase()}</span> : null}
        </p>
      </section>

      <footer className="q-foot">
        <a className="q-link q-link--quiet" href="#evidence">
          Evidence
        </a>
      </footer>
    </main>
  )
}
