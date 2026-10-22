# Hosting readiness (step 1d) — Render

Read on 2026-10-22 from render.com/docs: `workflows`, `workflows-limits`,
`workflows-defining`, `blueprint-spec`, `redirects-rewrites`. Nothing was
deployed.

## Render Workflows: not adopted

| Question | Answer from the docs |
| --- | --- |
| SDK languages | TypeScript and Python ("currently available for TypeScript and Python"); Python package `render>=1.0.1`, SDK 1.x current. |
| Maturity | Not labelled beta on the main page, but the limits page still refers to "the Render Workflows beta" when describing discontinued plans. Treat as young. |
| Pricing | Per-second compute (`flex`: $0.20 per CPU-hour, $0.05 per GB-hour; fixed plans from $0.40/h), plus $0.25/GB-month for retained task state. **No free tier or included usage is stated.** |
| Retries | Default up to 3 retries, 1 s first wait, doubling. Configurable per task. |
| Timeouts | 30 s to 24 h per task, default 2 h. |
| Fan-out | Tasks can start other tasks; runs beyond compute limits are queued. Hobby: 50 root runs per minute. |
| Shape | A separate workflow service, triggered through the Render SDK or REST API (an API key in our web service). Not supported in preview environments or Blueprint replication. |

**Fit.** Two candidate jobs:

- *Retrying reconciliation* of outcome-unknown payouts: a handful of GET calls,
  seconds of work, only when a payout's outcome is unknown.
- *Fan-out of per-merchant analyses* after a failed payout: one bounded analysis
  (≤ 5 merchants, ≤ 12 candidates, ≤ 2 actions) measured at ~1.5–1.7 s
  in-process.

Both already run in-process after the response (FastAPI background tasks) and
from `lce paypal reconcile`, and neither is on an interactive path. Adopting
Workflows would add a second service, a Render API key in the web service, a new
dependency, a paid compute line with no free tier, and a Blueprint feature
(replication) it does not support — about a day of work for jobs that take
seconds. Under the runbook's rule (drop if more than a day, keep the in-process
fallback), **it is dropped**. Revisit only if re-entry analyses ever need to fan
out across many merchants at once.

## What was prepared instead

- `render.yaml` — API (Python, free, Oregon, health check
  `/api/v1/health/ready`), static frontend with an `/api/*` rewrite, free
  Postgres for execution records. Every secret is `sync: false`.
- `scripts/render_start.sh` — rewrites Render's `postgresql://` URL for
  psycopg 3, runs `alembic upgrade head` if a database answers, and starts the API
  either way.
- Hosted-demo safety (`LCE_DEMO_MODE=recorded`): token header, per-address rate
  limit on approve / execute / refresh, canonical plan only, `lce demo reset`,
  `GET /api/v1/execution/demo` for the page to explain the mode.

## Unverified until deployed

- Whether a static-site rewrite to another host proxies `POST` bodies (the docs
  say a destination may be "a full, publicly accessible URL" but do not describe
  proxying). Fallback is documented in the README.
- Whether `preDeployCommand` works on free instances; not relied on — migrations
  run in the start script.
- `/api/v1/health/ready` requires the database (frozen behaviour). When the free
  database expires (30 days) the health check fails although the analysis still
  serves. Options then: upgrade the database, or point the health check at
  `/api/v1/health/live`.
- Cold start time and the re-entry analysis time on 0.1 CPU. Memory measured
  locally: 169 MB resident after loading the snapshot, the speed artifact and
  serving the main routes, against the free instance's 512 MB.
