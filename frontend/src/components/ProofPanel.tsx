/**
 * Card 05 — the proof.
 *
 * The other four cards say what the system found; this one says why to believe
 * it, and where not to. Like the others it carries no figures: a seal and the
 * names of the checks the backend stamped when the proof artifact was built.
 * The certificate, with the weaknesses listed first, is one click away.
 */

import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { ProofView } from '../api/schema'
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

  const lines = (proof?.audit ?? []).slice(0, 14)
  return (
    <Panel
      index={5}
      title="The proof"
      subtitle="What was checked and measured, and what failed, before any number above is trusted."
      reveals="Open the certificate"
      onOpen={onOpen}
      className="card--wide"
    >
      <TexturedTile tone="graphite" seed={proof?.artifact_id ?? 'proof'} className="pp">
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
          <ul className="pp__lines">
            {lines.map((a) => (
              <li key={a.name} className={a.passed ? '' : 'is-failed'}>
                <i aria-hidden="true">{a.passed ? '✓' : '†'}</i>
                {a.name.replace(/_/g, ' ')}
              </li>
            ))}
            {proof ? <li className="pp__more">and the weaknesses, listed first</li> : null}
          </ul>
        </div>
      </TexturedTile>
    </Panel>
  )
}
