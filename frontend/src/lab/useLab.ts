/**
 * The lab's state, built only from what the server streams.
 *
 * A rule moves through three states the chart animates between: `proposed`
 * (written, not yet run - it sits on its parent), `testing` (its scenarios come
 * in one by one and it climbs or sinks to its running mean), `scored`.
 */

import { useCallback, useReducer, useRef } from 'react'
import { ApiError } from '../api/client'
import {
  lab,
  type LabEvent,
  type LabHeader,
  type Pick,
  type Refusal,
  type Rule,
  type StartOptions,
  type Writer,
} from './api'

export type RuleStatus = 'proposed' | 'testing' | 'scored'

export interface LiveRule extends Rule {
  status: RuleStatus
  live: Record<string, number>
}

export interface SearchCell {
  key: string
  pct: number
  ms: number
  picks: Pick[]
}

export interface Thinking {
  generation: number
  mode: string
  writer: 'model' | 'mutation'
  model: string | null
  parents: string[]
  deep: boolean
  reason: string | null
  startedAt: number
}

export interface Round {
  generation: number
  author: string
  ms: number
  reasoning: string
  refused: Refusal[]
  admitted: string[]
}

export interface LogLine {
  at: number
  text: string
  tone?: 'model' | 'warn' | 'good'
}

export interface HeldOut {
  failures: string[]
  scenarios: { key: string; failing: string; budget: number; missed: number }[]
  rules: string[]
  search: Record<string, number>
  live: Record<string, Record<string, number>>
  rows: {
    id: string
    mean: number
    search_mean: number
    decide_ms: number
    search_ms: number
    wins: number
    n: number
  }[]
  done: boolean
}

export type Phase = 'setup' | 'starting' | 'ready' | 'busy'

export interface LabState {
  phase: Phase
  busyWith: string | null
  header: LabHeader | null
  search: Record<string, SearchCell>
  searchMean: number | null
  rules: Record<string, LiveRule>
  order: string[]
  generation: number
  thinking: Thinking | null
  rounds: Round[]
  refused: Refusal[]
  replays: number
  log: LogLine[]
  notice: string | null
  error: string | null
  heldOut: HeldOut | null
  writer: Writer | null
}

const INITIAL: LabState = {
  phase: 'setup',
  busyWith: null,
  header: null,
  search: {},
  searchMean: null,
  rules: {},
  order: [],
  generation: 0,
  thinking: null,
  rounds: [],
  refused: [],
  replays: 0,
  log: [],
  notice: null,
  error: null,
  heldOut: null,
  writer: null,
}

type Action =
  | { kind: 'event'; event: LabEvent }
  | { kind: 'busy'; what: string }
  | { kind: 'idle' }
  | { kind: 'failed'; message: string }
  | { kind: 'reset' }

const say = (state: LabState, text: string, tone?: LogLine['tone']): LogLine[] =>
  [...state.log, { at: Date.now(), text, tone }].slice(-60)

const pct = (v: number) => `${v.toFixed(1)}%`

function reduce(state: LabState, action: Action): LabState {
  switch (action.kind) {
    case 'reset':
      return INITIAL
    case 'busy':
      return { ...state, phase: state.header ? 'busy' : 'starting', busyWith: action.what, error: null, notice: null }
    case 'idle':
      return { ...state, phase: state.header ? 'ready' : 'setup', busyWith: null, thinking: null }
    case 'failed':
      return {
        ...state,
        phase: state.header ? 'ready' : 'setup',
        busyWith: null,
        thinking: null,
        error: action.message,
        log: say(state, action.message, 'warn'),
      }
    case 'event':
      return onEvent(state, action.event)
  }
}

function onEvent(state: LabState, e: LabEvent): LabState {
  switch (e.type) {
    case 'lab': {
      const header = e as unknown as LabHeader
      return {
        ...state,
        header,
        writer: header.writer,
        log: say(state, `Lab opened · ${header.scenarios.length} scenarios to win`),
      }
    }
    case 'phase':
      return { ...state, log: say(state, String(e.text)) }
    case 'search': {
      const cell = e as unknown as SearchCell
      return {
        ...state,
        search: { ...state.search, [cell.key]: cell },
        replays: state.replays + 1,
      }
    }
    case 'search_done':
      return {
        ...state,
        searchMean: Number(e.mean),
        log: say(state, `Keystone's search: ${pct(Number(e.mean))} of the damage, on average`),
      }
    case 'testing': {
      const rule = e.rule as Rule
      const known = state.rules[rule.id]
      return {
        ...state,
        rules: { ...state.rules, [rule.id]: { ...(known ?? rule), ...rule, cells: [], status: 'testing', live: {} } },
        order: known ? state.order : [...state.order, rule.id],
      }
    }
    case 'cell': {
      const id = String(e.rule_id)
      const rule = state.rules[id]
      if (!rule) return state
      return {
        ...state,
        replays: state.replays + (e.cached ? 0 : 1),
        rules: { ...state.rules, [id]: { ...rule, live: { ...rule.live, [String(e.key)]: Number(e.pct) } } },
      }
    }
    case 'scored': {
      const rule = e.rule as Rule
      const prev = state.rules[rule.id]
      const tone = rule.origin === 'model' ? 'model' : undefined
      return {
        ...state,
        rules: { ...state.rules, [rule.id]: { ...rule, status: 'scored', live: prev?.live ?? {} } },
        order: prev ? state.order : [...state.order, rule.id],
        log: say(state, `${rule.id}  ${rule.formula}  →  ${pct(rule.mean)}`, tone),
      }
    }
    case 'thinking': {
      const t: Thinking = {
        generation: Number(e.generation),
        mode: String(e.mode),
        writer: e.writer as Thinking['writer'],
        model: (e.model as string | null) ?? null,
        parents: (e.parents as string[]) ?? [],
        deep: Boolean(e.deep),
        reason: (e.reason as string | null) ?? null,
        startedAt: Date.now(),
      }
      const who = t.writer === 'model' ? 'The model' : 'Random mutation'
      const what = t.mode === 'breed' ? `breeds ${t.parents.join(' × ')}` : t.mode === 'push' ? `pushes ${t.parents[0]} further` : `studies ${t.parents.join(', ')}`
      return { ...state, thinking: t, log: say(state, `Round ${t.generation}: ${who} ${what}`, t.writer === 'model' ? 'model' : undefined) }
    }
    case 'proposed': {
      const generation = Number(e.generation)
      const written = (e.rules as { id: string; formula: string; idea: string; parents: string[] }[]) ?? []
      const refused = ((e.refused as Refusal[]) ?? []).map((r) => ({ ...r, generation }))
      const rules = { ...state.rules }
      const order = [...state.order]
      for (const w of written) {
        if (!rules[w.id]) order.push(w.id)
        rules[w.id] = {
          id: w.id,
          formula: w.formula,
          tree: { t: 'clue', name: '?' },
          size: 0,
          clues: [],
          idea: w.idea,
          origin: e.author === 'random mutation' ? 'mutation' : 'model',
          author: String(e.author),
          generation,
          parents: w.parents,
          mean: 0,
          wins: 0,
          ms: 0,
          cells: [],
          test: null,
          status: 'proposed',
          live: {},
        }
      }
      const round: Round = {
        generation,
        author: String(e.author),
        ms: Number(e.ms),
        reasoning: String(e.reasoning ?? ''),
        refused,
        admitted: written.map((w) => w.id),
      }
      const secs = (Number(e.ms) / 1000).toFixed(1)
      let log = say(state, `${written.length} rules written in ${secs} s${refused.length ? ` · ${refused.length} refused` : ''}`, e.author === 'random mutation' ? undefined : 'model')
      for (const r of refused) log = [...log, { at: Date.now(), text: `refused  ${r.formula}  (${r.reason})`, tone: 'warn' as const }]
      return { ...state, rules, order, rounds: [...state.rounds, round], refused: [...state.refused, ...refused], log: log.slice(-60) }
    }
    case 'refused': {
      const items = ((e.items as Refusal[]) ?? []).map((r) => ({ ...r, generation: Number(e.generation) }))
      return {
        ...state,
        refused: [...state.refused, ...items],
        error: items[0] ? `Refused: ${items[0].reason}` : state.error,
        log: say(state, items.map((r) => `refused  ${r.formula}  (${r.reason})`).join(' · '), 'warn'),
      }
    }
    case 'skipped':
      return state
    case 'notice':
      return { ...state, notice: String(e.text), log: say(state, String(e.text), 'warn') }
    case 'generation': {
      const best = e.best as Rule | null
      return {
        ...state,
        generation: Number(e.n),
        thinking: null,
        log: say(
          state,
          `Round ${e.n} done · ${e.replays} new replays in ${(Number(e.ms) / 1000).toFixed(1)} s${best ? ` · best ${best.id} ${pct(best.mean)}` : ''}`,
          'good',
        ),
      }
    }
    case 'test_start':
      return {
        ...state,
        heldOut: {
          failures: e.failures as string[],
          scenarios: e.scenarios as HeldOut['scenarios'],
          rules: e.rules as string[],
          search: {},
          live: {},
          rows: [],
          done: false,
        },
        log: say(state, `Final test on ${(e.failures as string[]).length} failures no rule has seen`),
      }
    case 'test_search': {
      if (!state.heldOut) return state
      return { ...state, heldOut: { ...state.heldOut, search: { ...state.heldOut.search, [String(e.key)]: Number(e.pct) } } }
    }
    case 'test_cell': {
      if (!state.heldOut) return state
      const id = String(e.rule_id)
      const live = { ...state.heldOut.live, [id]: { ...(state.heldOut.live[id] ?? {}), [String(e.key)]: Number(e.pct) } }
      return { ...state, heldOut: { ...state.heldOut, live } }
    }
    case 'tested': {
      if (!state.heldOut) return state
      const rows = e.rows as HeldOut['rows']
      const rules = { ...state.rules }
      for (const row of rows) {
        const r = rules[row.id]
        if (r) rules[row.id] = { ...r, test: { ...(row as unknown as Rule['test'] & object), cells: [] } }
      }
      const top = rows[0]
      return {
        ...state,
        rules,
        heldOut: { ...state.heldOut, rows, done: true },
        log: say(state, top ? `On unseen failures: ${top.id} ${pct(top.mean)} · search ${pct(top.search_mean)}` : 'Final test done', 'good'),
      }
    }
    case 'error':
      return { ...state, error: String(e.message), log: say(state, String(e.message), 'warn') }
    default:
      return state
  }
}

export function useLab() {
  const [state, dispatch] = useReducer(reduce, INITIAL)
  const running = useRef(false)

  const run = useCallback(async (what: string, job: (fn: (e: LabEvent) => void) => Promise<void>) => {
    if (running.current) return
    running.current = true
    dispatch({ kind: 'busy', what })
    try {
      await job((event) => dispatch({ kind: 'event', event }))
      dispatch({ kind: 'idle' })
    } catch (error) {
      dispatch({ kind: 'failed', message: error instanceof ApiError ? error.message : 'The lab could not be reached.' })
    } finally {
      running.current = false
    }
  }, [])

  const labId = state.header?.lab_id ?? null

  return {
    state,
    start: (options: StartOptions) => run('start', (fn) => lab.start(options, fn)),
    evolve: (mode: 'evolve' | 'breed' | 'push', parents: string[], deep: boolean) =>
      labId ? run(mode, (fn) => lab.evolve(labId, { mode, parents, deep }, fn)) : Promise.resolve(),
    add: (formula: string) => (labId ? run('add', (fn) => lab.add(labId, formula, fn)) : Promise.resolve()),
    test: (ids: string[]) => (labId ? run('test', (fn) => lab.test(labId, ids, fn)) : Promise.resolve()),
    reset: () => dispatch({ kind: 'reset' }),
    isRunning: () => running.current,
  }
}

/** A rule's mean so far: final when scored, running while its scenarios come in. */
export function runningMean(rule: LiveRule): number | null {
  if (rule.status === 'scored') return rule.mean
  const values = Object.values(rule.live)
  return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null
}
