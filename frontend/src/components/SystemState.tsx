/**
 * Loading and failure states.
 *
 * When the analytical service is unavailable this page shows nothing else. No
 * skeleton panels holding plausible shapes, no cached last-known figures, no
 * demo mode. Every number here is a model output, so a page that renders
 * without the model is not a degraded version of this product - it is a
 * different and dishonest one.
 */

import './system-state.css'

export function Loading() {
  return (
    <div className="sys" role="status" aria-live="polite">
      <span className="sys__pulse" aria-hidden="true" />
      <p className="sys__line">Loading analytical snapshot</p>
    </div>
  )
}

export function Failure({ message, path }: { message: string; path: string }) {
  return (
    <div className="sys sys--fail" role="alert">
      <p className="sys__title">Analytical service unavailable.</p>
      <p className="sys__line">{message}</p>
      {path ? <p className="sys__path num">{path}</p> : null}
      <p className="sys__hint">
        Start the backend, then reload:
        <code>python -m lce.cli serve --host 127.0.0.1 --port 8000</code>
      </p>
    </div>
  )
}
