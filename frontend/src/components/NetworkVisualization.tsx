/**
 * The dependency figure.
 *
 * Merchants on a ring ordered by tier, dependencies as bundled chords across
 * it. Node area is backend-measured systemic importance, chord weight is
 * estimated pass-through.
 *
 * Two modes. `compact` is the landing state: fewer chords, no labels, no
 * interaction, one merchant marked - enough to say "there is a structure here"
 * and nothing more. The full mode is the analysis.
 *
 * The figure never moves on its own. No pulsing, no blinking, no drift, no
 * ambient loop - nothing here animates to imply that a machine is thinking.
 * The only motion is the transition when a merchant is selected: that node and
 * the chords touching it come forward, and the rest of the field recedes far
 * enough that the neighbourhood can be read on its own.
 */

import { useMemo, useState } from 'react'
import type { Layout } from '../lib/networkLayout'
import { neighboursOf } from '../lib/networkLayout'
import './network-visualization.css'

interface Props {
  layout: Layout
  compact?: boolean
  /** Invert the ink for a dark tile. */
  onDark?: boolean
  /** Merchant to mark by hand; drawn in SVG units so it cannot drift. */
  ringed?: string | null
  /** Handwritten label for the ringed merchant, with a leader that reaches it. */
  ringNote?: string
  selected?: string | null
  onHover?: (id: string | null) => void
  onSelect?: (id: string | null) => void
}

export function NetworkVisualization({
  layout,
  compact = false,
  onDark = false,
  ringed,
  ringNote,
  selected = null,
  onHover,
  onSelect,
}: Props) {
  const [hovered, setHovered] = useState<string | null>(null)
  const focus = compact ? null : (hovered ?? selected)

  const neighbours = useMemo(
    () => (focus ? neighboursOf(layout.edges, focus) : null),
    [layout.edges, focus],
  )

  // A merchant with no estimated dependency at all: ring it rather than dimming
  // the other ninety-nine to make the point.
  const isolated = neighbours !== null && neighbours.size === 0
  const dimming = focus !== null && !isolated

  const { size, radius } = layout
  const cx = size / 2
  const cy = size / 2
  const ringNode = ringed ? layout.byId.get(ringed) : undefined

  return (
    <svg
      className={`nv${compact ? ' nv--compact' : ''}${onDark ? ' nv--dark' : ''}`}
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label="Merchant dependency network: merchants on a ring ordered by tier, dependencies as chords"
      onPointerLeave={() => {
        if (compact) return
        setHovered(null)
        onHover?.(null)
      }}
    >
      {/* ---- tier bands ---- */}
      <g className="nv__tiers">
        {layout.tiers.map((t) => (
          <g key={t.tier}>
            <path className="nv__band" d={arc(cx, cy, radius + 15, t.from, t.to)} />
            {!compact ? (
              <text
                className="nv__band-label"
                x={cx + Math.cos(t.mid) * (radius + 31)}
                y={cy + Math.sin(t.mid) * (radius + 31)}
                textAnchor={labelAnchor(t.mid)}
                dominantBaseline="middle"
              >
                {t.tier}
              </text>
            ) : null}
          </g>
        ))}
      </g>

      {/* ---- chords ---- */}
      <g className="nv__edges">
        {layout.edges.map((e, i) => {
          const touching = focus ? e.source === focus || e.target === focus : false
          const dim = dimming && !touching
          return (
            <path
              key={`${e.source}-${e.target}-${i}`}
              className={`nv__edge${touching ? ' is-lit' : ''}${dim ? ' is-dim' : ''}${
                e.hub ? ' is-hub' : ''
              }`}
              d={`M${e.x1} ${e.y1} Q${e.cx} ${e.cy} ${e.x2} ${e.y2}`}
              strokeWidth={touching ? e.width + 0.8 : e.width}
              /* Weak links recede rather than adding line noise. */
              style={{ ['--edge-o' as string]: (0.13 + e.weight * 0.44).toFixed(3) }}
            />
          )
        })}
      </g>

      {/* ---- the pen, in SVG units so it stays on its node ---- */}
      {ringNode ? (
        <g
          className="nv__ink"
          transform={`translate(${ringNode.x} ${ringNode.y}) scale(0.4) translate(-48 -32)`}
        >
          <path
            d="M62.5 7.2C50.1 3.4 33.8 4.1 22.6 9.9 11.4 15.7 5.9 26.9 8.9 37.2c3 10.3 15.1 17.9 30.4 19.4 15.3 1.5 32-3.2 40.2-11.5 8.2-8.3 6.6-19.6-3.1-27.1C68.4 11.6 57.9 8.6 47.2 8.1"
            pathLength={1}
          />
        </g>
      ) : null}

      {/* ---- the note, in the same user units as the ring it belongs to ---- */}
      {ringNode && ringNote ? (
        <RingNote note={ringNote} x={ringNode.x} y={ringNode.y} size={size} />
      ) : null}

      {/* ---- nodes ---- */}
      <g className="nv__nodes">
        {layout.nodes.map((n) => {
          const isFocus = focus === n.id
          const isNeighbour = neighbours?.has(n.id) ?? false
          const dim = dimming && !isFocus && !isNeighbour

          const cls = [
            'nv__node',
            n.vulnerable && 'is-uncovered',
            isFocus && 'is-focus',
            isFocus && isolated && 'is-isolated',
            !isFocus && isNeighbour && dimming && 'is-near',
            dim && 'is-dim',
          ]
            .filter(Boolean)
            .join(' ')

          return (
            <g
              key={n.id}
              className={cls}
              transform={`translate(${n.x} ${n.y})`}
              onPointerEnter={
                compact
                  ? undefined
                  : () => {
                      setHovered(n.id)
                      onHover?.(n.id)
                    }
              }
              onClick={
                compact ? undefined : () => onSelect?.(selected === n.id ? null : n.id)
              }
            >
              {!compact ? <circle className="nv__hit" r={Math.max(n.r + 6, 9)} /> : null}
              {isFocus ? <circle className="nv__halo" r={Math.max(n.r * 2.6, 11)} /> : null}
              <circle className="nv__dot" r={isFocus ? n.r * 1.5 + 1.2 : n.r} />
            </g>
          )
        })}
      </g>
    </svg>
  )
}

/**
 * The handwritten label for the ringed merchant.
 *
 * Placed radially outward from the node it names, in the figure's own user
 * units, so it sits beside its target at any ring position. The drawn ellipse
 * is the pointer; there is no leader line, because a leader long enough to
 * reach a corner has to cross the whole chord field to get there — which is
 * what the previous two attempts at this did.
 */
function RingNote({
  note,
  x,
  y,
  size,
}: {
  note: string
  x: number
  y: number
  size: number
}) {
  const cx = size / 2
  const cy = size / 2
  const a = Math.atan2(y - cy, x - cx)
  const r = Math.hypot(x - cx, y - cy) + 30

  const pad = 16
  const lx = clamp(cx + Math.cos(a) * r, pad, size - pad)
  const ly = clamp(cy + Math.sin(a) * r, pad + 10, size - pad)

  const c = Math.cos(a)
  const anchor = c > 0.3 ? 'start' : c < -0.3 ? 'end' : 'middle'

  return (
    <text className="nv__note-text" x={lx} y={ly} textAnchor={anchor}>
      {note}
    </text>
  )
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(Math.max(v, lo), hi)
}

/** SVG arc path along a circle, used for the tier bands. */
function arc(cx: number, cy: number, r: number, from: number, to: number): string {
  const x1 = cx + Math.cos(from) * r
  const y1 = cy + Math.sin(from) * r
  const x2 = cx + Math.cos(to) * r
  const y2 = cy + Math.sin(to) * r
  const large = to - from > Math.PI ? 1 : 0
  return `M${x1} ${y1} A${r} ${r} 0 ${large} 1 ${x2} ${y2}`
}

/** Keep band labels reading outward rather than overlapping the ring. */
function labelAnchor(angle: number): 'start' | 'middle' | 'end' {
  const c = Math.cos(angle)
  if (c > 0.3) return 'start'
  if (c < -0.3) return 'end'
  return 'middle'
}
