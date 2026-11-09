# Keystone — the page

One page: six cards, in the order the story runs. Each card is a picture; the
numbers are one click away.

![The landing](../docs/screens/01-landing.png)

| Card | Shows | Opens onto |
| --- | --- | --- |
| **01 The network** | Who pays whom, learned from payments alone | Every merchant and link, the ones the rest lean on |
| **02 What breaks** | One missed payment, and who else it reaches | Each merchant hit, when, and how badly; pull any merchant yourself |
| **03 Who to fund** | The fewest dollars that keep the most payments on time | The plan, the rescues it beat, and what waiting costs |
| **04 The rule lab** | A family tree of rules, ending in the one the AI wrote | The race: people's rules against the AI's, each replayed as you watch; the winner, whom it funds, and its PayPal payout |
| **05 Pay with PayPal** | The payout a person approves | Review → sign → pay in the PayPal sandbox, then the ledger PayPal's webhooks fill |
| **06 The proof** | An engraved seal and how many checks pass | Three claims, what we can't promise, how well it learned, every check |

The lab streams its work: each rule the AI writes enters the race at the foot,
its bar grows as each scenario's replay lands, and it slides to its place.
Code: `src/lab/`.

## Run it

```bash
npm install
npm run dev        # http://localhost:5173, proxies /api to the API on :8000
npm run build      # type-check and build
npm run lint
node scripts/screens.mjs   # README screenshots, with the app running
```

`LCE_API` points the dev proxy elsewhere; `VITE_API_BASE` does the same for a
separately hosted build.

## Rules the page keeps

- **No number is typed here.** Every figure comes from an API response; types are
  generated from the backend's OpenAPI schema (`src/api/schema.ts`), so a renamed
  field fails the build instead of rendering `undefined`.
- **Every figure says where it came from**: computed now, replayed offline,
  inferred, or executed in the PayPal sandbox — derived from the endpoint, never
  written by hand.
- **Money in one currency at a time.** US dollars by default at a declared
  ₹100 = $1 (the network is denominated in rupees); the sandbox pays 1/1000 of each.
  A switch shows rupees.
- **Nothing moves on its own.** Motion only answers a click, is short, and stops
  under reduced motion.
- **If the API is down, the page says so** instead of showing a plausible number.

## Design

Paper and ink, one display face (Instrument Serif, OFL, self-hosted), one
terracotta accent. Each card's tile is drawn by an SVG noise filter in its own
colour — forest, slate, brass, ember, and a pale certificate for the proof — so
the cards read as objects, not charts. No chart library; every figure is drawn
from data in SVG.
