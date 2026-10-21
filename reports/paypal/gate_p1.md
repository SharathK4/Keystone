# Gate P1 — PayPal provider, settings, mapping, approval, audit trail

**Status: PASSED offline. Nothing verified against the live sandbox** (live tests
written, not run, per instruction). Branch `paypal`.

## Results

| Check | Before P1 | After P1 |
| --- | --- | --- |
| `pytest` (offline) | 438 passed / 3 deselected | **517 passed / 8 deselected** (+79 new; deselected = 3 Razorpay + 5 PayPal live) |
| `ruff check src tests` | clean | **clean** |
| `mypy src` | clean, 129 files | **clean, 139 files** |
| `audit_backend.py` | 22 checks | **27 checks** (+5 PayPal) |
| `verify_phase4.py` | 28 checks | **28 checks** |
| Snapshot anchor `668c4a7b6d6fb71c` | ✓ | **unchanged** ✓ |
| Model anchor `d99a0b97feb42050` | ✓ | **unchanged** ✓ |
| Migrations | 2, head `0002_provenance` | 3, single head **`0003_execution`**; zero diff vs ORM; downgrade clean |

## Tests by brief item

| Item | Tests | What is proven |
| --- | --- | --- |
| **P1 contract** | `test_paypal_contract.py` (15) | Every Payouts body validates against PayPal's **published** OpenAPI schema (vendored, Apache-2.0, pinned commit); the validator is shown to reject broken bodies; money is `Decimal` strings with a currency; executed = analytic × scale by half-even rounding; idempotency fields fit PayPal's limits and are deterministic; item sums equal the plan total; the plan hash changes with every field an approver sees. |
| **P2 money** | `test_paypal_execution.py` (30) | No send without approval; an approval binds one exact hash (recipient change, other scenario, stale hash all refused); unmapped paid action blocks approval; incomplete plan cannot be approved; **one payout per action** across repeats, second approvals, a lost response and an explicit reconcile — counted as payouts the fake PayPal *created*; reconciliation maps every PayPal item status; rejections keep PayPal's diagnosis and `debug_id`; live mode and production hosts refused at construction; the generic provider interface cannot move money; credentials absent from records, events, logs and errors. |
| **P3 verification** | `test_paypal_webhooks.py` (24) | Real RSA key + X.509 cert: genuine delivery passes; tampered or re-serialised body, wrong listener id, each missing header, wrong algorithm, non-PayPal or plain-HTTP cert URL, unloadable or expired cert, foreign key, non-base64 signature all refused with a reason; simulator `WEBHOOK_ID` convention. |
| API + invariants | `test_paypal_api.py` (10) | `/execution/status` unchanged by default; switch reports PayPal in the same contract; `/execution/paypal` answers with no credentials and says why; full approve → execute → reconcile → ledger over HTTP; 409 without approval; 503 with reason when no database while analysis keeps working; no simulator vocabulary in new models. |

## What exists now

- `PayPalSettings` (standalone; secrets as `SecretStr`; **live mode and any
  non-sandbox host refused**, with no override), `LCE_EXECUTION_PROVIDER` switch
  defaulting to today's behaviour.
- `lce.paypal`: `Money`/`DemoScale` (default **USD 1 per ₹1 lakh**, half-even),
  REST client (token, payouts create/get, verify-signature postback, read-only
  probes; **writes never retried**), webhook self-verification.
- `lce.execution`: plan read from the snapshot and **checked complete** (actions
  must sum to the replayed cost), `PayPalSandboxProvider` with reasoned
  capabilities, `ExecutionService` (approve / execute / refresh / ledger).
- Tables `paypal_identities`, `execution_approvals`, `execution_records`
  (unique idempotency key), `execution_events` (append-only); migration `0003`.
- Routes under `/api/v1/execution/`: `paypal`, `plans/{id}`,
  `plans/{id}/approvals`, `plans/{id}/executions`, `records/{id}/refresh`,
  `ledger`. CLI: `lce paypal map | identities | status [--probe]`.

## Not verified (needs the owner to run `-m paypal_live`)

- Token flow, payout create/read, request-id idempotency, invoicing behaviour —
  spikes S1–S3 in `tests/test_paypal_live.py`; results table in `spikes.md`.
- The payouts probe's reading that a missing batch answers **404** (vs 403 when
  not permitted). Reported as `verified_in_sandbox: false` until S1/probe runs.
- Webhook certificate **chain** to PayPal's CA is not validated; an HTTPS
  `paypal.com` host allowlist stands in for it. Stated in the module.

## Deviations from the brief, stated

- Spikes are opt-in tests rather than standalone scripts, so they use the
  product's own client and settings; `scripts/paypal_spikes/README.md` points there.
- P3 here covers verification. HTTP-level replay / out-of-order / unknown-type
  handling lands with the receiver in Gate P2.
- Approval is refused (not only execution) when a paid action has no sandbox
  identity — approving a plan that cannot run would mislead.
- One PayPal batch per action, so each action has its own idempotency key and
  status. `ONHOLD → held`, `UNCLAIMED → failed` (it re-enters as a shock in P2).

## New finding that needs a decision

**The canonical artifacts are not in version control** (`artifacts/**` is
ignored), and a rebuild cannot reproduce them: the snapshot's content hash covers
`created_at` and freshly generated ids. So the anchors exist only on this machine,
and a judge cloning the repo — or a Render deploy — has neither the snapshot nor
the model. Proposed fix: un-ignore exactly
`artifacts/snapshots/snap-a4c99d5a49a64ffd/` and `artifacts/contagion/` (~1 MB)
and commit them. Tests here deliberately use a real extract
(`tests/fixtures/paypal/canonical_extract.json`) so they do not depend on it.
