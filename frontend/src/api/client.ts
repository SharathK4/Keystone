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

const BASE = '/api/v1'

export class ApiError extends Error {
  readonly status: number | null
  readonly path: string

  constructor(message: string, status: number | null, path: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.path = path
  }
}

const cache = new Map<string, Promise<unknown>>()

async function request<T>(path: string): Promise<T> {
  let response: Response
  try {
    response = await fetch(BASE + path, { headers: { accept: 'application/json' } })
  } catch {
    throw new ApiError('Analytical service unreachable.', null, path)
  }
  if (!response.ok) {
    // The API returns a typed error envelope; use its message when there is one.
    let detail = `Request failed (${response.status}).`
    try {
      const body = (await response.json()) as { message?: string }
      if (body?.message) detail = body.message
    } catch {
      /* non-JSON error body: keep the status line */
    }
    throw new ApiError(detail, response.status, path)
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
}
