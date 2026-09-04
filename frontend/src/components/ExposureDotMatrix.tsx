/**
 * The exposure field.
 *
 * One hundred merchants, one hundred dots. Two things are encoded and they are
 * deliberately different things, because the interesting result lives in the
 * gap between them:
 *
 *   position   how thinly the merchant covers its own book, thinnest first,
 *              reading left to right and top to bottom. Merchants with nothing
 *              falling due inside the horizon have no cover ratio at all, so
 *              they sit at the end as their own band rather than being scored.
 *
 *   ink        what this particular shock did to it.
 *
 * Reading them together is the point. The merchant a shock actually reaches is
 * usually *not* the thinnest-covered one - it is the one standing downstream of
 * the payment that failed - and putting the field in cover order is what makes
 * that visible instead of asserted. An earlier version sorted by state, which
 * made three tidy blocks and hid exactly this.
 *
 * `origins` is the merchant the shock starts at, marked with a ring rather than
 * a fourth colour.
 */

import { useMemo } from 'react'
import type { AffectedMerchant, MerchantView } from '../api/types'
import { count, inr } from '../lib/format'
import './exposure-dot-matrix.css'

interface Props {
  merchants: MerchantView[]
  affected: AffectedMerchant[]
  origins: string[]
  /** Landing state: smaller dots, no scale caption. */
  compact?: boolean
  /** Invert the ink for a dark tile. */
  onDark?: boolean
}

type State = 'reached' | 'thin' | 'covered' | 'idle'

const ORDER: State[] = ['reached', 'thin', 'covered', 'idle']

const LABEL: Record<State, string> = {
  reached: 'reached by this shock',
  thin: 'owes more than it can cover',
  covered: 'clears its own book',
  idle: 'nothing due this window',
}

export function ExposureDotMatrix({ merchants, affected, origins, compact, onDark }: Props) {
  const { dots, tally } = useMemo(() => {
    const hit = new Map(affected.map((a) => [a.merchant_id, a]))
    const originIds = new Set(origins)

    const rows = merchants.map((m) => {
      const idle = m.cover_ratio === null
      const h = hit.get(m.merchant_id)
      const state: State = h ? 'reached' : m.vulnerable ? 'thin' : idle ? 'idle' : 'covered'
      return {
        id: m.merchant_id,
        state,
        origin: originIds.has(m.merchant_id),
        cover: m.cover_ratio,
        payables: m.payables_in_horizon,
        hit: h,
      }
    })

    // Position is the cover ordering, not the state. Merchants with no cover
    // ratio are unscored, so they trail the field in id order.
    rows.sort((a, b) => {
      if ((a.cover === null) !== (b.cover === null)) return a.cover === null ? 1 : -1
      if (a.cover !== null && b.cover !== null && a.cover !== b.cover) return a.cover - b.cover
      return a.id.localeCompare(b.id)
    })

    const tally = ORDER.map((state) => ({
      state,
      n: rows.filter((r) => r.state === state).length,
    })).filter((t) => t.n > 0 || t.state === 'reached')

    return { dots: rows, tally }
  }, [merchants, affected, origins])

  const reachedValue = affected.reduce((s, a) => s + a.disrupted_value, 0)
  const nReached = tally.find((t) => t.state === 'reached')?.n ?? 0

  return (
    <figure className={`dm${compact ? ' dm--compact' : ''}${onDark ? ' dm--dark' : ''}`}>
      <div className="dm__field">
        <div className="dm__grid" role="img" aria-label={ariaLabel(tally, merchants.length)}>
          {dots.map((d) => (
            <span
              key={d.id}
              className={`dm__dot dm__dot--${d.state}${d.origin ? ' is-origin' : ''}`}
              title={title(d)}
            />
          ))}
        </div>

        {compact ? null : (
          <div className="dm__scale" aria-hidden="true">
            <span>thinnest cover</span>
            <i />
            <span>clears its book</span>
            <i />
            <span>nothing due</span>
          </div>
        )}
      </div>

      <figcaption className="dm__legend">
        {compact ? null : (
          <p className="dm__caption">
            One dot per merchant, laid out by how thinly it covers its own payments — thinnest
            first. Ink is what this shock did.
          </p>
        )}

        {tally.map(({ state, n }) => (
          <div className={`dm__row dm__row--${state}`} key={state}>
            <span className="dm__swatch" aria-hidden="true" />
            <span className="dm__n num">{count(n)}</span>
            <span className="dm__label">{LABEL[state]}</span>
            {state === 'reached' && !compact ? (
              <span className="dm__aside num">
                {n > 0 ? `${inr(reachedValue)} of payments delayed` : 'no merchant reached'}
              </span>
            ) : null}
          </div>
        ))}

        {!compact && nReached > 0 ? (
          <p className="dm__note">
            The merchants a shock reaches are not the thinnest-covered ones. They are the ones
            standing downstream of the payment that failed.
          </p>
        ) : null}
      </figcaption>
    </figure>
  )
}

function ariaLabel(tally: { state: State; n: number }[], total: number): string {
  const parts = tally.map((t) => `${t.n} ${LABEL[t.state]}`)
  return `${total} merchants ordered by cover ratio: ${parts.join(', ')}`
}

interface Dot {
  id: string
  cover: number | null
  payables: number
  hit: AffectedMerchant | undefined
}

function title(d: Dot): string {
  const cover = d.cover === null ? 'nothing due in horizon' : `cover ${d.cover.toFixed(2)}×`
  const owed = d.payables > 0 ? `, ${inr(d.payables)} due` : ''
  if (d.hit) {
    const when =
      d.hit.time_to_constraint_hours !== null
        ? `, constrained at ${Math.round(d.hit.time_to_constraint_hours)}h`
        : ''
    return `${d.id} — ${cover}${owed}${when}`
  }
  return `${d.id} — ${cover}${owed}`
}
