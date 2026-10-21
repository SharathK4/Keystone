# PayPal facts — verified 2026-10-21

Only what is marked **VERIFIED** may be relied on. Everything else stays
**UNVERIFIED** until a spike (S1–S6) proves it in the sandbox.

## Hackathon rules — VERIFIED

Source: <https://paypalaihackathon.devpost.com/rules> and
<https://paypalaihackathon.devpost.com/> (read directly; the pages block automated
fetchers).

- Submission period **Oct 1, 2026 09:15 PT → Nov 12, 2026 12:00 PT**. Judging
  Dec 1–15. Winners ~Dec 21.
- Build or update an app that integrates the PayPal developer platform (free
  sandbox) **and** an AI tool, model or platform. "Any AI tool works, as long as
  PayPal integration is central to the project."
- Existing projects must be **significantly updated after the start of the
  submission period**, and the entrant should explain how.
- Functional demo: run instructions in the repo **or** a hosted URL. Mockups do
  not count. Judges may judge on text, images and video alone; judging may use
  automated AI-driven analysis.
- Public repo **with an open-source licence visible in the About section**; must
  contain all source, assets and instructions.
- Video **under 3 minutes**, public on YouTube, shows the project running, **no
  third-party trademarks or copyrighted music/material without permission**.
- Must be solely the entrant's work and not violate anyone's IP.
- Stage 1 pass/fail on theme fit and reasonable use of the required APIs. Stage
  2: Technological Implementation, Design, Potential Impact, Innovation,
  Presentation — **equal weight**; ties broken by the first criterion
  (Technological Implementation).
- Honourable mentions include **Best Use of PayPal + AI** and **Best Use of
  Agentic Commerce**. Sponsor prizes include **Best Use of Render** (×3) and
  Postman, AG Grid, APIMatic, Bryntum, Channel3. One grand-or-honourable prize
  plus one sponsor prize per project.
- Theme sentence: *"asking what happens when you put AI behind the wheel."*

## Payouts

- Item webhook events — **VERIFIED**
  (<https://developer.paypal.com/api/rest/webhooks/event-names/>):
  `PAYMENT.PAYOUTS-ITEM.{SUCCEEDED, FAILED, HELD, BLOCKED, RETURNED, UNCLAIMED,
  REFUNDED, CANCELED}`; batch `PAYMENT.PAYOUTSBATCH.{PROCESSING, SUCCESS, DENIED}`.
- **INR cannot be used** — **VERIFIED (docs)**: PayPal Payments Private Limited
  users cannot use INR to receive payments, and India is excluded from payouts
  currency conversion (<https://developer.paypal.com/docs/nvp-soap-api/currency-codes/>,
  <https://developer.paypal.com/payouts/currency-conversion.md>). Sandbox
  execution must therefore use a supported currency (USD proposed) at a
  declared demo scale.
- Sandbox base `https://api-m.sandbox.paypal.com` — VERIFIED (agent-toolkit README).
- Idempotency via `sender_batch_id`; sandbox availability without production
  approval; item-status lifecycle timing — **UNVERIFIED** (spike S2).
- Production settlement times for payouts to a PayPal balance vs. a bank — **UNVERIFIED**.
  Do not name rail speeds in UI or video until verified; label the latency grid
  in hours only.

## Invoicing v2

- Webhook events — **VERIFIED**: `INVOICING.INVOICE.{CREATED, UPDATED, SCHEDULED,
  PAID, CANCELLED, REFUNDED}`. **There is no overdue event**, so brief §5.7's
  "invoice … overdue if available" is not available as a webhook.
- Agent Toolkit exposes `update_invoicing`, `send_invoice_reminder`,
  `record_payment_for_invoice`, `create_conditional_rules_for_invoice`,
  `cancel_sent_invoice` — VERIFIED as tool names.
- Whether the due date can change after send; partial-payment schedules;
  early-payment discounts — **UNVERIFIED** (spike S3).

## Webhooks — VERIFIED

Source: <https://developer.paypal.com/api/rest/webhooks/rest/>

- Two verification methods: **self-verification (preferred)** — RSA/SHA256
  signature (`paypal-transmission-sig`) over
  `transmission_id|transmission_time|webhook_id|crc32(raw body, decimal)` using the
  cert at `paypal-cert-url`; or **postback** to
  `POST /v1/notifications/verify-webhook-signature`.
- **Simulator mock events use the literal `WEBHOOK_ID` and do not support
  postback** — so self-verification must be implemented to test with the
  simulator at all.
- Listener must return **2xx**; PayPal retries **up to 25 times over 3 days**.
  HTTPS on 443, reachable by PayPal (local development needs a tunnel or a
  deployed URL).
- Max URLs per app (10) — **UNVERIFIED**.

## Agent Toolkit / MCP — VERIFIED

Source: <https://github.com/paypal/agent-toolkit> (Apache-2.0).

- TypeScript package `@paypal/agent-toolkit` (Node 18+); a `python` folder
  exists but is undocumented on the README. Frameworks: OpenAI Agents SDK,
  LangChain, Vercel AI SDK, MCP.
- Tool groups: Invoices (20 tools), Payments/Orders, Disputes, Shipment
  Tracking, Catalog, Subscriptions, Reporting (`list_transactions`,
  `get_merchant_insights`).
- **No Payouts tools.** `liquidity_injection → Payout` must use the REST API
  directly, not the toolkit.
- Remote MCP endpoint and its sandbox URL — **UNVERIFIED** (spike S6).
