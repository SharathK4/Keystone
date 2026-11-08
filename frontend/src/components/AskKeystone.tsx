/**
 * Ask Keystone: one question box, and the work shown as it happens.
 *
 * Each line of the trace is a real tool call on the server - finding the
 * merchant, replaying the week without its payment, searching for the cheapest
 * rescue, checking a delay, pricing the PayPal payout - with its measured time.
 * The answer is two or three sentences whose numbers were checked against those
 * calls. The cards below redraw around the answer; paying opens the approval.
 */

import { useEffect, useState, type FormEvent } from 'react'
import { api } from '../api/client'
import type { AgentRun } from '../api/useAgent'
import './ask-keystone.css'

const SUGGESTIONS = [
  'What if m0038 misses a payment this week?',
  'What if our most important merchant can’t pay?',
  'm0038 misses a payment and our money lands 2 days late',
]

interface Props {
  run: AgentRun | null
  onAsk: (question: string) => void
  onClear: () => void
  onPay: () => void
}

interface Status {
  mode: string
  model: string | null
}

/** "claude-opus-5-5" -> "Claude Opus 5.5". */
function modelName(id: string | null | undefined): string {
  if (!id) return 'rules'
  const m = id.replace(/^anthropic\//, '').match(/^claude-([a-z]+)-(\d+)(?:[-.](\d+))?/)
  if (m) {
    return `Claude ${m[1].charAt(0).toUpperCase()}${m[1].slice(1)} ${m[2]}${m[3] ? `.${m[3]}` : ''}`
  }
  // Another model through OpenRouter: "nvidia/nemotron-3.5-lightning:free".
  const name = id.split('/').pop() ?? id
  const free = name.endsWith(':free')
  const words = name
    .replace(/:free$/, '')
    .split('-')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ')
  return `${words}${free ? ' (free)' : ''} via OpenRouter`
}

const seconds = (ms?: number) =>
  ms === undefined ? '' : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`

export function AskKeystone({ run, onAsk, onClear, onPay }: Props) {
  const [question, setQuestion] = useState('')
  const [status, setStatus] = useState<Status | null>(null)

  useEffect(() => {
    let live = true
    api
      .agentStatus()
      .then((s) => live && setStatus(s))
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [])

  const busy = run?.phase === 'running'
  const submit = (e: FormEvent) => {
    e.preventDefault()
    const q = question.trim()
    if (q.length >= 3 && !busy) onAsk(q)
  }

  return (
    <section className="ak" aria-label="Ask Keystone">
      <form className="ak__bar" onSubmit={submit}>
        <label className="sr-only" htmlFor="ak-q">
          Ask Keystone
        </label>
        <input
          id="ak-q"
          className="ak__input"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask: what if a merchant misses a payment?"
          maxLength={400}
          autoComplete="off"
        />
        <button className="ak__go" type="submit" disabled={busy || question.trim().length < 3}>
          {busy ? 'Working' : 'Ask'}
        </button>
      </form>

      {!run ? (
        <div className="ak__chips">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              type="button"
              className="ak__chip"
              onClick={() => {
                setQuestion(s)
                onAsk(s)
              }}
            >
              {s}
            </button>
          ))}
          {status ? (
            <span className="ak__who">
              {status.mode === 'claude'
                ? `answers by ${modelName(status.model)}, using Keystone’s tools`
                : 'no language model on this server: the same tools run by rules'}
            </span>
          ) : null}
        </div>
      ) : (
        <div className="ak__run" aria-live="polite">
          <p className="ak__q">“{run.question}”</p>
          <ol className="ak__steps">
            {run.steps.map((s) => (
              <li key={s.id} className={`ak__step is-${s.status}`}>
                <i aria-hidden="true">{s.status === 'running' ? '◌' : s.status === 'error' ? '×' : '✓'}</i>
                <span className="ak__label">{s.label}</span>
                {s.summary ? <span className="ak__sum">{s.summary}</span> : null}
                <span className="ak__ms num">{seconds(s.ms)}</span>
              </li>
            ))}
            {busy && run.steps.every((s) => s.status !== 'running') ? (
              <li className="ak__step is-running">
                <i aria-hidden="true">◌</i>
                <span className="ak__label">Thinking</span>
              </li>
            ) : null}
          </ol>

          {run.notice ? <p className="ak__notice">{run.notice}</p> : null}
          {run.error ? <p className="ak__notice">{run.error}</p> : null}

          {run.answer ? (
            <p className="ak__answer">{run.answer.text}</p>
          ) : null}

          <div className="ak__foot">
            {run.payment && run.payment.payouts.some((p) => p.payable) ? (
              <button type="button" className="ak__pay" onClick={onPay}>
                Review and pay with PayPal
                <span className="num">{run.payment.total}</span>
              </button>
            ) : run.payment ? (
              <span className="ak__meta">
                {run.payment.payouts[0]?.reason ?? 'nothing in this plan can be paid'}
              </span>
            ) : null}
            {run.answer ? (
              <span className="ak__meta">
                {run.answer.source === 'rules'
                  ? 'worded by rules, no language model'
                  : run.answer.source === 'template'
                    ? 'model answer failed the number check; shown from the tools'
                    : `worded by ${modelName(run.answer.source)} · every number checked`}
                {run.ms ? ` · ${seconds(run.ms)}` : ''}
              </span>
            ) : null}
            {run.phase !== 'running' ? (
              <button type="button" className="ak__clear" onClick={onClear}>
                Ask another
              </button>
            ) : null}
          </div>
        </div>
      )}
    </section>
  )
}
