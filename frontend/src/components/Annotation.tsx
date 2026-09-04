/**
 * The hand-drawn layer: an analyst's marginal note on a printed report.
 *
 * Only the caption lives here now. The marks that have to *point* at something
 * - the ring around a node, the leader to it - are drawn inside the figure in
 * its own user units, because anything positioned in CSS pixels over a square
 * SVG in a wider box drifts off its target.
 *
 * Rules that keep this from turning cartoonish: one weight, one colour, never
 * more than two marks in a panel, and the label set in the text face at small
 * size rather than in a "handwriting" font.
 */

import type { ReactNode } from 'react'
import './annotation.css'

interface MarkProps {
  children: ReactNode
  className?: string
  style?: React.CSSProperties
  /** Delay the ink-on animation so marks land after the panel content. */
  delay?: number
}

/** A caption with a short leader line. The workhorse. */
export function Annotation({ children, className = '', style, delay = 0 }: MarkProps) {
  return (
    <div
      className={`annot ${className}`.trim()}
      style={{ ...style, ['--annot-delay' as string]: `${delay}ms` }}
    >
      {children}
    </div>
  )
}
