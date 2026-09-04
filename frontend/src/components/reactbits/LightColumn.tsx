/**
 * The light column above the wordmark.
 *
 * A single soft shaft descending onto the title, the way a skylight falls on an
 * object in a gallery. Architectural rather than atmospheric: one wide
 * monochrome gradient with a very long falloff, a narrower core inside it, and
 * a faint pool where the light lands.
 *
 * Deliberately not a beam of particles and not a glow. It moves on a 40-second
 * cycle by a few pixels and a few percent of opacity - enough that the page is
 * not dead, far too slow to read as an animation. Nothing about it is meant to
 * suggest that a machine is thinking.
 */

import './light-column.css'

interface Props {
  /** 0-1. Kept low; this is the room's light, not an effect. */
  intensity?: number
  className?: string
}

export function LightColumn({ intensity = 1, className = '' }: Props) {
  return (
    <div
      className={`lightcol ${className}`.trim()}
      style={{ ['--lc-i' as string]: intensity }}
      aria-hidden="true"
    >
      <span className="lightcol__wash" />
      <span className="lightcol__core" />
      <span className="lightcol__pool" />
    </div>
  )
}
