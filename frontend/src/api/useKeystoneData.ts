/**
 * Page state: what has loaded, what is selected, what failed.
 *
 * The structural reads (network, merchants, dependencies, systemic ranking,
 * execution status, snapshot identity) happen once on mount. The scenario read
 * happens again whenever the selection changes, and is served from the client
 * cache the second time a scenario is chosen - so flipping back and forth
 * between scenarios costs nothing after the first visit.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiError, api } from './client'
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

export interface Structure {
  network: NetworkOverview
  merchants: MerchantView[]
  dependencies: DependencyView[]
  systemic: SystemicRankingView
  scenarios: ScenarioSummary[]
  execution: ExecutionStatus
  snapshot: SnapshotHealth
}

type Phase = 'loading' | 'ready' | 'failed'

export function useKeystoneData() {
  const [phase, setPhase] = useState<Phase>('loading')
  const [error, setError] = useState<ApiError | null>(null)
  const [structure, setStructure] = useState<Structure | null>(null)

  const [scenarioId, setScenarioId] = useState<string | null>(null)
  const [scenario, setScenario] = useState<ScenarioSnapshot | null>(null)
  const [scenarioError, setScenarioError] = useState<ApiError | null>(null)

  const [merchantId, setMerchantId] = useState<string | null>(null)
  const [merchant, setMerchant] = useState<MerchantDetail | null>(null)

  useEffect(() => {
    let live = true
    Promise.all([
      api.network(),
      api.merchants(),
      api.dependencies(),
      api.systemic(),
      api.scenarios(),
      api.execution(),
      api.snapshot(),
    ])
      .then(([network, merchants, dependencies, systemic, scenarios, execution, snapshot]) => {
        if (!live) return
        setStructure({ network, merchants, dependencies, systemic, scenarios, execution, snapshot })
        // Default to the scenario that actually has something to show: the
        // first one carrying a recommendation. Falls back to the first.
        const withAction = scenarios.find((s) => s.recommended_action !== null)
        setScenarioId((withAction ?? scenarios[0])?.scenario_id ?? null)
        setPhase('ready')
      })
      .catch((e: unknown) => {
        if (!live) return
        setError(e instanceof ApiError ? e : new ApiError('Unexpected client error.', null, ''))
        setPhase('failed')
      })
    return () => {
      live = false
    }
  }, [])

  useEffect(() => {
    if (!scenarioId) return
    let live = true
    api
      .scenario(scenarioId)
      .then((s) => live && setScenario(s))
      .catch((e: unknown) => {
        if (!live) return
        setScenarioError(e instanceof ApiError ? e : new ApiError('Scenario failed.', null, ''))
      })
    return () => {
      live = false
    }
  }, [scenarioId])

  useEffect(() => {
    if (!merchantId) return
    let live = true
    api
      .merchant(merchantId)
      .then((d) => live && setMerchant(d))
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [merchantId])

  // Derived, not stored: a stale scenario on screen is exactly the busy state.
  const scenarioBusy = scenarioId !== null && scenario?.scenario_id !== scenarioId

  const selectScenario = useCallback((id: string) => setScenarioId(id), [])
  const selectMerchant = useCallback((id: string | null) => setMerchantId(id), [])

  /** Merchant lookup by id, built once - the panels index into it constantly. */
  const byId = useMemo(() => {
    const map = new Map<string, MerchantView>()
    structure?.merchants.forEach((m) => map.set(m.merchant_id, m))
    return map
  }, [structure])

  return {
    phase,
    error,
    structure,
    byId,
    scenario,
    scenarioId,
    scenarioBusy,
    scenarioError,
    selectScenario,
    // Only hand back detail that belongs to the current selection, so a slow
    // response for a previous merchant can never be rendered against this one.
    merchant: merchant && merchant.merchant.merchant_id === merchantId ? merchant : null,
    merchantId,
    selectMerchant,
  }
}
