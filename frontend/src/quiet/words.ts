/**
 * Numbers said as words, for the walk's sentences. Every input is an API
 * field; these only choose how to say it.
 */

const SMALL = ['No one', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten']

/** "One more feels it." / "Two more feel it." / "No one else feels it." */
export function moreFeelIt(n: number): string {
  if (n <= 0) return 'No one else feels it.'
  const who = n < SMALL.length ? SMALL[n] : String(n)
  return `${who} more ${n === 1 ? 'feels' : 'feel'} it.`
}

/** 38.8 -> "39 hours"; 101.4 -> "four days". */
export function spokenHours(hours: number): string {
  if (hours < 48) {
    const h = Math.round(hours)
    return `${h} hour${h === 1 ? '' : 's'}`
  }
  const d = Math.round(hours / 24)
  const word = d < SMALL.length ? SMALL[d].toLowerCase() : String(d)
  return `${word} day${d === 1 ? '' : 's'}`
}

/** cascade depth 1 -> "one step out". */
export function stepsOut(depth: number): string {
  if (depth <= 0) return 'at the source'
  const word = depth < SMALL.length ? SMALL[depth].toLowerCase() : String(depth)
  return `${word} step${depth === 1 ? '' : 's'} out`
}

/** 0 -> "0h", 24 -> "1d": the delay selector's labels. */
export function delayLabel(hours: number): string {
  if (hours === 0) return 'now'
  if (hours < 24 || hours % 24 !== 0) return `${hours}h`
  return `${hours / 24}d`
}

/** "14:02:11" from an ISO timestamp, in the reader's clock. */
export function clock(iso: string | null | undefined): string {
  if (!iso) return ''
  const t = new Date(iso)
  return Number.isNaN(t.getTime()) ? '' : t.toLocaleTimeString([], { hour12: false })
}

/** 4123.4 ms -> "4.1 s". */
export function seconds(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return ''
  return `${(ms / 1000).toFixed(1)} s`
}
