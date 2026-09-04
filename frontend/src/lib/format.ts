/**
 * Presentation of backend figures. No arithmetic on money beyond choosing a
 * unit and rounding for display - if a number needs to be derived, the backend
 * derives it.
 */

const INR = new Intl.NumberFormat('en-IN')

/**
 * Indian short scale, because the values are INR and a Razorpay-facing surface
 * reading "₹58,749,728,979" instead of "₹5,875 Cr" is the wrong register.
 * Returns the parts separately so a component can typeset the unit smaller.
 */
export function inrParts(value: number | null | undefined): { value: string; unit: string } {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return { value: '—', unit: '' }
  }
  const abs = Math.abs(value)
  const sign = value < 0 ? '-' : ''
  if (abs >= 1e7) return { value: sign + trim(abs / 1e7), unit: 'Cr' }
  if (abs >= 1e5) return { value: sign + trim(abs / 1e5), unit: 'L' }
  if (abs >= 1e3) return { value: sign + trim(abs / 1e3), unit: 'K' }
  return { value: sign + Math.round(abs).toString(), unit: '' }
}

function trim(n: number): string {
  if (n >= 100) return Math.round(n).toString()
  if (n >= 10) return n.toFixed(1).replace(/\.0$/, '')
  return n.toFixed(2).replace(/\.?0+$/, '')
}

export function inr(value: number | null | undefined): string {
  const { value: v, unit } = inrParts(value)
  if (v === '—') return v
  return unit ? `₹${v} ${unit}` : `₹${v}`
}

/** Exact rupees, grouped Indian-style. For tooltips and fine print. */
export function inrExact(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `₹${INR.format(Math.round(value))}`
}

export function hours(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  if (value < 48) return `${Math.round(value)}h`
  const days = value / 24
  return `${days % 1 === 0 ? days : days.toFixed(1)}d`
}

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value.toFixed(digits)}%`
}

export function multiple(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)}×`
}

/**
 * The disruption index, D(G,S).
 *
 * NOT rupees. The objective sums three deliberately different units - a
 * value-weighted delay term in INR-days, a default count, and a liquidity
 * deficit-time term in INR-hours - reconciled by the configured gammas. Every
 * `*_disruption`, `*_prevented`, `marginal_disruption` and `disruption_spread`
 * field on the wire is in these units.
 *
 * It therefore gets its own formatter with no currency sign, because printing
 * it as money is a false claim about what the model measured. The rupee story
 * lives in `commerce_preserved`, `disrupted_value` and `downstream_delayed_value`.
 */
export function idx(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const abs = Math.abs(value)
  const sign = value < 0 ? '-' : ''
  if (abs >= 1e9) return `${sign}${trim(abs / 1e9)} B`
  if (abs >= 1e6) return `${sign}${trim(abs / 1e6)} M`
  if (abs >= 1e3) return `${sign}${trim(abs / 1e3)} K`
  return sign + Math.round(abs).toString()
}

/** A plain multiplier with two decimals, for ratios that sit close to 1. */
export function ratio(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value >= 10 ? value.toFixed(1) : value.toFixed(2)}×`
}

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return INR.format(value)
}

/** `single_missed_inflow` -> `Single missed inflow`. */
export function humanise(token: string): string {
  const s = token.replace(/_/g, ' ')
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/** Short scenario labels for the selector. Long headlines live in the panel. */
export const SCENARIO_LABEL: Record<string, string> = {
  single_missed_inflow: 'Missed inflow',
  delayed_inflow: 'Delayed payment',
  partial_payment: 'Partial payment',
  liquidity_drain: 'Liquidity drain',
  supplier_failure: 'Supplier failure',
  concentrated_shock: 'Concentrated shock',
  multi_node_shock: 'Multi-node shock',
}

export function scenarioLabel(family: string): string {
  return SCENARIO_LABEL[family] ?? humanise(family)
}
