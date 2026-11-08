/**
 * Card 05 — the proof.
 *
 * The other four cards say what the system found; this one says why to believe
 * it, and where not to. It makes three claims a reader would otherwise have to
 * take on trust, each backed by checks the backend stamped when the proof
 * artifact was built. The count beside a claim is those checks, read from the
 * artifact, never typed here; a failed or missing check shows as one. The
 * certificate, with the weaknesses listed first, is one click away.
 */

import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { ProofView } from '../api/schema'
import { CLAIMS, tally } from '../lib/proofClaims'
import { Panel } from './Panel'
import { TexturedTile } from './TexturedTile'
import './proof-panel.css'

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

  return (
    <Panel
      index={5}
      title="The proof"
      subtitle="What we checked, and what we can’t promise."
      reveals="See the checks"
      onOpen={onOpen}
      className="card--wide"
    >
      <TexturedTile tone="pearl" seed={proof?.artifact_id ?? 'proof'} className="pp">
        <div className="pp__body">
          <svg className="pp__seal" viewBox="0 0 200 200" aria-hidden="true">
            <defs>
              <path id="pp-ring" d="M100,100 m-74,0 a74,74 0 1,1 148,0 a74,74 0 1,1 -148,0" />
            </defs>
            <circle cx="100" cy="100" r="92" className="pp__ring" />
            <circle cx="100" cy="100" r="58" className="pp__ring pp__ring--inner" />
            <text className="pp__ringtext">
              <textPath href="#pp-ring" startOffset="0">
                KEYSTONE · INSPECTED · CONTENT ADDRESSED ·
              </textPath>
            </text>
            <path d="M78 101 l15 15 l31 -34" className="pp__tick" />
          </svg>
          {proof ? (
            <ul className="pp__claims">
              {CLAIMS.map((c) => {
                const { passed, total } = tally(proof, c)
                const held = passed === total
                return (
                  <li key={c.title} className={held ? '' : 'is-failed'}>
                    <i aria-hidden="true">{held ? '✓' : '†'}</i>
                    <span className="pp__claim">{c.title}</span>
                    <span className="pp__count num">
                      {passed}/{total} checks pass
                    </span>
                  </li>
                )
              })}
              <li className="pp__more">and what we can’t promise, inside</li>
            </ul>
          ) : null}
        </div>
      </TexturedTile>
    </Panel>
  )
}
