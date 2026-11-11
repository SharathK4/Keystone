# Keystone
![alt text](image.png)
![alt text](image-1.png)
![alt text](image-2.png)
![alt text](image-3.png)
![alt text](image-4.png)
![alt text](image-6.png)

**Live app: https://keystone-web-85pe.onrender.com** (free tier: the first visit can take a minute to wake)

**Demo Link: https://www.loom.com/share/24f2ece2fffc42dfac401686578f81c4**

**When one merchant misses a payment, others start missing theirs. Keystone finds
the few merchants to fund that stop it, lets an AI write a better rule for who to
fund first, and pays the rescue through PayPal.**

Merchants on a payment platform do not fail independently. A supplier who is paid
late pays their own suppliers late, and the delay travels along the same edges the
money does. The platform is the only party that sees those edges — so it is the
only party that can tell who is short of cash, and who is short because someone
upstream was.

Keystone answers five questions, in order:

1. **Who pays whom?** Learn the network from payment history alone.
2. **What breaks when one payment fails?** Replay the week without it and see who
   runs short, by how much, and when.
3. **Where does one dollar do the most work?** Search for the cheapest rescue and
   score it by replaying the week with it.
4. **Can a machine write a better rule?** Let a language model invent the rule for
   who gets the money first (below).
5. **Pay it.** A person approves the plan; PayPal pays the merchants.

---

## The rule lab: the AI writes the rule

People write rules for who to fund first — *biggest gap first*, *most important
first*. None wins everywhere. So a language model writes its own: one-line
formulas over 14 things Keystone knows about each merchant. Every formula is
replayed on every test scenario, the best go back to the model, and it writes
better ones (an evolutionary search in the style of DeepMind's FunSearch).

| Same money, share of the week's damage prevented | Learned on | Never seen |
| --- | --- | --- |
| Best of six rules people wrote | 17.9% | 18.4% |
| Keystone's own search | 18.6% | 19.5% |
| **The AI's rule:** `pays / (paid_by + 1) · need` | **29.9%** | **30.5%** |

In words: *fund the merchants that pay many others but are paid by few.* It beat
the search in all 16 scenarios it had never seen, and decides in under a
millisecond. The model only ever returns text; every formula is parsed into a
whitelisted expression before anything runs. Each run is live, so the AI's rules
vary from run to run.

## PayPal

- **Payouts** (sandbox) pay each approved rescue, with one `PayPal-Request-Id` per
  plan and action, so a plan can never pay twice.
- **Webhooks** are verified (RSA-SHA256) and move each payout through a state
  machine; a payout that fails or goes unclaimed becomes a new shortfall that
  Keystone plans for again.
- Nothing is paid without a person approving the exact plan. Live mode is refused
  in code.

## Research basis

**Programmable Repayment: Risk-Sharing and Enforcement Through the Payment
Platform** — Kumar Rishabh and Alessandro Di Stefano, 7 May 2026.
[SSRN 6729024](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6729024)

The paper argues a payment platform can enforce and share risk in ways an outside
lender cannot. Keystone asks the question it implies: if the platform can place
money anywhere in the network, *where* should it go?

## What is in here

| | |
| --- | --- |
| `src/lce/discovery` | The rule lab: safe formula parser, scenarios, replay scoring, the model prompt |
| `src/lce/paypal`, `src/lce/execution` | PayPal OAuth, Payouts, webhooks, approvals, pay-at-most-once |
| `src/lce/simulation`, `src/lce/intervention` | The week's replay, rescue search, counterfactual scoring |
| `src/lce/inference`, `src/lce/learning` | Learning the network from payment history |
| `src/lce/api` | FastAPI service |
| `frontend` | The page: six cards, each opening onto its analysis |

## Running it

```bash
pip install -e ".[dev]"
cp .env.example .env        # optional: PayPal sandbox keys, OPENROUTER_API_KEY or ANTHROPIC_API_KEY
sh scripts/live_sandbox.sh  # API on http://127.0.0.1:8000
```

```bash
cd frontend && npm install && npm run dev   # http://localhost:5173
```

It runs with no keys: random mutation writes the lab's rules instead of a model,
and plans are approved but not paid. Deploying: [`docs/DEPLOY.md`](docs/DEPLOY.md).

## Stated plainly

- **Scope:** a 100-merchant network with 216 learned links; not yet run on a live
  merchant book.
- **Sandbox only.** PayPal runs in sandbox mode; amounts are shown at a declared
  ₹100 = $1 and the sandbox pays 1/1000 of each.
- **Not credit decisions.** Amounts and costs are model outputs under stated
  assumptions.

## New since October 1

PayPal Payouts and webhooks with approvals and failure re-entry; the rule lab;
a redesigned six-card page; 650+ tests; a Render blueprint.

Built with Python, FastAPI, React, the PayPal REST APIs, and a language model via
OpenRouter or the Anthropic API. [Apache-2.0](LICENSE).
