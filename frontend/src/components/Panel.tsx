/**
 * A landing card.
 *
 * This layer sells the product; it does not report on it. A card carries a
 * title, one line of plain English and a single rich visual — and no figures at
 * all. Every number on this page lives one click away, which is the entire
 * point of the two layers: the first says what the thing *is*, the second
 * proves it.
 *
 * The whole surface is the control, so there is no button to hunt for and
 * nothing competing with the artwork.
 */

import type { KeyboardEvent, ReactNode } from 'react'
import './panel.css'

interface PanelProps {
  index: number
  title: string
  subtitle: string
  children: ReactNode
  /** One line on the bottom rule: what opening this card will show. */
  reveals: string
  onOpen: () => void
  className?: string
}

export function Panel({
  index,
  title,
  subtitle,
  children,
  reveals,
  onOpen,
  className = '',
}: PanelProps) {
  const keyOpen = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onOpen()
    }
  }

  return (
    <section
      className={`card ${className}`.trim()}
      style={{ ['--card-delay' as string]: `${160 + index * 100}ms` }}
      role="button"
      tabIndex={0}
      aria-label={`${title}. ${reveals}`}
      onClick={onOpen}
      onKeyDown={keyOpen}
    >
      <header className="card__head">
        <span className="card__index num">{String(index).padStart(2, '0')}</span>
        <h2 className="card__title">{title}</h2>
        <p className="card__subtitle">{subtitle}</p>
      </header>

      <div className="card__stage">{children}</div>

      <footer className="card__foot">
        <span className="card__reveals">{reveals}</span>
        <span className="card__open" aria-hidden="true">
          <i />
        </span>
      </footer>
    </section>
  )
}
