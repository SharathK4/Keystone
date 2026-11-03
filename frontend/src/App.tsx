/**
 * Two pages, one hash. The walk is the default; the five-card gallery is
 * "Evidence", reached by #evidence and by the hashes its panels always used,
 * so an old link to "#offer" still opens the offer.
 */

import { useEffect, useState } from 'react'
import { KeystonePage } from './components/KeystonePage'
import { QuietPage } from './quiet/QuietPage'

const EVIDENCE = new Set(['evidence', 'network', 'exposure', 'intervention', 'offer', 'proof'])

function onEvidence(): boolean {
  return EVIDENCE.has(window.location.hash.replace('#', ''))
}

export function App() {
  const [evidence, setEvidence] = useState(onEvidence)
  useEffect(() => {
    const sync = () => setEvidence(onEvidence())
    window.addEventListener('hashchange', sync)
    return () => window.removeEventListener('hashchange', sync)
  }, [])
  useEffect(() => {
    if (evidence) window.scrollTo(0, 0)
  }, [evidence])
  return evidence ? <KeystonePage /> : <QuietPage />
}
