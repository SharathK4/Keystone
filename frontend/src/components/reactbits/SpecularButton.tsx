/**
 * Specular Button (React Bits pattern, implemented locally).
 *
 * A specular highlight tracks the pointer across the face of the button, the
 * way light moves on a curved surface. Implemented against CSS custom
 * properties written on pointermove rather than React state, so the highlight
 * follows the cursor without re-rendering the tree behind it.
 *
 * Deliberately restrained: the sheen only exists while the pointer is on the
 * control, and it never plays on its own. This is the page's primary action,
 * not an ornament.
 */

import { useCallback, useRef, type ButtonHTMLAttributes, type ReactNode } from 'react'
import './specular-button.css'

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  children: ReactNode
  tone?: 'product' | 'quiet'
}

export function SpecularButton({ children, tone = 'product', className = '', ...rest }: Props) {
  const ref = useRef<HTMLButtonElement>(null)

  const track = useCallback((event: React.PointerEvent<HTMLButtonElement>) => {
    const node = ref.current
    if (!node) return
    const box = node.getBoundingClientRect()
    node.style.setProperty('--px', `${((event.clientX - box.left) / box.width) * 100}%`)
    node.style.setProperty('--py', `${((event.clientY - box.top) / box.height) * 100}%`)
  }, [])

  const reset = useCallback(() => {
    const node = ref.current
    if (!node) return
    node.style.setProperty('--px', '50%')
    node.style.setProperty('--py', '120%')
  }, [])

  return (
    <button
      {...rest}
      ref={ref}
      onPointerMove={track}
      onPointerLeave={reset}
      className={`specular specular--${tone} ${className}`.trim()}
    >
      <span className="specular__sheen" aria-hidden="true" />
      <span className="specular__label">{children}</span>
    </button>
  )
}
