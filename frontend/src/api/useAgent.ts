/**
 * Ask Keystone's state: the question, every step as it streams in, the
 * analysis the cards redraw around, the PayPal payouts and the answer.
 */

import { useCallback, useRef, useState } from 'react'
import { ApiError, askKeystone } from './client'
import type { ScenarioSnapshot } from './types'

export interface AgentStep {
  id: string
  tool: string
  label: string
  status: 'running' | 'done' | 'error'
  summary?: string
  ms?: number
}

export interface AgentPayout {
  merchant_id: string
  paypal_account: string | null
  sandbox_amount: string
  payable: boolean
  reason: string | null
}

export interface AgentPayment {
  plan_ref: string
  connected: boolean
  complete: boolean
  payouts: AgentPayout[]
  total: string
}

export interface AgentAnswer {
  text: string
  source: string
  grounded: boolean
  replaced?: boolean
}

export interface AgentRun {
  phase: 'running' | 'done' | 'error'
  question: string
  mode: 'claude' | 'rules' | null
  model: string | null
  notice: string | null
  steps: AgentStep[]
  scenario: ScenarioSnapshot | null
  planRef: string | null
  payment: AgentPayment | null
  answer: AgentAnswer | null
  error: string | null
  ms: number | null
}

const fresh = (question: string): AgentRun => ({
  phase: 'running',
  question,
  mode: null,
  model: null,
  notice: null,
  steps: [],
  scenario: null,
  planRef: null,
  payment: null,
  answer: null,
  error: null,
  ms: null,
})

export function useAgent() {
  const [run, setRun] = useState<AgentRun | null>(null)
  const seq = useRef(0)

  const ask = useCallback((question: string) => {
    const id = ++seq.current
    setRun(fresh(question))
    const update = (fn: (r: AgentRun) => AgentRun) =>
      setRun((r) => (r && seq.current === id ? fn(r) : r))

    askKeystone(question, (e) => {
      switch (e.type) {
        case 'start':
          update((r) => ({ ...r, mode: e.mode as AgentRun['mode'], model: (e.model as string) ?? null }))
          break
        case 'notice':
          update((r) => ({ ...r, notice: String(e.message), mode: 'rules' }))
          break
        case 'step':
          update((r) => {
            const known = r.steps.find((s) => s.id === e.id)
            const step: AgentStep = {
              id: String(e.id),
              tool: String(e.tool),
              label: (e.label as string) ?? known?.label ?? String(e.tool),
              status: e.status as AgentStep['status'],
              summary: (e.summary as string) ?? known?.summary,
              ms: (e.ms as number) ?? known?.ms,
            }
            return {
              ...r,
              steps: known ? r.steps.map((s) => (s.id === step.id ? step : s)) : [...r.steps, step],
            }
          })
          break
        case 'scenario':
          update((r) => ({
            ...r,
            scenario: e.scenario as ScenarioSnapshot,
            planRef: String(e.plan_ref),
          }))
          break
        case 'payment':
          update((r) => ({ ...r, payment: e as unknown as AgentPayment }))
          break
        case 'answer':
          update((r) => ({ ...r, answer: e as unknown as AgentAnswer }))
          break
        case 'error':
          update((r) => ({ ...r, phase: 'error', error: String(e.message) }))
          break
        case 'done':
          update((r) => ({ ...r, phase: r.phase === 'error' ? 'error' : 'done', ms: e.ms as number }))
          break
      }
    }).catch((err: unknown) =>
      update((r) => ({
        ...r,
        phase: 'error',
        error: err instanceof ApiError ? err.message : 'Keystone could not answer.',
      })),
    )
  }, [])

  const clear = useCallback(() => {
    seq.current += 1
    setRun(null)
  }, [])

  return { run, ask, clear }
}
