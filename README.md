# Keystone
![alt text](image.png)
![alt text](image-1.png)
![alt text](image-2.png)
![alt text](image-3.png)
![alt text](image-4.png)
![alt text](image-6.png)
**A liquidity shock at one merchant, followed all the way to the capital that stops it.**

Merchants on a payment platform do not fail independently. A supplier who is
paid late pays their own suppliers late, and the delay travels along the same
edges the money does. The platform is the only party that can see those edges —
it processes both sides of the transaction — and is therefore the only party
that can tell the difference between a merchant who is genuinely short of cash
and a merchant who is short because someone upstream was.

Keystone is a decision engine for that difference. It answers four questions in
order:

1. **Where does payment dependence actually live?** Estimate the network from
   transaction history alone — who pays whom, how much passes through, with
   what lag and what reliability.
2. **What breaks when one payment fails?** Simulate the shock through that
   network and measure who becomes liquidity-constrained, by how much, and when.
3. **Where does one rupee do the most work?** Search over candidate
   interventions, replay each plan against the same shock, and report the
   difference between the two runs.
4. **What should be offered, to whom?** Turn the selected action into a
   structured liquidity offer sized to that merchant's own payment cycle.

The distinguishing claim is in step 3. Most systems predict which merchants are
at risk. Keystone measures what an intervention *achieved* by replaying it —
the recommendation is scored by the simulator, not by the model that proposed it.

---

## Research basis

This work builds on:

**Programmable Repayment: Risk-Sharing and Enforcement Through the Payment
Platform** — Kumar Rishabh and Alessandro Di Stefano, 7 May 2026.
[SSRN 6729024](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6729024)

The paper's argument is that a payment platform can enforce and share risk in
ways a lender outside the flow cannot, because repayment can be made a property
of the payment rail itself. Keystone takes that premise and asks the operational
question it implies: if the platform can place liquidity anywhere in the network,
*where* should it go? That is a network problem, not a credit-scoring problem,
and it is the problem this repository solves.

---

## What is in here

| | |
| --- | --- |
| `src/lce/domain` | Payment graph, dependency estimation, cascade propagation, the disruption objective |
| `src/lce/simulation` | Discrete-event liquidity simulator over a 168-hour horizon |
| `src/lce/optimization` | Intervention search, systemic-importance ranking, counterfactual replay |
| `src/lce/data` | Synthetic network generator with known ground truth |
| `src/lce/api` | FastAPI service |
| `src/lce/snapshot` | Precomputed analytical snapshot the API serves |
| `frontend` | The Keystone interface — four panels, each opening onto its analysis |

The default benchmark is a 100-merchant network with 216 estimated
relationships and seven analysed shock scenarios.

## Running it

```bash
pip install -e ".[dev]"
python -m lce.cli serve --host 127.0.0.1 --port 8000
```

```bash
cd frontend && npm install && npm run dev
```

The frontend proxies `/api` to the backend and has no data of its own. If the
service is down the page says so rather than rendering a plausible number.
See [`frontend/README.md`](frontend/README.md) for the interface, and
[`BACKEND_FREEZE_REPORT.md`](BACKEND_FREEZE_REPORT.md) for the architecture,
benchmarks and verified provider capabilities.

## Deploy (Render)

[`render.yaml`](render.yaml) is a Render Blueprint for three pieces: the API as a
Python web service, the frontend as a static site, and a free Postgres database
that holds **execution records only**. The analysis is served from committed
artifacts and works with no database at all. Nothing deploys automatically.

1. Create a Blueprint from the repository. Render asks once for each
   `sync: false` value: the sandbox app's `PAYPAL_CLIENT_ID` and
   `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID` (fill in after step 3) and
   `LCE_DEMO_TOKEN` (a long random string you keep private). Leave
   `LCE_CORS_ORIGINS` and `VITE_API_BASE` empty unless you use the fallback below.
2. After the first deploy, check the API's address. If it is not
   `keystone-api.onrender.com`, edit the `/api/*` rewrite in `render.yaml`.
3. In the PayPal developer dashboard, add a **sandbox** webhook pointing at
   `https://<api>/api/v1/webhooks/paypal` for `PAYMENT.PAYOUTS-ITEM.*` and
   `PAYMENT.PAYOUTSBATCH.*`, and put its id in `PAYPAL_WEBHOOK_ID`.
4. Map the two sandbox accounts against the hosted database (`DATABASE_URL` set
   to Render's external connection string):
   `lce paypal map m0005 --email <sandbox account>` and the same for `m0009`.
5. Record the canonical run once, sending the header `X-Keystone-Demo-Token`.
   `lce demo reset --yes` clears it (identities are kept) if you need to redo it.

**What visitors can and cannot do.** The hosted service runs with
`LCE_DEMO_MODE=recorded`: everyone sees the analysis and the ledger of the
sandbox run recorded in step 5, and nobody starts a new payout without the demo
token. A token holder is limited to the canonical plan and to six execution
requests per address per ten minutes. Reads are never limited. PayPal is sandbox
only; live mode is refused in code, with no override.

**Free-tier limits (US region, Oregon).** A free web service spins down after a
period without traffic, and the next request waits while it starts again (about
a minute). A free Postgres database expires 30 days after it is created; after
that `/api/v1/health/ready` (which needs the database) fails while the analysis
routes still answer, so upgrade or recreate it first. The plan is to move the API
to an always-on paid instance for 1–15 December. A free instance has 0.1 CPU:
the re-entry analysis that takes about 1.6 s on a laptop will take longer there
(not measured).

**Known limitations.** The PayPal webhook signature is verified locally, but the
certificate **chain is not validated**: the certificate must come from an HTTPS
`paypal.com` URL and be within its validity window, and the RSA-SHA256 signature
must match. The demo rate limit is in memory, per process. Whether Render's
static-site rewrite proxies `POST /api/*` to the API is unverified; if it does
not, set `VITE_API_BASE` on the static site to the API's address and
`LCE_CORS_ORIGINS` on the API to the site's address, then redeploy both.
Render Workflows is not used ([why](reports/hosting/render.md)).

## Three things worth stating plainly

**The benchmark is synthetic, and that is the point.** Every metric is measured
on networks whose generating process is known, so the model can be *scored*
rather than demonstrated. No claim is made here about real-world predictive
accuracy.

**Nothing moves money.** Razorpay integration runs in Test Mode and is used for
connectivity and capability probing. Route and Direct Transfers are not enabled
on the test account — probed, not assumed — so every recommendation is recorded
as a plan.

**The offers are model output, not credit decisions.** Amounts, terms and
indicative costs are computed under stated assumptions. They are not quotes and
not underwriting.
