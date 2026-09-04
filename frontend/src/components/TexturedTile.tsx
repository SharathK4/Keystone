/**
 * The hero object inside a landing card.
 *
 * One construction, four colourways. Each tile is a physical-feeling slab with
 * a generated surface — `feTurbulence` warped through a colour matrix, so the
 * texture is organic rather than a gradient, and different for every seed while
 * obviously belonging to the same family of objects.
 *
 * The contract card is the one the others are modelled on; giving each card its
 * own tile is what stops three of them reading as diagrams on paper while the
 * fourth reads as a product.
 */

import { useId, type ReactNode } from 'react'
import './textured-tile.css'

export type TileTone = 'ink' | 'slate' | 'clay' | 'ember'

interface Props {
  tone: TileTone
  /** Anything stable — a merchant id, a dataset id — so a tile is reproducible. */
  seed?: string
  children?: ReactNode
  className?: string
  /** Sits above the texture, below the content: the figure the card is about. */
  figure?: ReactNode
}

/** Colour matrices per tone. Row order is R,G,B,A; last column is the offset. */
const MATRIX: Record<TileTone, string> = {
  // deep forest, for structure
  ink: `0.25 0.05 0   0 -0.05
        0.75 0.35 0   0 -0.10
        0.45 0.20 0.2 0 -0.06
        0    0    0   0  1`,
  // deep indigo stone, for consequence
  slate: `0.34 0.15 0.10 0 -0.10
          0.44 0.23 0.14 0 -0.12
          0.76 0.38 0.26 0 -0.10
          0    0    0    0  1`,
  // burnished brass, for capital. Deliberately held back from the contract's
  // orange: the fourth card is the only saturated one in the composition.
  clay: `1.02 0.34 0    0 -0.26
         0.80 0.42 0    0 -0.28
         0.26 0.14 0.12 0 -0.14
         0    0    0    0  1`,
  // razorpay orange, the product
  ember: `1.90 0    0    0 -0.18
          0.90 0.50 0    0 -0.24
          0.20 0.10 0.30 0 -0.14
          0    0    0    0  1`,
}

function seedOf(value: string): number {
  let h = 0
  for (let i = 0; i < value.length; i += 1) h = (h * 31 + value.charCodeAt(i)) % 9973
  return h
}

export function TexturedTile({ tone, seed = 'keystone', children, figure, className = '' }: Props) {
  const uid = useId().replace(/:/g, '')
  const filterId = `tile-${uid}`
  const n = seedOf(seed)

  return (
    <div className={`tile tile--${tone} ${className}`.trim()}>
      <div className="tile__glow" aria-hidden="true" />

      <div className="tile__body">
        <svg className="tile__texture" aria-hidden="true" preserveAspectRatio="xMidYMid slice">
          <defs>
            <filter id={filterId} x="-20%" y="-20%" width="140%" height="140%">
              <feTurbulence
                type="fractalNoise"
                baseFrequency="0.011 0.019"
                numOctaves={4}
                seed={n}
                result="noise"
              />
              <feColorMatrix in="noise" type="matrix" values={MATRIX[tone]} result="toned" />
              <feGaussianBlur in="toned" stdDeviation="1.1" />
            </filter>
          </defs>
          <rect width="100%" height="100%" filter={`url(#${filterId})`} />
        </svg>

        <div className="tile__sheen" aria-hidden="true" />

        {figure ? <div className="tile__figure">{figure}</div> : null}
        {children ? <div className="tile__content">{children}</div> : null}
      </div>
    </div>
  )
}
