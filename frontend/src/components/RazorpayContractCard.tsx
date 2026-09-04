/**
 * The contract card.
 *
 * A physical object: a rounded card with an organic, living surface, the
 * Razorpay wordmark, and the two facts a merchant cares about first — how much,
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
import './razorpay-contract-card.css'

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

export function RazorpayContractCard({
  merchantId,
  amount,
  durationHours,
  terms,
  status,
  compact,
  fill,
}: Props) {
  const seed = useMemo(() => seedOf(merchantId), [merchantId])
  const filterId = `rzp-organic-${seed}`

  return (
    <div className={`rzpc${compact ? ' rzpc--compact' : ''}${fill ? ' rzpc--fill' : ''}`}>
      <div className="rzpc__glow" aria-hidden="true" />

      <div className="rzpc__card">
        {/* The living surface. */}
        <svg className="rzpc__texture" aria-hidden="true" preserveAspectRatio="xMidYMid slice">
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

        <div className="rzpc__sheen" aria-hidden="true" />

        <div className="rzpc__body">
          <header className="rzpc__top">
            <span className="rzpc__brand">Razorpay</span>
            {status && !compact ? <span className="rzpc__status">{status}</span> : null}
          </header>

          <footer className="rzpc__bottom">
            <div className="rzpc__amount">
              <span className="rzpc__amount-value num">{inr(amount)}</span>
              <span className="rzpc__amount-unit">
                / <span className="num">{hours(durationHours)}</span>
              </span>
            </div>
            <span className="rzpc__terms">{terms}</span>
            <span className="rzpc__for num">Issued to {merchantId}</span>
          </footer>
        </div>
      </div>
    </div>
  )
}
