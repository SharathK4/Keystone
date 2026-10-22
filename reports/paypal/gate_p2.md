# Gate P2 — webhooks, payout status, failure re-entry

Offline gate. Every PayPal response here comes from the fake in
`tests/paypal_fakes.py`; every webhook is signed with a locally generated RSA key
and certificate, exactly as PayPal signs (`transmission_id|time|webhook_id|crc32`).
Nothing in this gate has been seen from the real sandbox yet.

## What passed

| Requirement | Where it is proven |
| --- | --- |
| Raw body verified before anything is stored; 400 and no processing on failure | `TestReceiver::test_a_tampered_body_…`, `…another_listener…`, `…missing_headers…` |
| Race-safe dedupe by event id | `TestConcurrentDelivery` (8 threads, one row, one accepted); `…replayed_event…` |
| Raw event persisted with provenance and the verification result, no secrets | `paypal_webhook_events`: verification, transmission headers, body SHA-256, payload |
| 2xx quickly; unknown types recorded and ignored | analysis runs after the response; `…unknown_type…` |
| Idempotent, out-of-order tolerant, terminal never regresses | `TestStateMachineProperties`: every sequence of 1–5 events from every start (9,330 sequences × properties) |
| Failure re-entry: failed / held / blocked / returned / unclaimed → typed shock → bounded analysis → stored record | `TestFullFailurePath` (real canonical snapshot, real bounded analysis) |
| Not in the live subset → reason recorded, nothing analysed | `TestShockMapping` |
| Reconciliation never resubmits money | `test_reconciling_a_lost_response_never_resubmits`; audit `paypal_reconcile_never_resubmits` |

Audit: 30 checks (three new). Full suite: 539 passed, 8 deselected (live).
Migration 0004: single head `0004_reentry`, zero diff against the ORM, clean
downgrade and re-upgrade.

## How a failed payout re-enters

The missing amount is the payout's **analytic** rupee amount (not the sandbox
dollars). The shock is a cash withdrawal at the mapped merchant with
`magnitude_multiple = missing / liquidity slack`, onset 0 h. The analysis is the
existing bounded on-demand path (≤ 5 merchants, ≤ 12 candidates, ≤ 2 actions);
in-process it takes about 1.5–1.7 s on the canonical snapshot, so it runs after
the webhook is acknowledged. The proposed plan is stored with provenance
`computed_now`, the snapshot id and the limits. It is never executed without a
new approval.

## Behaviour changed from P1

`POST /execution/records/{id}/refresh` used to repeat the identical create
request when a response was lost (safe because of `PayPal-Request-Id`, but still a
POST). It now only reads: it learns the batch id from a stored batch event, or
stays `unknown` with the reason "nothing is resubmitted". A retry needs a fresh
approval.

## Unverified

- What PayPal returns for a batch it has never seen when looked up by our
  idempotent key — there is no documented lookup by `sender_batch_id`; we wait
  for the batch webhook instead.
- Real webhook delivery: needs a public URL registered as a sandbox webhook.
- The certificate chain is **not** validated (we check the URL is HTTPS on
  `paypal.com`, the validity window and the RSA signature).

## Found, not fixed (frozen code)

The existing `webhook_events` table has a BIGINT primary key that SQLite does not
auto-increment, so inserting into it fails on SQLite. It is untouched (freeze);
PayPal events use their own table.
