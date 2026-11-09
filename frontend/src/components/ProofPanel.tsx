/**
 * Card 06 — the proof.
 *
 * A picture, like cards 01, 03 and 05: an engraved seal in the line-work
 * banknotes and certificates are printed with, a tick at its heart, and one
 * small line underneath - how many of the backend's stamped checks pass, read
 * from the proof artifact, never typed here. The claims, the limits and every
 * check are one click away.
 */

import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { ProofView } from '../api/schema'
import { CLAIMS, tally } from '../lib/proofClaims'
import { Panel } from './Panel'
import { TexturedTile } from './TexturedTile'
import './proof-panel.css'

/** One woven band: copies of a wave around a circle, each shifted in phase. */
function band(r0: number, amp: number, waves: number, copies: number, cx = 200, cy = 200): string[] {
  const out: string[] = []
  for (let j = 0; j < copies; j += 1) {
    const phase = (j / copies) * ((2 * Math.PI) / waves)
    let d = ''
    for (let i = 0; i <= 720; i += 1) {
      const t = (i / 720) * 2 * Math.PI
      const r = r0 + amp * Math.sin(waves * t + phase * waves)
      d += `${i ? 'L' : 'M'}${(cx + r * Math.cos(t)).toFixed(2)},${(cy + r * Math.sin(t)).toFixed(2)}`
    }
    out.push(`${d}Z`)
  }
  return out
}

/** A spirograph rosette (a hypotrochoid), the centre of an engraved seal. */
function rosette(R: number, r: number, d: number, cx = 200, cy = 200): string {
  const turns = r / gcd(R, r)
  let path = ''
  const steps = 3600
  for (let i = 0; i <= steps; i += 1) {
    const t = (i / steps) * 2 * Math.PI * turns
    const x = cx + (R - r) * Math.cos(t) + d * Math.cos(((R - r) / r) * t)
    const y = cy + (R - r) * Math.sin(t) - d * Math.sin(((R - r) / r) * t)
    path += `${i ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)}`
  }
  return path
}

function gcd(a: number, b: number): number {
  return b === 0 ? a : gcd(b, a % b)
}

export function ProofPanel({ onOpen }: { onOpen: () => void }) {
  const [proof, setProof] = useState<ProofView | null>(null)

  useEffect(() => {
    let live = true
    api
      .proof()
      .then((p) => live && setProof(p))
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [])

  const art = useMemo(
    () => ({
      outer: band(164, 7, 48, 9),
      middle: band(128, 9, 30, 11),
      inner: rosette(90, 33, 46),
    }),
    [],
  )

  const counts = proof ? CLAIMS.map((c) => tally(proof, c)) : []
  const passed = counts.reduce((a, c) => a + c.passed, 0)
  const total = counts.reduce((a, c) => a + c.total, 0)

  return (
    <Panel
      index={6}
      title="The proof"
      subtitle="What we checked, and what we can’t promise."
      reveals="See the checks"
      onOpen={onOpen}
    >
      <TexturedTile tone="plum" seed={proof?.artifact_id ?? 'proof'} className="pp">
        <div className="pp__body">
          <svg className="pp__seal" viewBox="0 0 400 400" aria-hidden="true">
            <circle cx="200" cy="200" r="184" className="pp__rim" />
            <circle cx="200" cy="200" r="176" className="pp__rim is-thin" />
            {art.outer.map((d, i) => (
              <path key={`o${i}`} d={d} className="pp__line" />
            ))}
            <circle cx="200" cy="200" r="146" className="pp__rim is-thin" />
            {art.middle.map((d, i) => (
              <path key={`m${i}`} d={d} className="pp__line" />
            ))}
            <circle cx="200" cy="200" r="110" className="pp__rim is-thin" />
            <path d={art.inner} className="pp__line is-rosette" />
            <circle cx="200" cy="200" r="46" className="pp__boss" />
            <path d="M180 201 l14 14 l27 -30" className="pp__tick" />
          </svg>
          {proof ? (
            <p className={`pp__caption${passed === total ? '' : ' is-failed'}`}>
              <span className="num">{passed}</span> of <span className="num">{total}</span> checks pass
            </p>
          ) : null}
        </div>
      </TexturedTile>
    </Panel>
  )
}
