/**
 * The rule lab's wire types and calls.
 *
 * Every number the lab draws arrives in one of these events, from a replay of
 * the week that ran for the request (or was reused from an identical one).
 */

import { call, streamEvents } from '../api/client'

export type Origin = 'people' | 'you' | 'model' | 'mutation'

export type Tree =
  | { t: 'num'; v: number }
  | { t: 'clue'; name: string }
  | { t: 'neg'; a: Tree }
  | { t: 'bin'; op: '+' | '-' | '*' | '/' | '**'; a: Tree; b: Tree }
  | { t: 'cmp'; op: '<' | '<=' | '>' | '>='; a: Tree; b: Tree }
  | { t: 'fn'; name: 'log' | 'sqrt' | 'abs' | 'min' | 'max'; args: Tree[] }

export interface Clue {
  name: string
  words: string
  group: string
  money: boolean
}

export interface PoolMerchant {
  merchant_id: string
  missed: number
  sector: string
  tier: string
  training: boolean
}

export interface Writer {
  mode: 'model' | 'mutation'
  model: string | null
  reason: string | null
}

export interface Setup {
  pool: PoolMerchant[]
  budgets: number[]
  clues: Clue[]
  people: { formula: string; idea: string }[]
  k: number
  writer: Writer
  board: Board | null
}

export interface Board {
  search_mean: number
  people_best: { formula: string; idea: string; mean: number } | null
  best: { formula: string; idea: string; mean: number; author: string; origin: Origin } | null
}

export type Pick = [string, number]

export interface Cell {
  key: string
  pct: number
  kept?: number
  picks: Pick[]
}

export interface TestResult {
  mean: number
  search_mean: number
  decide_ms: number
  search_ms: number
  wins: number
  n: number
  cells: { key: string; pct: number; picks: Pick[] }[]
}

export interface Rule {
  id: string
  formula: string
  tree: Tree
  size: number
  clues: string[]
  idea: string
  origin: Origin
  author: string
  generation: number
  parents: string[]
  mean: number
  wins: number
  ms: number
  cells: Cell[]
  test: TestResult | null
}

export interface Scenario {
  key: string
  failing: string
  budget: number
  missed: number
  candidates?: number
}

export interface LabHeader {
  lab_id: string
  training: string[]
  held_out: string[]
  budgets: number[]
  k: number
  banned: string[]
  scenarios: Scenario[]
  writer: Writer
}

export interface Refusal {
  formula: string
  reason: string
  author: string
  generation?: number
}

export interface UseResult {
  plan_ref: string
  rule_id: string
  formula: string
  failing: string
  budget: number
  missed: number
  picks: { merchant_id: string; amount: number }[]
  pct: number
  kept: number
  decide_ms: number
  search: { pct: number; ms: number; picks: { merchant_id: string; amount: number }[] }
}

export interface Check {
  ok: boolean
  formula?: string | null
  tree?: Tree | null
  size?: number | null
  clues: string[]
  error?: string | null
}

export type LabEvent = Record<string, unknown> & { type: string }

export interface StartOptions {
  training: string[]
  budgets: number[]
  k: number
  banned: string[]
  your_rule: string | null
}

const on = (fn: (e: LabEvent) => void) => (e: Record<string, unknown>) => fn(e as LabEvent)

export const lab = {
  setup: () => call<Setup>('/discovery/setup'),
  check: (formula: string, banned: string[]) =>
    call<Check>('/discovery/check', { formula, banned }),
  start: (body: StartOptions, fn: (e: LabEvent) => void) =>
    streamEvents('/discovery/labs', body, on(fn)),
  evolve: (
    id: string,
    body: { mode: 'evolve' | 'breed' | 'push'; parents: string[]; deep: boolean },
    fn: (e: LabEvent) => void,
  ) => streamEvents(`/discovery/labs/${id}/evolve`, body, on(fn)),
  add: (id: string, formula: string, fn: (e: LabEvent) => void) =>
    streamEvents(`/discovery/labs/${id}/rules`, { formula }, on(fn)),
  test: (id: string, ruleIds: string[], fn: (e: LabEvent) => void) =>
    streamEvents(`/discovery/labs/${id}/test`, { rule_ids: ruleIds }, on(fn)),
  use: (id: string, ruleId: string, failing: string, budget: number) =>
    call<UseResult>(`/discovery/labs/${id}/use`, { rule_id: ruleId, failing, budget }),
}
