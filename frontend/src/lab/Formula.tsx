/**
 * A rule, typeset as mathematics rather than shown as code.
 *
 * Division becomes a fraction, a power a superscript, `sqrt` a radical, `abs`
 * bars, and a comparison an Iverson bracket - [need ≤ budget] is 1 when true,
 * 0 when not - which is exactly what the lab computes. Clue names are set in
 * the display italic and carry their plain-words meaning on hover.
 */

import type { ReactNode } from 'react'
import type { Clue, Tree } from './api'

interface Props {
  tree: Tree
  clues?: Clue[]
  size?: 'xl' | 'lg' | 'md' | 'sm'
  className?: string
}

const CMP: Record<string, string> = { '<': '<', '<=': '≤', '>': '>', '>=': '≥' }
const PREC: Record<string, number> = { '+': 1, '-': 1, '*': 2, '/': 4, '**': 3 }
const SUP: Record<string, string> = {
  '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴', '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹', '-': '⁻',
}

function number(v: number): ReactNode {
  const abs = Math.abs(v)
  if (abs !== 0 && (abs >= 1e5 || abs < 1e-3)) {
    const exp = Math.floor(Math.log10(abs))
    const mant = v / 10 ** exp
    const m = Number.isInteger(mant) ? String(mant) : mant.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')
    return (
      <span className="fx-num">
        {m === '1' ? '' : `${m}×`}10<span className="fx-exp">{String(exp).split('').map((c) => SUP[c] ?? c).join('')}</span>
      </span>
    )
  }
  const text = Number.isInteger(v) ? String(v) : v.toFixed(3).replace(/0+$/, '').replace(/\.$/, '')
  return <span className="fx-num">{text}</span>
}

function precedence(t: Tree): number {
  if (t.t === 'bin') return PREC[t.op]
  if (t.t === 'neg') return 2.5
  return 9
}

function render(t: Tree, words: Map<string, Clue>, key = 'r'): ReactNode {
  switch (t.t) {
    case 'num':
      return <span key={key}>{number(t.v)}</span>
    case 'clue': {
      const clue = words.get(t.name)
      return (
        <span
          key={key}
          className={`fx-clue${clue?.group === 'this failure' ? ' is-failure' : ''}`}
          title={clue ? `${t.name}: ${clue.words}` : t.name}
        >
          {t.name.replace('_', ' ')}
        </span>
      )
    }
    case 'neg':
      return (
        <span key={key} className="fx-row">
          <span className="fx-op">−</span>
          {wrap(t.a, 2.5, words, `${key}a`)}
        </span>
      )
    case 'bin': {
      if (t.op === '/') {
        return (
          <span key={key} className="fx-frac">
            <span className="fx-frac__top">{render(t.a, words, `${key}a`)}</span>
            <span className="fx-frac__bar" aria-hidden="true" />
            <span className="fx-frac__bottom">{render(t.b, words, `${key}b`)}</span>
          </span>
        )
      }
      if (t.op === '**') {
        return (
          <span key={key} className="fx-row">
            {wrap(t.a, 3.5, words, `${key}a`)}
            <sup className="fx-sup">{render(t.b, words, `${key}b`)}</sup>
          </span>
        )
      }
      const p = PREC[t.op]
      const sign = t.op === '*' ? '·' : t.op === '-' ? '−' : '+'
      return (
        <span key={key} className="fx-row">
          {wrap(t.a, p, words, `${key}a`)}
          <span className={`fx-op${t.op === '*' ? ' is-dot' : ''}`}>{sign}</span>
          {wrap(t.b, t.op === '-' ? p + 0.5 : p, words, `${key}b`)}
        </span>
      )
    }
    case 'cmp':
      return (
        <span key={key} className="fx-row fx-iverson" title="1 when true, 0 when not">
          <span className="fx-bracket">[</span>
          {render(t.a, words, `${key}a`)}
          <span className="fx-op">{CMP[t.op]}</span>
          {render(t.b, words, `${key}b`)}
          <span className="fx-bracket">]</span>
        </span>
      )
    case 'fn': {
      const [a, b] = t.args
      if (t.name === 'sqrt') {
        return (
          <span key={key} className="fx-row fx-sqrt">
            <span className="fx-radical">√</span>
            <span className="fx-sqrt__under">{render(a, words, `${key}a`)}</span>
          </span>
        )
      }
      if (t.name === 'abs') {
        return (
          <span key={key} className="fx-row">
            <span className="fx-bar">|</span>
            {render(a, words, `${key}a`)}
            <span className="fx-bar">|</span>
          </span>
        )
      }
      return (
        <span key={key} className="fx-row">
          <span className="fx-fn">{t.name}</span>
          <span className="fx-paren">(</span>
          {render(a, words, `${key}a`)}
          {b ? (
            <>
              <span className="fx-comma">,</span>
              {render(b, words, `${key}b`)}
            </>
          ) : null}
          <span className="fx-paren">)</span>
        </span>
      )
    }
  }
}

function wrap(t: Tree, outer: number, words: Map<string, Clue>, key: string): ReactNode {
  if (precedence(t) >= outer) return render(t, words, key)
  return (
    <span key={key} className="fx-row">
      <span className="fx-paren">(</span>
      {render(t, words, key)}
      <span className="fx-paren">)</span>
    </span>
  )
}

export function Formula({ tree, clues = [], size = 'md', className = '' }: Props) {
  const words = new Map(clues.map((c) => [c.name, c]))
  return (
    <span className={`fx fx--${size} ${className}`.trim()} role="math">
      {render(tree, words)}
    </span>
  )
}
