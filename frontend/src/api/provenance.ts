/**
 * Every figure carries one of four provenance kinds, derived from the class of
 * the endpoint it came from and never typed by hand.
 */

export type ProvenanceKind = 'computed_now' | 'replayed_offline' | 'executed_paypal_sandbox' | 'inferred'

export const KIND_LABEL: Record<ProvenanceKind, string> = {
  computed_now: 'Computed now',
  replayed_offline: 'Replayed offline',
  executed_paypal_sandbox: 'Executed in PayPal sandbox',
  inferred: 'Inferred',
}

/** `source` is "METHOD /path" with the /api/v1 prefix omitted. First match wins. */
const CLASSES: [RegExp, ProvenanceKind][] = [
  [/^POST \/scenarios\/analyze$/, 'computed_now'],
  [/^GET \/network\/dependencies$/, 'inferred'],
  [/^GET \/execution\/reentries/, 'computed_now'],
  [/^GET \/execution\/ledger/, 'executed_paypal_sandbox'],
  [/^POST \/execution\/plans\/[^/]+\/executions$/, 'executed_paypal_sandbox'],
  [/^POST \/execution\/records\/[^/]+\/refresh$/, 'executed_paypal_sandbox'],
  // A plan Ask Keystone just found is priced from an analysis run on request.
  [/^GET \/execution\/plans\/adhoc-/, 'computed_now'],
  [/^GET \/(network|scenarios|structure|offers|execution\/plans)/, 'replayed_offline'],
]

export function provenanceFor(source: string): ProvenanceKind {
  const path = source.split('?')[0]
  for (const [re, kind] of CLASSES) if (re.test(path)) return kind
  throw new Error(`no provenance class for ${source}`)
}
