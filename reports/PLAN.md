# Keystone × PayPal — review of the two briefs, and the plan to win

Written at Gate 0 (2026-10-21), after the baseline and both Step 0 reports.
**22 days to the deadline** (Nov 12, 12:00 PT). Evidence:
[`structure/step0.md`](structure/step0.md), [`paypal/step0.md`](paypal/step0.md),
[`paypal/facts.md`](paypal/facts.md), probes in [`structure/probes/`](structure/probes/).

---

## 1. The finding that should reshape the plan

Replaying the canonical plan with the money landing later — same network, same
shock, same plan, Common Random Numbers, nothing else changed:

| Money lands after | Commerce kept on schedule | Merchants protected | Disruption cut |
| --- | --- | --- | --- |
| **1 hour** | **₹12.18 Cr** | 1 | 40.3 % |
| 6 hours | ₹10.48 Cr | 0 | 39.7 % |
| 12 hours | ₹4.54 Cr | 0 | 37.8 % |
| 24 hours | ₹2.65 Cr | 0 | 32.9 % |
| **48 hours** | **₹0** | 0 | 20.9 % |

*Measured by replay, not predicted.* At δ = 0 it reproduces the frozen snapshot
exactly.

This is the result that makes PayPal **causally** central rather than an
execution endpoint bolted on at the end: **the speed of the rail is a variable
in the answer.** Same capital, same merchant — one rail keeps ₹12 Cr of payments
on time, a two-day rail keeps none. No other submission will have that chart,
and every number in it is measured.

It also rescues Horizon. Horizon as written cannot work on the frozen artifact
(below); Horizon as **value of speed** works today, needs no frozen code, and is
the strongest thing in the repository.

## 2. What the briefs get right, and where they break

**Right, keep as written:** the freeze discipline, additive-only rule,
pre-registration, null over plausible numbers, human approval before any money
moves, idempotent execution with no auto-retry, webhook → shock → re-plan
loop, the three run modes so a judge can run it without credentials, units
discipline, provider neutrality in the UI.

**Breaks on contact with the code — each is a STOP the brief asked for:**

| Brief item | What the code says | Proposed resolution |
| --- | --- | --- |
| **Horizon §5** — reuse hazard `T_j` | The frozen hazard model gives **P ≈ 0 to every non-origin merchant** in all 7 scenarios (threshold 1.0, 0.4 % positive rate). Every downstream merchant would be "reachable" at every latency — a flat, empty figure. | **Redefine Horizon as value of speed** (§1). Validation becomes replay-only: monotonicity reported (one violation already seen: multi-node 2.44 % → 3.12 % at 6 h), clustered bootstrap over the 14-scenario × 4-seed Phase-4 set. |
| **Edge §4 / E2** — `N` reproduces the propagation to 1e-6 | Two propagators. The snapshot uses a **threshold rupee-flow** model that has no linearisation. The Hawkes predictor linearises **exactly** for one origin (≤ 4.6e-8) but **misses by 0.088** with three origins (noisy-OR ≠ sum). ρ̂ = **0.283**. | Choose: define `N` on the Hawkes predictor and **change E2 to "exact for expected counts"** (your call — that is a gate change, I won't make it silently), or ship Edge as a labelled research note, or cut. |
| **Edge E4** — sweep κ on buffers | The linearisable `N` has **no buffer term**: scaling buffers cannot move ρ. | κ must go **through the generator** (`scale_config(overrides={"coverage_low", "coverage_high", "credit_line_ratio"})` — existing, additive). Costs a regenerate + EM fit per grid point. |
| **PayPal §5.7** — invoice overdue as a shock | **No invoice-overdue webhook exists.** | Drop it. Payout `FAILED / HELD / RETURNED / UNCLAIMED / BLOCKED` are real and enough. |
| **PayPal §6** — Agent Toolkit for proposals | The toolkit has **no Payouts tools**; it covers invoicing, orders, disputes, subscriptions, reporting. | Payouts via REST directly; toolkit only for the invoice-based actions. |
| **PayPal §5.6** — verify webhooks | Simulator mock events **cannot be postback-verified**. | Implement CRC32 + cert **self-verification**; it is the only way the simulator works at all. |
| **PayPal §5.4** — INR plan amounts | **PayPal cannot pay out INR.** | Analysis stays in ₹ (frozen anchor); execution in USD at one declared demo scale; never one number in both. |
| **PayPal §0.6** — "human runs spikes" | This session runs **locally**, not in a cloud sandbox. | If you add `PAYPAL_*` to `.env`, I can run the spikes myself through settings without ever reading or printing them. |
| **Two branches** (`structure`, `paypal`) | Both edit `audit_backend.py`, the serving-import test, router registration, `client.ts`, `pyproject.toml`, README. | **One branch, `paypal`** (created), structure work as commits on it. |

## 3. Two blockers that exist today, before any new work

1. **The fonts.** All three are *personal-use-only* demo fonts, and the one in
   use (`Against`) sets the Keystone wordmark and every heading. They are
   **tracked and pushed to a public repository**
   (`github.com/SharathK4/Keystone`, public since Sep 6) that has **no licence**.
   An open-source submission cannot redistribute them. Replace with an
   OFL-licensed display face and remove the files. Removing from HEAD leaves
   them in public history; scrubbing history means a force-push to a public
   repo — your decision, not mine.
2. **No licence.** Rules require an open-source licence visible in the About
   section; `pyproject.toml` says "Proprietary". Recommend **Apache-2.0** (same as
   PayPal's own agent toolkit, explicit patent grant).

## 4. Scope for 22 days

| Tier | Work | Why |
| --- | --- | --- |
| **1 — must** | PayPal sandbox provider: Payouts (USD, demo scale), capability probing with reasons, approval record, idempotent execute, audit trail · webhooks with self-verification, dedupe, persist · failed payout → typed shock → bounded re-analysis · **Horizon = value of speed** · licence, font swap, Razorpay de-branding · recorded-fixtures mode · README, 3-min video, Devpost text · **Render deploy** | Stage 1 is pass/fail on PayPal. Render gives the public HTTPS URL webhooks need **and** is a sponsor prize. |
| **2 — should** | Invoicing for `receivable_acceleration` / `supplier_term_extension` (if spike S3 passes) via the Agent Toolkit · Edge, honest version: ρ̂ with bootstrap interval, E1 analytic tests, E7 cross-check | Agent Toolkit use is the natural "agentic commerce" story, and stays bounded: amounts come from the optimiser, a human approves. |
| **3 — defer** | **Fog** (Gandy–Veraart cycle sampler, ensemble propagation, coverage studies) · free-text language layer | Fog is the largest build with the highest gate-failure risk: each sampled book needs a full scenario analysis (~20 s), times hundreds of samples, times visibility levels, times two mask mechanisms. It is excellent research and a poor 22-day bet. |

## 5. The pitch the work should serve

> **Who it's for:** a payments platform's merchant-financing team.
> **Problem:** merchants don't fail alone — a late payment travels along the same
> edges the money does, and a credit score sees one merchant at a time.
> **Why PayPal:** PayPal sees both sides of every payment, so it can see the edge
> — and it can move money fast enough to get there first.
> **What it does:** finds the merchant whose shortfall would cascade, sizes the
> facility, a human approves, PayPal pays it out, PayPal's webhooks report back,
> and a failed payout re-enters as a new shock.
> **The proof:** the same plan, landing an hour after the shock, keeps ₹12 Cr of
> payments on schedule. Landing two days later, it keeps nothing. Measured, not
> predicted.

That covers all five criteria: a real PayPal loop (implementation), the existing
four-panel product (design), a specific audience and problem (impact), the
value-of-speed result (innovation), and a 3-minute story with a handled failure
(presentation). "AI behind the wheel" is literal — the model chooses where the
money goes — with a human's hand on the brake.

## 6. Decisions I need from you

1. **Horizon → value of speed.** Approve the redefinition?
2. **Edge.** Change E2 to "exact for expected counts", keep it as a research note, or cut?
3. **Fog.** Defer (recommended) or keep in scope?
4. **Licence.** Apache-2.0 (recommended) or MIT?
5. **Font.** Pick a replacement (I'll propose three OFL faces with a rendered
   comparison), and decide whether to scrub the old files from public history.
6. **One branch** (`paypal`) instead of two — OK?
7. **Credentials.** Add `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` to `.env` (I
   never read it) so I can run the spikes locally, or would you rather run them?
8. **Render.** Deploy a hosted demo there (recommended)?
