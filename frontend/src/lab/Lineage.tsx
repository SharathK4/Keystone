/**
 * The family tree of rules, and the scoreboard, in one picture.
 *
 *   across  the round that wrote the rule (people first, then each round)
 *   up      the share of the week's damage it prevents, averaged over every
 *           test scenario
 *   lines   which rules each one was written from
 *
 * A rule being tested sits on its parent and moves to its running mean as each
 * scenario's replay lands, so the chart climbs or sinks while you watch. The
 * dashed line is Keystone's own search at the same money; the silver line is
 * the best rule people wrote. The best rule's ancestry is lit.
 */

import { useEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent } from 'react'
import { modelName, type Refusal } from './api'
import { runningMean, type LiveRule, type Thinking } from './useLab'

interface Props {
  rules: LiveRule[]
  searchMean: number | null
  generation: number
  thinking: Thinking | null
  refused: Refusal[]
  selected: string | null
  partner: string | null
  bestId: string | null
  onPick: (id: string, event: MouseEvent) => void
  onHover: (id: string | null, at?: { x: number; y: number }) => void
}

const PAD = { l: 62, r: 188, t: 64, b: 46 }
const NODE_R = { people: 6.5, you: 7.5, model: 7.5, mutation: 6 }

interface Placed {
  rule: LiveRule
  x: number
  y: number
  value: number | null
}

export function Lineage({
  rules,
  searchMean,
  generation,
  thinking,
  refused,
  selected,
  partner,
  bestId,
  onPick,
  onHover,
}: Props) {
  const box = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ w: 900, h: 520 })
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    const el = box.current
    if (!el) return
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect
      setSize({ w: Math.max(420, width), h: Math.max(300, height) })
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  // The writing column counts the seconds while the model works.
  useEffect(() => {
    if (!thinking) return
    const timer = window.setInterval(() => setNow(Date.now()), 250)
    return () => window.clearInterval(timer)
  }, [thinking])

  const { w, h } = size
  const columns = Math.max(generation + 1 + (thinking ? 1 : 0), 4)
  const colW = (w - PAD.l - PAD.r) / columns
  const x = (g: number) => PAD.l + (g + 0.5) * colW

  const values = rules.map(runningMean).filter((v): v is number => v !== null)
  const top = Math.max(10, searchMean ?? 0, ...values)
  const hi = Math.ceil((top + 2) / 5) * 5
  const lo = 0
  const y = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo)) * (h - PAD.t - PAD.b)

  const people = rules.filter((r) => r.origin === 'people' && r.status === 'scored')
  const peopleBest = people.length ? Math.max(...people.map((r) => r.mean)) : null

  // Place every rule; nodes in a column that would overlap step sideways.
  const placed = useMemo(() => {
    const byId = new Map<string, Placed>()
    const columnsUsed = new Map<number, Placed[]>()
    const ordered = [...rules].sort((a, b) => a.generation - b.generation)
    for (const rule of ordered) {
      const value = runningMean(rule)
      const parent = rule.parents.map((p) => byId.get(p)).find(Boolean)
      const baseY = value !== null ? y(value) : parent ? parent.y : y((hi + lo) / 2)
      const cx = x(rule.generation)
      const col = columnsUsed.get(rule.generation) ?? []
      let dx = 0
      for (const step of [0, 15, -15, 30, -30, 45, -45, 60, -60]) {
        if (col.every((p) => Math.hypot(p.x - (cx + step), p.y - baseY) >= 14)) {
          dx = step
          break
        }
        dx = step
      }
      const item: Placed = { rule, x: cx + dx, y: baseY, value }
      col.push(item)
      columnsUsed.set(rule.generation, col)
      byId.set(rule.id, item)
    }
    return byId
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rules, w, h, hi, columns])

  // The best rule's ancestry: every rule it descends from.
  const lineage = useMemo(() => {
    const out = new Set<string>()
    const walk = (id: string | null) => {
      if (!id || out.has(id)) return
      out.add(id)
      placed.get(id)?.rule.parents.forEach(walk)
    }
    walk(bestId)
    return out
  }, [placed, bestId])

  const ticks: number[] = []
  for (let v = lo; v <= hi; v += 5) ticks.push(v)

  const refusedBy = new Map<number, Refusal[]>()
  for (const r of refused) {
    const g = r.generation ?? 0
    refusedBy.set(g, [...(refusedBy.get(g) ?? []), r])
  }

  const authors = new Map<number, string>()
  for (const r of rules) {
    if (r.generation > 0 && !authors.has(r.generation)) authors.set(r.generation, r.author)
  }

  // Two reference lines close together: keep their labels apart.
  const searchY = searchMean !== null ? y(searchMean) : null
  const peopleY = peopleBest !== null ? y(peopleBest) : null
  let searchLabelY = searchY ?? 0
  let peopleLabelY = peopleY ?? 0
  if (searchY !== null && peopleY !== null && Math.abs(searchY - peopleY) < 15) {
    const mid = (searchY + peopleY) / 2
    const upper = searchY <= peopleY
    searchLabelY = upper ? mid - 8 : mid + 8
    peopleLabelY = upper ? mid + 8 : mid - 8
  }

  const best = bestId ? placed.get(bestId) : undefined
  const elapsed = thinking ? ((now - thinking.startedAt) / 1000).toFixed(1) : '0'

  return (
    <div className="ln" ref={box}>
      <svg className="ln__svg" width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img"
        aria-label="Every rule tested, by round and by share of damage prevented">
        <defs>
          <linearGradient id="ln-scan" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="#b5462a" stopOpacity="0" />
            <stop offset="50%" stopColor="#b5462a" stopOpacity="0.07" />
            <stop offset="100%" stopColor="#b5462a" stopOpacity="0" />
          </linearGradient>
        </defs>

        {/* ---- grid ---- */}
        {ticks.map((v) => (
          <g key={v} className="ln__tick">
            <line x1={PAD.l} x2={w - PAD.r + 24} y1={y(v)} y2={y(v)} />
            <text x={PAD.l - 14} y={y(v) + 4} textAnchor="end">{v}%</text>
          </g>
        ))}
        <text className="ln__axis" transform={`translate(${18} ${(PAD.t + h - PAD.b) / 2}) rotate(-90)`} textAnchor="middle">
          share of the week’s damage prevented
        </text>

        {/* ---- columns ---- */}
        {Array.from({ length: columns }, (_, g) => {
          const isThinking = thinking && g === generation + 1
          const label = g === 0 ? 'People' : `Round ${g}`
          const who = g === 0 ? 'six rules + yours' : isThinking ? (thinking.writer === 'model' ? 'writing…' : 'mutating…') : authors.get(g) ? modelName(authors.get(g)) : ''
          return (
            <g key={g} className={`ln__col${g > generation && !isThinking ? ' is-empty' : ''}`}>
              {g > 0 ? <line className="ln__colrule" x1={x(g) - colW / 2} x2={x(g) - colW / 2} y1={PAD.t - 30} y2={h - PAD.b} /> : null}
              <text className="ln__colname" x={x(g)} y={PAD.t - 34} textAnchor="middle">{label}</text>
              <text className="ln__colwho" x={x(g)} y={PAD.t - 19} textAnchor="middle">{who}</text>
            </g>
          )
        })}

        {/* ---- the column being written ---- */}
        {thinking ? (
          <g className="ln__writing">
            <rect x={x(generation + 1) - colW / 2 + 6} y={PAD.t - 8} width={colW - 12} height={h - PAD.t - PAD.b + 8} rx={14} />
            <rect className="ln__scan" x={x(generation + 1) - colW / 2 + 6} y={PAD.t - 8} width={colW - 12} height={90} />
            <text x={x(generation + 1)} y={h - PAD.b - 14} textAnchor="middle" className="ln__clock">{elapsed} s</text>
            {thinking.parents.map((pid) => {
              const p = placed.get(pid)
              if (!p) return null
              return (
                <path key={pid} className="ln__reach" d={curve(p.x, p.y, x(generation + 1) - colW / 2 + 10, p.y)} />
              )
            })}
          </g>
        ) : null}

        {/* ---- reference lines ---- */}
        {peopleBest !== null ? (
          <g className="ln__ref is-people">
            <line x1={PAD.l} x2={w - PAD.r + 24} y1={y(peopleBest)} y2={y(peopleBest)} />
            <text x={w - PAD.r + 32} y={peopleLabelY + 4}>
              <tspan className="ln__refval">{peopleBest.toFixed(1)}%</tspan> best rule people wrote
            </text>
          </g>
        ) : null}
        {searchMean !== null ? (
          <g className="ln__ref is-search">
            <line x1={PAD.l} x2={w - PAD.r + 24} y1={y(searchMean)} y2={y(searchMean)} />
            <text x={w - PAD.r + 32} y={searchLabelY + 4}>
              <tspan className="ln__refval">{searchMean.toFixed(1)}%</tspan> Keystone’s search
            </text>
          </g>
        ) : null}

        {/* ---- descent ---- */}
        <g className="ln__edges">
          {[...placed.values()].flatMap((child) =>
            child.rule.parents.map((pid) => {
              const parent = placed.get(pid)
              if (!parent) return null
              const lit = lineage.has(child.rule.id) && lineage.has(pid)
              const d = curve(parent.x, parent.y, child.x, child.y)
              return (
                <path
                  key={`${pid}-${child.rule.id}`}
                  className={`ln__edge${lit ? ' is-lit' : ''}${child.rule.status !== 'scored' ? ' is-new' : ''}`}
                  d={d}
                  style={{ d: `path('${d}')` } as CSSProperties}
                />
              )
            }),
          )}
        </g>

        {/* ---- refused ---- */}
        {[...refusedBy.entries()].map(([g, items]) => (
          <g key={`x${g}`} className="ln__refused">
            {items.slice(0, 8).map((r, i) => (
              <text key={i} x={x(g) - ((Math.min(items.length, 8) - 1) * 9) / 2 + i * 9} y={h - PAD.b + 22} textAnchor="middle">
                <title>{`Refused: ${r.formula} — ${r.reason}`}</title>×
              </text>
            ))}
          </g>
        ))}

        {/* ---- rules ---- */}
        <g className="ln__nodes">
          {[...placed.values()].map(({ rule, x: nx, y: ny }) => {
            const r = NODE_R[rule.origin]
            const isBest = rule.id === bestId
            const classes = [
              'ln__node',
              `is-${rule.origin}`,
              `is-${rule.status}`,
              lineage.has(rule.id) ? 'is-lineage' : '',
              rule.id === selected ? 'is-selected' : '',
              rule.id === partner ? 'is-partner' : '',
              isBest ? 'is-best' : '',
            ].join(' ')
            return (
              <g
                key={rule.id}
                className={classes}
                style={{ transform: `translate(${nx}px, ${ny}px)` }}
                onClick={(e) => onPick(rule.id, e)}
                onMouseEnter={() => onHover(rule.id, { x: nx, y: ny })}
                onMouseLeave={() => onHover(null)}
                role="button"
                aria-label={`${rule.id}: ${rule.formula}`}
              >
                <circle className="ln__hit" r={16} />
                {isBest ? <circle className="ln__halo" r={r + 9} /> : null}
                {rule.status !== 'scored' ? <circle className="ln__pulse" r={r + 3} /> : null}
                <circle className="ln__ring" r={r + 5} />
                <circle className="ln__dot" r={r} />
              </g>
            )
          })}
        </g>

        {/* ---- the best so far ---- */}
        {best && best.value !== null ? (
          <g className="ln__crown" style={{ transform: `translate(${best.x}px, ${best.y}px)` }}>
            <line x1={10} y1={-10} x2={28} y2={-28} />
            <text x={32} y={-32}>best · {best.value.toFixed(1)}%</text>
          </g>
        ) : null}
      </svg>
    </div>
  )
}

function curve(x1: number, y1: number, x2: number, y2: number): string {
  const dx = Math.max(24, (x2 - x1) * 0.5)
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`
}
