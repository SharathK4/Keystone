# Deploy on Render

[`render.yaml`](../render.yaml) is a Render Blueprint for three pieces: the API as a
Python web service, the page as a static site, and a free Postgres database that
holds **payout records only**. The analysis is served from committed artifacts
and works with no database at all. Nothing deploys automatically.

1. **Create a Blueprint** from the repository. Render asks once for each
   `sync: false` value: the sandbox app's `PAYPAL_CLIENT_ID` and
   `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID` (fill in after step 3),
   `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY` (optional; the model that writes
   the lab's rules, otherwise random mutation writes them) and
   `LCE_DEMO_TOKEN` (a long random string you keep private). Leave
   `LCE_CORS_ORIGINS` and `VITE_API_BASE` empty unless you use the fallback below.
2. **Check the API's address** after the first deploy. If it is not
   `keystone-api.onrender.com`, edit the `/api/*` rewrite in `render.yaml`.
3. **Register the webhook.** In the PayPal developer dashboard, add a **sandbox**
   webhook pointing at `https://<api>/api/v1/webhooks/paypal` for
   `PAYMENT.PAYOUTS-ITEM.*` and `PAYMENT.PAYOUTSBATCH.*`, and put its id in
   `PAYPAL_WEBHOOK_ID`.
4. **Map the sandbox accounts** against the hosted database (`DATABASE_URL` set to
   Render's external connection string), for each merchant the plan pays:
   `lce paypal map m0005 --email <sandbox account>`, and the same for `m0009`.
5. **Check everything:** `lce paypal doctor --probe --public-url https://<api>`.

## What visitors can and cannot do

The hosted service runs with `LCE_DEMO_MODE=recorded`. Everyone can run the rule
lab (rate-limited per address) and read every analysis. Nobody starts a PayPal
payout without the demo token; a token holder can pay the reference plan or a plan
a lab rule just decided, a few times per address per ten minutes. PayPal is
sandbox only; live mode is refused in code, with no override.

## Free-tier limits (US region, Oregon)

- A free web service spins down when idle; the next request waits about a minute
  while it starts.
- A free Postgres database expires 30 days after it is created. After that
  `/api/v1/health/ready` (which needs the database) fails while the analysis
  routes still answer, so upgrade or recreate it first.
- A free instance has 0.1 CPU, so an analysis that takes about 4 s on a laptop
  takes longer there. An always-on paid instance is planned for the judging
  window (1–15 December).

## Known limitations

- The PayPal webhook signature is verified locally, but the certificate **chain is
  not validated**: the certificate must come from an HTTPS `paypal.com` URL and be
  within its validity window, and the RSA-SHA256 signature must match.
- The demo rate limit is in memory, per process.
- Labs and the plans their rules decide are kept in the API process's memory;
  after a restart, start a new lab before paying. The first lab after a cold
  start waits while Keystone's search solves the test scenarios (about a minute).
- In the sandbox, `lce paypal map-rest --from m0005 m0009` shares those payees
  with every other merchant, so any merchant a rule funds can be paid.
- If Render's static-site rewrite does not proxy `POST /api/*` to the API, set
  `VITE_API_BASE` on the static site to the API's address and `LCE_CORS_ORIGINS`
  on the API to the site's address, then redeploy both.
