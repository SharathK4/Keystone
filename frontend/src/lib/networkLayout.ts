/**
 * Layout for the dependency view.
 *
 * Not a force simulation. A force layout at this size jitters for two seconds
 * and settles into a hairball, and the meaning is the point here.
 *
 * Every merchant sits on one ring, which is the decision that makes the picture
 * readable: with the interior empty, dependencies become chords and the eye can
 * follow them. An earlier version placed nodes *inside* the ring by importance
 * and the chords had nowhere to go - eighty lines crossing the middle at once.
 *
 *   position on the ring  <- tier, anchor round to micro, so the merchant
 *                            classes read as arcs of the circle
 *   within a tier         <- systemic importance, so the load-bearing members
 *                            of each class lead it
 *   node area             <- systemic importance
 *   chord depth           <- angular span. Neighbours get a shallow arc near
 *                            the rim; opposite ends of the ring get a chord
 *                            through the middle. That is what bundles them.
 *
 * Tier orders the ring rather than stacking it into layers because the data
 * does not support a layered reading: most dependencies here skip tiers
 * entirely, and bands would draw a hierarchy that is not in the network.
 *
 * Deterministic: the same snapshot lays out identically every time, which is
 * what lets a hover highlight mean something across reloads. Layout is
 * presentation - no backend quantity is recomputed here, only placed.
 */

import type { DependencyView, MerchantView } from '../api/types'

export interface LaidOutNode {
  id: string
  x: number
  y: number
  /** Angle on the ring, radians. Also used to place tier labels. */
  angle: number
  r: number
  importance: number
  rank: number | null
  sector: string
  tier: string
  vulnerable: boolean
  degree: number
}

export interface LaidOutEdge {
  source: string
  target: string
  x1: number
  y1: number
  x2: number
  y2: number
  /** Bundling control point: deep for long chords, shallow for neighbours. */
  cx: number
  cy: number
  weight: number
  width: number
  hub: boolean
}

export interface TierArc {
  tier: string
  from: number
  to: number
  count: number
  /** Mid-angle, for the label. */
  mid: number
}

export interface Layout {
  nodes: LaidOutNode[]
  edges: LaidOutEdge[]
  tiers: TierArc[]
  byId: Map<string, LaidOutNode>
  size: number
  radius: number
}

/** Largest counterparty first. Anything unrecognised sorts to the end. */
const TIER_ORDER = ['anchor', 'large', 'medium', 'small', 'micro']

function tierRank(tier: string): number {
  const i = TIER_ORDER.indexOf(tier)
  return i === -1 ? TIER_ORDER.length : i
}

export function layoutNetwork(
  merchants: MerchantView[],
  dependencies: DependencyView[],
  options: { size?: number; maxEdges?: number } = {},
): Layout {
  const size = options.size ?? 560
  const maxEdges = options.maxEdges ?? 216
  const cx = size / 2
  const cy = size / 2
  const radius = size * 0.395

  const maxImportance = Math.max(
    ...merchants.map((m) => m.systemic_importance ?? 0),
    Number.EPSILON,
  )

  // Tier bands round the ring; importance leads each band.
  const ordered = [...merchants].sort(
    (a, b) =>
      tierRank(a.tier) - tierRank(b.tier) ||
      (b.systemic_importance ?? -1) - (a.systemic_importance ?? -1) ||
      a.merchant_id.localeCompare(b.merchant_id),
  )

  // A small gap between tiers so the bands read as bands.
  const gap = 0.05
  const tiersPresent = new Set(ordered.map((m) => m.tier)).size
  const usable = Math.PI * 2 - gap * tiersPresent
  const step = usable / Math.max(ordered.length, 1)

  const nodes: LaidOutNode[] = []
  const tiers: TierArc[] = []
  let angle = -Math.PI / 2
  let currentTier: string | null = null
  let bandStart = angle
  let bandCount = 0

  const closeBand = (end: number) => {
    if (currentTier === null) return
    tiers.push({
      tier: currentTier,
      from: bandStart,
      to: end,
      count: bandCount,
      mid: (bandStart + end) / 2,
    })
  }

  for (const m of ordered) {
    if (m.tier !== currentTier) {
      if (currentTier !== null) {
        closeBand(angle - step)
        angle += gap
      }
      currentTier = m.tier
      bandStart = angle
      bandCount = 0
    }

    const importance = (m.systemic_importance ?? 0) / maxImportance
    nodes.push({
      id: m.merchant_id,
      x: cx + Math.cos(angle) * radius,
      y: cy + Math.sin(angle) * radius,
      angle,
      r: 2 + Math.sqrt(importance) * 6,
      importance,
      rank: m.systemic_rank,
      sector: m.sector,
      tier: m.tier,
      vulnerable: m.vulnerable,
      degree: m.in_degree + m.out_degree,
    })

    angle += step
    bandCount += 1
  }
  closeBand(angle - step)

  const byId = new Map(nodes.map((n) => [n.id, n]))

  // Heaviest first, so that a cap - if one is ever applied - keeps the
  // structure rather than an arbitrary slice of it.
  const kept = [...dependencies]
    .sort((a, b) => b.pass_through - a.pass_through)
    .slice(0, maxEdges)
  const maxWeight = kept[0]?.pass_through ?? 1

  const edges: LaidOutEdge[] = []
  for (const d of kept) {
    const a = byId.get(d.source_id)
    const b = byId.get(d.target_id)
    if (!a || !b) continue

    // Angular separation drives the bundling. Chords across the ring pass close
    // to the centre; neighbours bow only slightly off the rim.
    let delta = Math.abs(a.angle - b.angle) % (Math.PI * 2)
    if (delta > Math.PI) delta = Math.PI * 2 - delta
    const t = 0.46 * (1 - delta / Math.PI)

    const mx = (a.x + b.x) / 2
    const my = (a.y + b.y) / 2
    const weight = d.pass_through / (maxWeight || 1)

    edges.push({
      source: d.source_id,
      target: d.target_id,
      x1: a.x,
      y1: a.y,
      x2: b.x,
      y2: b.y,
      cx: cx + (mx - cx) * t,
      cy: cy + (my - cy) * t,
      weight,
      width: 0.3 + weight * 1.15,
      hub: a.importance > 0.5 || b.importance > 0.5,
    })
  }

  return { nodes, edges, tiers, byId, size, radius }
}

/** Ids adjacent to `id` in the drawn edge set, for hover emphasis. */
export function neighboursOf(edges: LaidOutEdge[], id: string): Set<string> {
  const out = new Set<string>()
  for (const e of edges) {
    if (e.source === id) out.add(e.target)
    else if (e.target === id) out.add(e.source)
  }
  return out
}
