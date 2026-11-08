/**
 * The only place this app talks to the network.
 *
 * Two rules it exists to enforce:
 *
 *  1. Every GET is cached by URL for the life of the page. The backend serves a
 *     precomputed snapshot, so the answer for a given URL cannot change while
 *     the tab is open; re-fetching on a hover or a re-render would be pure
 *     waste. In-flight requests are shared, so a burst of mounts is one call.
 *
 *  2. A failure is a failure. It surfaces as an ApiError the UI renders as a
 *     system state - never as a fallback number. Nothing in this file, or
 *     anywhere downstream of it, invents a figure when the service is down.
 */

import type {
  DependencyView,
  ExecutionStatus,
  MerchantDetail,
  MerchantView,
  NetworkOverview,
  ScenarioSnapshot,
  ScenarioSummary,
  SnapshotHealth,
  SystemicRankingView,
} from './types'
import type {
  ApprovalView,
  DemoStatusView,
  DisplayView,
  ExecutablePlanView,
  ExecutionRecordView,
  LedgerView,
  PayPalStatusView,
  ProofView,
  PullsView,
  ReentryView,
  SpeedScenarioView,
  SpeedSummary,
  WalkAskView,
  WalkFireView,
  WalkSpeedView,
  WalkView,
} from './schema'

// Same origin by default; a separately hosted frontend sets VITE_API_BASE.
const BASE = `${import.meta.env.VITE_API_BASE ?? ''}/api/v1`

export class ApiError extends Error {
  readonly status: number | null
  readonly path: string
  readonly code: string | null

  constructor(message: string, status: number | null, path: string, code: string | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.path = path
    this.code = code
  }
}

const TOKEN_KEY = 'keystone.demoToken'
const TOKEN_HEADER = 'X-Keystone-Demo-Token'

/** The hosted demo's token, kept for this tab only. Sent on actions, never on reads. */
export function demoToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setDemoToken(value: string): void {
  try {
    if (value) sessionStorage.setItem(TOKEN_KEY, value)
    else sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    /* storage unavailable: the token lives only in the field */
  }
}

const cache = new Map<string, Promise<unknown>>()

async function request<T>(path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { accept: 'application/json' }
  if (body !== undefined) {
    headers['content-type'] = 'application/json'
    const token = demoToken()
    if (token) headers[TOKEN_HEADER] = token
  }
  let response: Response
  try {
    response = await fetch(BASE + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError('Analytical service unreachable.', null, path)
  }
  if (!response.ok) {
    // The API returns a typed error envelope; use its message when there is one.
    let detail = `Request failed (${response.status}).`
    let code: string | null = null
    try {
      const err = (await response.json()) as { message?: string; code?: string }
      if (err?.message) detail = err.message
      code = err?.code ?? null
    } catch {
      /* non-JSON error body: keep the status line */
    }
    throw new ApiError(detail, response.status, path, code)
  }
  return (await response.json()) as T
}

function cached<T>(path: string): Promise<T> {
  const hit = cache.get(path)
  if (hit) return hit as Promise<T>
  const pending = request<T>(path).catch((error: unknown) => {
    // A failed request must not be cached, or a transient outage would poison
    // the page until reload.
    cache.delete(path)
    throw error
  })
  cache.set(path, pending)
  return pending as Promise<T>
}

export const api = {
  network: () => cached<NetworkOverview>('/network'),
  merchants: () => cached<MerchantView[]>('/network/merchants'),
  merchant: (id: string) => cached<MerchantDetail>(`/network/merchants/${encodeURIComponent(id)}`),
  dependencies: (limit = 240) => cached<DependencyView[]>(`/network/dependencies?limit=${limit}`),
  systemic: (limit = 100) => cached<SystemicRankingView>(`/network/systemic-importance?limit=${limit}`),
  scenarios: () => cached<ScenarioSummary[]>('/scenarios'),
  scenario: (id: string) => cached<ScenarioSnapshot>(`/scenarios/${encodeURIComponent(id)}`),
  execution: () => cached<ExecutionStatus>('/execution/status'),
  snapshot: () => cached<SnapshotHealth>('/snapshot'),
  display: () => cached<DisplayView>('/display'),

  // The walk. Its mode can change (a recording lands), so it is never cached;
  // firing and asking are user actions and always reach the server.
  walk: () => request<WalkView>('/walk'),
  walkFire: () => request<WalkFireView>('/walk/fire', {}),
  walkAsk: (text: string) => request<WalkAskView>('/walk/ask', { text }),
  walkSpeed: () => cached<WalkSpeedView>('/walk/speed'),
  agentStatus: () =>
    request<{ mode: string; model: string | null; effort: string | null }>('/agent/status'),

  // Offline, content-addressed artifacts: frozen like the snapshot, so cached.
  pulls: () => cached<PullsView>('/structure/pulls'),
  speed: () => cached<SpeedSummary>('/structure/speed'),
  speedScenario: (id: string) =>
    cached<SpeedScenarioView>(`/structure/speed/scenarios/${encodeURIComponent(id)}`),
  proof: () => cached<ProofView>('/structure/proof'),

  // A user-triggered bounded analysis: computed now, never cached.
  analyze: (merchantIds: string[], magnitudeMultiple: number, onsetHours: number) =>
    request<ScenarioSnapshot>('/scenarios/analyze', {
      merchant_ids: merchantIds,
      magnitude_multiple: magnitudeMultiple,
      onset_hours: onsetHours,
    }),

  // Execution state changes underneath the page, so it is read fresh each time
  // it is opened or explicitly refreshed, never from the cache.
  paypal: () => request<PayPalStatusView>('/execution/paypal'),
  demo: () => request<DemoStatusView>('/execution/demo'),
  plan: (id: string) => request<ExecutablePlanView>(`/execution/plans/${encodeURIComponent(id)}`),
  ledger: (id: string) =>
    request<LedgerView>(`/execution/ledger?scenario_id=${encodeURIComponent(id)}`),
  reentries: (id: string) =>
    request<ReentryView[]>(`/execution/reentries?scenario_id=${encodeURIComponent(id)}`),
  approve: (id: string, approver: string, planHash: string) =>
    request<ApprovalView>(`/execution/plans/${encodeURIComponent(id)}/approvals`, {
      approver,
      plan_hash: planHash,
    }),
  execute: (id: string, approvalId: string) =>
    request<ExecutionRecordView[]>(`/execution/plans/${encodeURIComponent(id)}/executions`, {
      approval_id: approvalId,
    }),
  refresh: (recordId: string) =>
    request<ExecutionRecordView>(`/execution/records/${encodeURIComponent(recordId)}/refresh`, {}),
}

/**
 * Ask Keystone. The answer streams as server-sent events - each tool call as it
 * runs and finishes, the analysis, the PayPal payouts, the answer - and each is
 * handed to `onEvent` the moment it arrives.
 */
export async function askKeystone(
  question: string,
  onEvent: (event: Record<string, unknown>) => void,
): Promise<void> {
  const headers: Record<string, string> = {
    accept: 'text/event-stream',
    'content-type': 'application/json',
  }
  const token = demoToken()
  if (token) headers[TOKEN_HEADER] = token
  let response: Response
  try {
    response = await fetch(`${BASE}/agent/ask`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ question }),
    })
  } catch {
    throw new ApiError('Analytical service unreachable.', null, '/agent/ask')
  }
  if (!response.ok || !response.body) {
    let detail = `Request failed (${response.status}).`
    try {
      const err = (await response.json()) as { message?: string }
      if (err?.message) detail = err.message
    } catch {
      /* keep the status line */
    }
    throw new ApiError(detail, response.status, '/agent/ask')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let cut = buffer.indexOf('\n\n')
    while (cut !== -1) {
      const chunk = buffer.slice(0, cut)
      buffer = buffer.slice(cut + 2)
      const data = chunk
        .split('\n')
        .filter((line) => line.startsWith('data: '))
        .map((line) => line.slice(6))
        .join('\n')
      if (data) onEvent(JSON.parse(data) as Record<string, unknown>)
      cut = buffer.indexOf('\n\n')
    }
  }
}
