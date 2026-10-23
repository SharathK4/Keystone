/**
 * Approve and pay.
 *
 * The plan behind the offer, priced twice and never mixed: in analytic rupees,
 * and in PayPal sandbox dollars at a declared demo scale. Nothing moves until a
 * person reviews the exact plan (its hash), signs it, and asks for payment; the
 * server refuses anything else. What PayPal says afterwards is read on open or
 * on request, never polled, and a failed payout comes back as a new shock with
 * a new plan that needs its own signature.
 */

import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { ApiError, api, demoToken, setDemoToken } from '../api/client'
import type {
  ApprovalView,
  DemoStatusView,
  ExecutablePlanView,
  LedgerView,
  PayPalStatusView,
  ReentryView,
} from '../api/schema'
import { count, inr } from '../lib/format'
import { Seal } from './Seal'
import './settlement.css'

interface Props {
  scenarioId: string
}

interface Desk {
  plan: ExecutablePlanView
  paypal: PayPalStatusView
  demo: DemoStatusView
}

const usd = (value: string | null | undefined) =>
  value === null || value === undefined ? '—' : `$${Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

const MARK: Record<string, { sign: string; label: string }> = {
  planned: { sign: '○', label: 'recorded' },
  submitted: { sign: '…', label: 'submitted' },
  pending: { sign: '…', label: 'pending' },
  held: { sign: '◇', label: 'held by PayPal' },
  succeeded: { sign: '✓', label: 'paid' },
  failed: { sign: '†', label: 'failed' },
  unknown: { sign: '?', label: 'outcome unknown' },
}

export function Settlement({ scenarioId }: Props) {
  const [desk, setDesk] = useState<Desk | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [nonce, setNonce] = useState(0)
  const reread = useCallback(() => setNonce((n) => n + 1), [])

  useEffect(() => {
    let live = true
    Promise.all([api.plan(scenarioId), api.paypal(), api.demo()])
      .then(([plan, paypal, demo]) => live && setDesk({ plan, paypal, demo }))
      .catch((e: unknown) => live && setError(e instanceof ApiError ? e : null))
    return () => {
      live = false
    }
  }, [scenarioId])

  if (error) return <p className="st__missing">{error.message}</p>
  if (!desk) return <p className="st__missing">Opening the settlement desk…</p>
  return (
    <div className="st">
      <Status paypal={desk.paypal} demo={desk.demo} />
      <PlanTable plan={desk.plan} />
      <Steps plan={desk.plan} onPaid={reread} />
      <Ledger scenarioId={scenarioId} nonce={nonce} onChange={reread} />
      <Reentries scenarioId={scenarioId} nonce={nonce} />
    </div>
  )
}

/* ---------------------------------------------------------------- status */

function Status({ paypal, demo }: { paypal: PayPalStatusView; demo: DemoStatusView }) {
  const [token, setToken] = useState(demoToken())
  const state = paypal.refused
    ? { tone: 'off', text: `PayPal sandbox refused: ${paypal.refused}` }
    : !paypal.configured
      ? { tone: 'off', text: 'PayPal sandbox not configured on this server: the plan can be read and approved, not paid' }
      : { tone: 'on', text: 'PayPal sandbox connected: approved plans are paid as payouts' }
  return (
    <div className="st__status">
      <span className={`st__light st__light--${state.tone}`}>
        <i aria-hidden="true" />
        {state.text}
      </span>
      {demo.mode === 'recorded' ? (
        <div className="st__recorded">
          <p>
            This hosted copy shows a sandbox run recorded earlier. {demo.note} Approvals and payouts are limited to the canonical
            plan.
          </p>
          <label className="st__field">
            <span>Demo token</span>
            <input
              type="password"
              autoComplete="off"
              value={token}
              onChange={(e) => {
                setToken(e.target.value)
                setDemoToken(e.target.value)
              }}
            />
          </label>
        </div>
      ) : null}
    </div>
  )
}

/* ------------------------------------------------------------------ plan */

function PlanTable({ plan }: { plan: ExecutablePlanView }) {
  return (
    <div className="st__plan">
      <table className="st__table">
        <thead>
          <tr>
            <th scope="col">Action</th>
            <th scope="col">Merchant</th>
            <th scope="col" className="is-num">
              Analytic
            </th>
            <th scope="col" className="is-num">
              Sandbox
            </th>
            <th scope="col">Paid to</th>
          </tr>
        </thead>
        <tbody>
          {plan.actions.map((a) => (
            <tr key={a.key}>
              <td>
                {a.action_type.replace(/_/g, ' ')}
                {a.mapping ? <em>as a PayPal {a.mapping}</em> : null}
              </td>
              <td className="num">{a.merchant_id}</td>
              <td className="is-num num">{inr(Number(a.analytic_amount_inr))}</td>
              <td className="is-num num">{usd(a.executed.value)}</td>
              <td className={a.recipient ? 'num' : 'st__gap'}>
                {a.recipient ? a.recipient.receiver : (a.reason ?? 'no sandbox account mapped')}
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row" colSpan={2}>
              Total
            </th>
            <td className="is-num num">{inr(Number(plan.replayed_cost_inr))}</td>
            <td className="is-num num">{usd(plan.executed_total.value)}</td>
            <td className="st__muted">sandbox column: payable now, to mapped accounts</td>
          </tr>
        </tfoot>
      </table>
      <p className="st__scale">
        Sandbox amounts use a declared display scale of <span className="num">{plan.demo.scale}</span> {plan.demo.currency} per
        analytic rupee, not an exchange rate. The two columns are never added together.
      </p>
      <Seal src={`GET /execution/plans/${plan.scenario_id}`} refId={plan.snapshot_id}>
        priced from the frozen snapshot
      </Seal>
    </div>
  )
}

/* ----------------------------------------------------------------- steps */

type Step = 'review' | 'sign' | 'pay' | 'done'

function Steps({ plan, onPaid }: { plan: ExecutablePlanView; onPaid: () => void }) {
  const [step, setStep] = useState<Step>('review')
  const [approver, setApprover] = useState('')
  const [confirm, setConfirm] = useState(false)
  const [approval, setApproval] = useState<ApprovalView | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  const sign = (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    api
      .approve(plan.scenario_id, approver.trim(), plan.plan_hash)
      .then((a) => {
        setApproval(a)
        setStep('pay')
      })
      .catch((err: unknown) => setError(err instanceof ApiError ? err : null))
      .finally(() => setBusy(false))
  }
  const pay = () => {
    if (!approval) return
    setBusy(true)
    setError(null)
    api
      .execute(plan.scenario_id, approval.approval_id)
      .then(() => {
        setStep('done')
        onPaid()
      })
      .catch((err: unknown) => setError(err instanceof ApiError ? err : null))
      .finally(() => setBusy(false))
  }

  const cls = (s: Step, order: Step[]) =>
    `st__step${step === s ? ' is-on' : order.indexOf(step) > order.indexOf(s) ? ' is-done' : ''}`
  const order: Step[] = ['review', 'sign', 'pay', 'done']

  return (
    <div className="st__steps">
      <ol>
        <li className={cls('review', order)}>
          <span className="st__n">1</span>
          <h5>Review the exact plan</h5>
          {step === 'review' ? (
            <button type="button" className="st__btn" onClick={() => setStep('sign')}>
              I have read the plan
            </button>
          ) : (
            <p>
              Plan <span className="num">{plan.plan_hash.slice(0, 12)}</span>. Change any amount, merchant or recipient and it is a
              different plan that needs a new signature.
            </p>
          )}
        </li>
        <li className={cls('sign', order)}>
          <span className="st__n">2</span>
          <h5>Sign it</h5>
          {step === 'sign' ? (
            <form onSubmit={sign} className="st__sign">
              <label className="st__field">
                <span>Approver</span>
                <input required maxLength={80} value={approver} onChange={(e) => setApprover(e.target.value)} autoComplete="name" />
              </label>
              <label className="st__check">
                <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} required />
                <span>I approve paying exactly these amounts in the PayPal sandbox.</span>
              </label>
              <button type="submit" className="st__btn st__btn--ink" disabled={busy || !confirm || !approver.trim()}>
                {busy ? 'Signing…' : 'Sign the approval'}
              </button>
            </form>
          ) : approval ? (
            <p>
              Signed by {approval.approver} · <span className="num">{approval.approval_id}</span>
            </p>
          ) : (
            <p>After review.</p>
          )}
        </li>
        <li className={cls('pay', order)}>
          <span className="st__n">3</span>
          <h5>Pay in the PayPal sandbox</h5>
          {step === 'pay' ? (
            <button type="button" className="st__btn st__btn--ink" onClick={pay} disabled={busy}>
              {busy ? 'Submitting to PayPal…' : 'Pay the approved plan'}
            </button>
          ) : step === 'done' ? (
            <p>Submitted once. The ledger below shows what PayPal has said so far.</p>
          ) : (
            <p>Only after a signature on this exact plan.</p>
          )}
        </li>
      </ol>
      {error ? (
        <p className="st__error" role="alert">
          {error.message}
        </p>
      ) : null}
    </div>
  )
}

/* ---------------------------------------------------------------- ledger */

function Ledger({ scenarioId, nonce, onChange }: { scenarioId: string; nonce: number; onChange: () => void }) {
  const [ledger, setLedger] = useState<LedgerView | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [asking, setAsking] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    api
      .ledger(scenarioId)
      .then((l) => {
        if (!live) return
        setLedger(l)
        setError(null)
      })
      .catch((e: unknown) => live && setError(e instanceof ApiError ? e : null))
    return () => {
      live = false
    }
  }, [scenarioId, nonce])

  const ask = (recordId: string) => {
    setAsking(recordId)
    api
      .refresh(recordId)
      .then(onChange)
      .catch((e: unknown) => setError(e instanceof ApiError ? e : null))
      .finally(() => setAsking(null))
  }

  return (
    <div className="st__ledger">
      <div className="st__ledger-head">
        <h5>The ledger</h5>
        <button type="button" className="st__btn st__btn--quiet" onClick={onChange}>
          Re-read
        </button>
      </div>
      {error?.status === 503 ? (
        <p className="st__line">No execution database on this server: plans can be read, nothing can be approved or paid.</p>
      ) : error ? (
        <p className="st__error">{error.message}</p>
      ) : !ledger ? (
        <p className="st__line">Reading the ledger…</p>
      ) : ledger.records.length === 0 ? (
        <p className="st__line">
          {ledger.approvals.length
            ? 'Approved and awaiting payment: nothing has been sent to PayPal yet.'
            : 'Recorded as a plan: nothing has been approved or paid.'}
        </p>
      ) : (
        <ul className="st__records">
          {ledger.records.map((r) => {
            const mark = MARK[r.status] ?? MARK.unknown
            const waiting = (r.status === 'submitted' || r.status === 'pending') && !r.paypal_payout_item_id
            const open = !['succeeded', 'failed', 'planned'].includes(r.status)
            return (
              <li key={r.record_id} className={`st__record st__record--${r.status}`}>
                <span className="st__mark" aria-hidden="true">
                  {mark.sign}
                </span>
                <div className="st__who">
                  <b className="num">{r.merchant_id}</b> · {mark.label}
                  <span className="st__reason">
                    {waiting ? "Waiting for PayPal's webhook; nothing is resubmitted." : (r.reason ?? '')}
                  </span>
                </div>
                <span className="num st__amount">{usd(r.executed.value)}</span>
                {r.paypal_payout_batch_id ? (
                  <Seal src="GET /execution/ledger" refId={r.paypal_payout_item_id ?? r.paypal_payout_batch_id}>
                    {r.paypal_status ?? undefined}
                  </Seal>
                ) : (
                  <span className="st__muted">no PayPal reference yet</span>
                )}
                {open ? (
                  <button type="button" className="st__btn st__btn--quiet" onClick={() => ask(r.record_id)} disabled={asking === r.record_id}>
                    {asking === r.record_id ? 'Asking…' : 'Ask PayPal'}
                  </button>
                ) : null}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

/* -------------------------------------------------------------- re-entry */

function Reentries({ scenarioId, nonce }: { scenarioId: string; nonce: number }) {
  const [rows, setRows] = useState<ReentryView[]>([])
  useEffect(() => {
    let live = true
    api
      .reentries(scenarioId)
      .then((r) => live && setRows(r))
      .catch(() => live && setRows([]))
    return () => {
      live = false
    }
  }, [scenarioId, nonce])
  if (!rows.length) return null
  return (
    <div className="st__reentry">
      <h5>A payout failed, so the week was run again</h5>
      {rows.map((r) => (
        <article key={r.analysis_id} className="st__re">
          <p>
            PayPal reported the payout to <b className="num">{r.merchant_id}</b> as {r.trigger.toLowerCase()}.{' '}
            {r.status === 'analysed' ? (
              <>
                It re-entered as a shock of {inr(Number(r.missing_inr))}, analysed within the on-demand bounds, reaching{' '}
                {count(r.merchants_reached ?? 0)} merchants.
              </>
            ) : (
              <>Not analysed: {r.reason}</>
            )}
          </p>
          {r.new_plan.length ? (
            <p className="st__newplan">
              New plan, needing its own signature:{' '}
              {r.new_plan.map((p, i) => (
                <span key={i}>
                  {i ? '; ' : ''}
                  {p.action_type.replace(/_/g, ' ')} to <b className="num">{p.merchant_id}</b>, {inr(Number(p.analytic_amount_inr))}
                </span>
              ))}
            </p>
          ) : null}
          <Seal src={`GET /execution/reentries?scenario_id=${scenarioId}`} refId={r.adhoc_scenario_id}>
            {r.computed_in_ms !== null ? `${count(Math.round(r.computed_in_ms))} ms` : undefined}
          </Seal>
        </article>
      ))}
    </div>
  )
}
