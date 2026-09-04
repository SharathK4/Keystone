/**
 * The analytical view behind a panel.
 *
 * Opening a panel does not navigate anywhere. The four-panel composition stays
 * where it is, recedes, and the system behind the chosen panel comes forward on
 * top of it - so the reader never loses the thing they are reading *from*, and
 * closing puts them back exactly where they were.
 *
 * The shell is shared so the four deep views cannot drift into four different
 * layouts. It is deliberately almost empty: a close control, an optional title,
 * an optional row of controls, and the body. Numbered eyebrows and provenance
 * rails were removed - inside an analysis the reader knows where they are, and
 * the small print was competing with the thing they opened it to read.
 */

import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import './expanded-analysis.css'

export type PanelId = 'network' | 'exposure' | 'intervention' | 'offer'

interface Props {
  title: string
  subtitle: string
  children: ReactNode
  /** Controls that belong to the analysis, e.g. the scenario selector. */
  aside?: ReactNode
  /** Header shrinks to the close alone, for a body that has its own headline. */
  bare?: boolean
  onClose: () => void
}

export function ExpandedAnalysis({
  title,
  subtitle,
  children,
  aside,
  bare,
  onClose,
}: Props) {
  const shell = useRef<HTMLDivElement>(null)

  // Escape closes, the body underneath stops scrolling, and focus moves into
  // the view so the keyboard follows the eye.
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    shell.current?.focus({ preventScroll: true })
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [onClose])

  // Portalled to the body on purpose. Any ancestor with a filter, transform or
  // backdrop-filter becomes the containing block for position:fixed - which is
  // exactly what happened here: a saturate() on the page made this overlay size
  // itself to the whole document instead of the viewport, so it could not
  // scroll and its lower half was unreachable.
  return createPortal(
    <div className="xa" role="dialog" aria-modal="true" aria-label={title}>
      <button className="xa__scrim" onClick={onClose} aria-label="Close" />

      <div className="xa__shell" ref={shell} tabIndex={-1}>
        <header className={`xa__head${bare ? ' xa__head--bare' : ''}`}>
          <div className="xa__heading">
            {bare ? null : (
              <>
                <h2 className="xa__title">{title}</h2>
                <p className="xa__subtitle">{subtitle}</p>
              </>
            )}
          </div>

          <button className="xa__close" onClick={onClose} aria-label="Close">
            <span aria-hidden="true" />
          </button>
        </header>

        {/* Controls get their own rule rather than competing with the heading
         * for the same line: the scenario row is as wide as seven names, and
         * sharing the row squeezed the title into a two-word column. */}
        {aside ? <div className="xa__aside">{aside}</div> : null}

        <div className="xa__body">{children}</div>
      </div>
    </div>,
    document.body,
  )
}

/** Section rule used inside the deep views, so their internals also align. */
export function XSection({
  label,
  note,
  children,
  className = '',
}: {
  label: string
  note?: string
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`xs ${className}`.trim()}>
      <div className="xs__head">
        <h3 className="xs__label">{label}</h3>
        {note ? <span className="xs__note">{note}</span> : null}
      </div>
      {children}
    </section>
  )
}
