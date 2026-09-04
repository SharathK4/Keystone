/**
 * Offer copy and treatment.
 *
 * The backend supplies the *structure* of an offer - type, amount, duration,
 * repayment terms, indicative cost, eligibility - but no customer-facing title.
 * Copy is presentation, so it lives here.
 *
 * Keyed on the **scenario family** first, because that is what actually varies:
 * every offer in this snapshot is a `liquidity_injection`, but the reason it is
 * being offered differs completely between a missed inflow and a supplier
 * failure. Keying on intervention type alone would make all seven scenarios
 * read identically. Intervention type is the fallback.
 *
 * This is a lookup, not logic. If the contract later grows `title`/`subtitle`
 * fields, `resolveOfferCopy` already prefers them.
 *
 * Every line is written for a merchant being offered support ahead of a
 * squeeze, not warned about one. No "risk", no "distress", no "default", no
 * "emergency", no "vulnerable". The merchant is not the problem in this
 * product; the timing of their payment cycle is.
 */

export type OfferAccent = 'bridge' | 'window' | 'continuity' | 'align'

export interface OfferCopy {
  title: string
  subtitle: string
  /** What the money does, in the merchant's own terms. */
  purpose: string
  /** Why this is being shown now. Never a warning. */
  occasion: string
  accent: OfferAccent
}

const BY_FAMILY: Record<string, OfferCopy> = {
  single_missed_inflow: {
    title: 'Bridge the payment cycle',
    subtitle: 'Cover the gap left by an inflow that has not landed.',
    purpose: 'Keeps your outgoing payments on their original dates while a receipt catches up.',
    occasion: 'An expected receipt is running behind your payables.',
    accent: 'bridge',
  },
  delayed_inflow: {
    title: 'Bridge the payment cycle',
    subtitle: 'Cover the gap while a receipt arrives late.',
    purpose: 'Holds your schedule steady across a delayed settlement.',
    occasion: 'An expected receipt is arriving later than your payables fall due.',
    accent: 'bridge',
  },
  partial_payment: {
    title: 'Top up a short settlement',
    subtitle: 'Cover the balance of a payment that arrived light.',
    purpose: 'Makes up the shortfall so your own obligations clear in full and on time.',
    occasion: 'A settlement landed below its invoiced amount.',
    accent: 'bridge',
  },
  supplier_failure: {
    title: 'Extend the payment window',
    subtitle: 'More room between what you receive and what you owe.',
    purpose: 'Lines your supplier due dates up with your incoming settlements.',
    occasion: 'A counterparty in your chain has stopped paying on schedule.',
    accent: 'window',
  },
  liquidity_drain: {
    title: 'Maintain payment continuity',
    subtitle: 'Temporary liquidity support, sized to your upcoming obligations.',
    purpose: 'Keeps scheduled payments on time through a tight settlement window.',
    occasion: 'Your balance is being drawn down faster than it is replenished.',
    accent: 'continuity',
  },
  concentrated_shock: {
    title: 'Maintain payment continuity',
    subtitle: 'Support timed to a concentrated demand on your balance.',
    purpose: 'Absorbs a single large call on your liquidity without moving your payment dates.',
    occasion: 'A large obligation is concentrated in one settlement window.',
    accent: 'continuity',
  },
  multi_node_shock: {
    title: 'Keep the chain moving',
    subtitle: 'Liquidity placed where several payment paths meet.',
    purpose: 'Holds a shared point in the payment chain steady so downstream schedules hold.',
    occasion: 'Several counterparties are settling late at once.',
    accent: 'continuity',
  },
}

const BY_TYPE: Record<string, OfferCopy> = {
  liquidity_injection: {
    title: 'Maintain payment continuity',
    subtitle: 'Temporary liquidity support, sized to your upcoming obligations.',
    purpose: 'Keeps scheduled payments on time through a tight settlement window.',
    occasion: 'Your upcoming obligations sit close to your available balance.',
    accent: 'continuity',
  },
  credit_line_increase: {
    title: 'Extend available headroom',
    subtitle: 'Additional working headroom for the current cycle.',
    purpose: 'Raises available balance so scheduled payments clear without delay.',
    occasion: 'Your current headroom is tight against this cycle.',
    accent: 'continuity',
  },
  receivable_acceleration: {
    title: 'Bridge the payment cycle',
    subtitle: 'Unlock receivables already owed to you, earlier.',
    purpose: 'Brings forward money you are already due, ahead of its settlement date.',
    occasion: 'You are owed money that settles after your own payments fall due.',
    accent: 'bridge',
  },
  supplier_term_extension: {
    title: 'Extend the payment window',
    subtitle: 'More room between what you receive and what you owe.',
    purpose: 'Moves supplier due dates to line up with your incoming settlements.',
    occasion: 'Your payables fall due ahead of your receipts.',
    accent: 'window',
  },
  repayment_restructure: {
    title: 'Align repayment with settlement flow',
    subtitle: 'Repayment scheduled around when money actually arrives.',
    purpose: 'Reshapes instalments to match the rhythm of your settlements.',
    occasion: 'Your repayment dates and your settlement dates are out of step.',
    accent: 'align',
  },
}

const FALLBACK: OfferCopy = {
  title: 'Business liquidity offer',
  subtitle: 'Designed around your payment cycle.',
  purpose: 'Support timed to your upcoming obligations.',
  occasion: 'Based on the payment activity in your network.',
  accent: 'continuity',
}

/**
 * @param interventionType `intervention_type` from the offer contract.
 * @param scenarioFamily   the analysed scenario; the stronger signal.
 * @param supplied         anything the backend already provides, which wins.
 */
export function resolveOfferCopy(
  interventionType: string,
  scenarioFamily?: string | null,
  supplied?: Partial<OfferCopy> | null,
): OfferCopy {
  const base =
    (scenarioFamily ? BY_FAMILY[scenarioFamily] : undefined) ??
    BY_TYPE[interventionType] ??
    FALLBACK
  if (!supplied) return base
  return { ...base, ...stripUndefined(supplied) }
}

function stripUndefined(o: Partial<OfferCopy>): Partial<OfferCopy> {
  return Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined))
}

/** Repayment structure, said plainly. */
export function repaymentSummary(structure: string, instalments: number): string {
  if (structure === 'bullet') return 'Single repayment at term'
  if (instalments > 1) return `${instalments} scheduled instalments`
  return structure.charAt(0).toUpperCase() + structure.slice(1)
}
