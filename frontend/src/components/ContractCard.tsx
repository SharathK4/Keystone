/**
 * The contract card.
 *
 * A physical object: a rounded card with an organic, living surface, the
 * Keystone wordmark, and the two facts a merchant cares about first — how much,
 * and for how long. It is the only saturated colour in the entire product and
 * the only place the brand appears.
 *
 * The surface is generated, not an image. `feTurbulence` is seeded from the
 * merchant id, so m0005's card and m0071's card are visibly different objects
 * while staying obviously the same product — the card really is issued to that
 * merchant rather than being a template with a name dropped into it.
 */

import { useMemo } from 'react'
import { hours, inr } from '../lib/format'
import './contract-card.css'

interface Props {
  merchantId: string
  amount: number
  durationHours: number
  /** Repayment summary, e.g. "Single repayment at term". */
  terms: string
  status?: string
  /** Landing state: smaller type, no status pill. */
  compact?: boolean
  /** Fill the stage rather than sitting at its natural card ratio. */
  fill?: boolean
}

/** Stable small integer from a merchant id, so a card looks the same each load. */
function seedOf(id: string): number {
  let h = 0
  for (let i = 0; i < id.length; i += 1) h = (h * 31 + id.charCodeAt(i)) % 9973
  return h
}

export function ContractCard({
  merchantId,
  amount,
  durationHours,
  terms,
  status,
  compact,
  fill,
}: Props) {
  const seed = useMemo(() => seedOf(merchantId), [merchantId])
  const filterId = `card-organic-${seed}`

  return (
    <div className={`ctc${compact ? ' ctc--compact' : ''}${fill ? ' ctc--fill' : ''}`}>
      <div className="ctc__glow" aria-hidden="true" />

      <div className="ctc__card">
        {/* The living surface. */}
        <svg className="ctc__texture" aria-hidden="true" preserveAspectRatio="xMidYMid slice">
          <defs>
            <filter id={filterId} x="-20%" y="-20%" width="140%" height="140%">
              <feTurbulence
                type="fractalNoise"
                baseFrequency="0.011 0.019"
                numOctaves={4}
                seed={seed}
                result="noise"
              />
              {/* Pull the noise into warm bands rather than grey mush. */}
              <feColorMatrix
                in="noise"
                type="matrix"
                values="
                  1.9 0   0   0  -0.18
                  0.9 0.5 0   0  -0.24
                  0.2 0.1 0.3 0  -0.14
                  0   0   0   0   1"
                result="warm"
              />
              <feGaussianBlur in="warm" stdDeviation="1.1" />
            </filter>
          </defs>
          <rect width="100%" height="100%" filter={`url(#${filterId})`} />
        </svg>

        <div className="ctc__sheen" aria-hidden="true" />

        <div className="ctc__body">
          <header className="ctc__top">
            <span className="ctc__brand">Keystone</span>
            {status && !compact ? <span className="ctc__status">{status}</span> : null}
          </header>

          <footer className="ctc__bottom">
            <div className="ctc__amount">
              <span className="ctc__amount-value num">{inr(amount)}</span>
              <span className="ctc__amount-unit">
                / <span className="num">{hours(durationHours)}</span>
              </span>
            </div>
            <span className="ctc__terms">{terms}</span>
            <span className="ctc__for num">Issued to {merchantId}</span>
          </footer>
        </div>
      </div>
    </div>
  )
}
