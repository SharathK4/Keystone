# PayPal sandbox spikes — results

**Status: read-only probes run 2026-11-04 (S1, probe); payout and invoice spikes NOT RUN.**
Written in Gate P1; S2-S3 run by the owner's word (`-m paypal_live`, after "run live tests").
Until a spike passes, everything that depends on it stays marked
`verified_in_sandbox: false` in `/execution/paypal`.

| Spike | What it settles | How | Result |
| --- | --- | --- | --- |
| S1 | Token flow works for this app | `test_s1_token` | **PASS** 2026-11-04 via `lce paypal status --probe`: token issued |
| probe | A missing payout batch answers 404 (the payouts probe's assumption) | `test_probe_readings` | **PASS** 2026-11-04: payouts 404 (reachable, permitted); invoicing **403 NOT_AUTHORIZED** for this app; webhooks API 200 |
| S2 | USD payout to a sandbox personal account is accepted and readable; item status | `test_s2_payout_create_and_read` | — |
| S2b | Same `PayPal-Request-Id` returns the same batch (idempotency, observed) | `test_s2b_same_request_id_returns_same_batch` | — |
| S3 | Invoice create / send / get; whether the due date can change after send | `test_s3_invoice_create_send_get_and_due_date` | — |
| S4 | A real signed webhook verifies against `PAYPAL_WEBHOOK_ID` | needs the Gate P2 receiver on a public HTTPS URL | — |
| S5 | How many sandbox accounts can be created (sizes the live subset) | developer dashboard → Sandbox accounts | — |
| S6 | Agent Toolkit / MCP in the sandbox | deferred: Tier 2 | — |

The live subset for the canonical plan needs **two** sandbox personal accounts,
mapped with:

```bash
lce paypal map m0005 --email <sandbox personal account 1>
lce paypal map m0009 --email <sandbox personal account 2>
```
