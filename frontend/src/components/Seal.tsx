/**
 * Where a figure came from.
 *
 * Every number in an analysis is one of four kinds, and the kind is derived
 * from the endpoint that produced it, never written by hand: replayed offline
 * on a frozen artifact, inferred from payment history, computed now on request,
 * or executed in the PayPal sandbox. The seal says which, and names the thing
 * it came from (an artifact id, a PayPal reference, a measured time).
 */

import type { ReactNode } from 'react'
import { KIND_LABEL, provenanceFor } from '../api/provenance'
import './seal.css'

interface Props {
  /** "METHOD /path", the /api/v1 prefix omitted. */
  src: string
  refId?: string | null
  children?: ReactNode
}

export function Seal({ src, refId, children }: Props) {
  const kind = provenanceFor(src)
  return (
    <span className={`seal seal--${kind}`} data-kind={kind} data-src={src}>
      <i aria-hidden="true" />
      <b>{KIND_LABEL[kind]}</b>
      {refId ? <span className="seal__ref num">{refId}</span> : null}
      {children ? <span className="seal__note">{children}</span> : null}
    </span>
  )
}
