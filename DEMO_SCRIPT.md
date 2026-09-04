# Keystone — demo script

**Target: 9 minutes.** Timings are cumulative. Spoken lines are written to be read aloud —
short sentences, no jargon until it is earned.

Before you record: backend on `127.0.0.1:8000`, frontend at **http://localhost:5173**,
browser at 100% zoom, Excalidraw open in a second tab with `keystone-diagrams.excalidraw`.

---

## 1 · The paper — 40 seconds

> This started with a paper published this May. *Programmable Repayment*, by Kumar Rishabh
> and Alessandro Di Stefano.
>
> Its argument is simple. A bank lends money and then hopes to be repaid. A payment
> platform is different. The platform sits **inside** the flow of money. It can make
> repayment a property of the rail itself.
>
> That is a powerful idea. But it leaves one question open.
>
> If a platform can place liquidity anywhere in its network — **where should it go?**
>
> That question is what we built.

*On screen: the SSRN page, or the Research basis section of the README.*

---

## 2 · The problem — 55 seconds (1:35)

> Here is the thing everybody misses about merchant lending.
>
> Merchants do not fail on their own.
>
> A supplier gets paid late. So they pay *their* supplier late. That one goes late too.
> The delay travels — and it travels along exactly the same edges the money does.
>
> So when a merchant is short of cash, there are two completely different reasons.
> Either they run a bad business. Or somebody upstream of them was late.
>
> A credit score cannot tell those apart. It only sees one merchant at a time.
>
> The platform can. Razorpay processes both sides of the transaction. It is the only
> party in the system that can see the edge.
>
> So we stopped asking "who is risky?" and started asking a network question:
> **when one payment fails, who does it reach — and where do you put one rupee to stop it?**

---

## 3 · The diagrams — 2 minutes 15 (3:50)

### Diagram 1 — How Keystone works *(35s)*

> This is the whole idea in one picture.
>
> Each circle is a merchant. The black one fails to receive a payment. Follow the arrows —
> the delay spreads to the merchants downstream of it.
>
> Now look at the red node. That is not where the problem started. That is where the model
> says to put the money. Place capital there, and the merchants past it keep paying on time.
>
> Underneath is the pipeline. Estimate the network. Simulate the shock. Search for an action
> and replay it. Write the offer.
>
> Four steps. The rest of this demo is those four steps, in order.

### Diagram 2 — Where the data comes from *(35s)*

> Two lanes, kept strictly separate.
>
> On the left, the benchmark. We generate a synthetic payment network where we *know* the
> true dependencies. Then we throw the truth away, hand the estimator only the payment
> events, and see if it can recover the network. That dashed red arrow is the whole point —
> the estimate gets **scored** against the truth that produced it. That is what makes this a
> benchmark and not a demo.
>
> On the right, the real Razorpay integration. Test Mode. We probed what the account can
> actually do. Payments, orders, settlements — available. Transfers and Route — not enabled.
>
> So every action Keystone recommends is recorded as a plan. Probed, not assumed. No funds
> move. We will come back to that.

### Diagram 3 — The mathematics *(40s)*

> Everything the model ranks on reduces to one function. D.
>
> Three terms. How much money was late, and how late. Who stopped paying entirely. And how
> far below their floor a merchant sat, and for how long.
>
> Now look at the units. Rupee-days. A count. Rupee-hours. **Three different units in one
> sum.** The weights reconcile them.
>
> Which means D is an **index**. It is not rupees.
>
> That one line saved this project from shipping a lie, and I will show you exactly how in
> about three minutes.

### Diagram 4 — Propose, replay, measure *(25s)*

> And this is the part that is not a risk score.
>
> The model proposes an action. Then we replay that action through the same simulator,
> against the same shock, and measure the difference between the two runs.
>
> The model does not get to grade its own homework. The simulator does.
>
> A risk score ranks merchants and stops. We answer the next question.

---

## 4 · The product — 4 minutes 40 (8:30)

### Open the page *(20s)*

*Full four-panel view. Do not click yet.*

> This is Keystone. Four panels, one story. The network. What breaks. Where capital works.
> The offer.
>
> Every number behind these is a live model output. If the backend goes down, this page
> shows an error — it does not fall back to a nice-looking number.

---

### Panel 01 — The network *(45s)*

**Click "The network."**

> A hundred merchants on a ring. Every line is an estimated dependency — recovered from
> transaction history alone. Nobody told the model who pays whom.

**Hover over the top-ranked merchant in the list on the right (m0006), then click it.**

> Selecting one merchant lights its relationships and fades everything else.
>
> The ranking on the right is not degree, and it is not size. It is *measured*: we shock each
> merchant and record what actually breaks downstream.

**Point at the correlation line at the bottom.**

> And we report how much that ranking agrees with the simpler ones. Cash deficit, 0.85.
> Degree, 0.68. Correlated — but not the same. Ranking by size picks different merchants.

**Close (Esc).**

---

### Panel 02 — What breaks *(60s)*

**Click "What breaks."**

> One shock. Merchant m0070 never receives ₹53 lakh that was due at hour nineteen.
>
> One merchant reached. ₹17.9 lakh of payments delayed.

**Point at the dot grid.**

> A hundred dots, one merchant each. And the position means something — they are laid out by
> how thinly each one covers its own book. Thinnest on the left.
>
> The dark ones at the top owe more than they can cover. The field fades as merchants get
> stronger. The last thirty-four have nothing due this week at all.
>
> Now find the red dot.

**Point at it — it sits mid-field.**

> It is not on the left. The merchant this shock actually reached is a perfectly healthy one,
> sitting right in the middle. It got hit because of *where it stands* — not because of what
> it owes.
>
> That is the finding. And a credit score would never see it.

**Click "Multi-node shock" in the scenario row.**

> Change the scenario and the whole thing recomputes on the backend. Three merchants reached
> now. ₹96.9 lakh. The timing chart fills in. The dots move.

**Click back to "Missed inflow." Close.**

---

### Panel 03 — Where capital works *(90s — this is the centrepiece)*

**Click "Where capital works."**

> Here is where I have to tell you something.
>
> An earlier version of this page showed a big headline: **one rupee protects twenty rupees.**
> It looked incredible. It was also completely false.
>
> Remember diagram three? That twenty came from dividing the objective by the cost. But the
> objective is an index — rupee-days plus a count plus rupee-hours. Dividing an index by
> rupees does not give you rupees. It gives you index-per-rupee.
>
> We caught it, and we fixed it. This is the honest version.

**Point at the three-part hero.**

> ₹11.2 crore of capital committed. ₹12.2 crore of commerce protected — that is real payment
> value that stayed on its original dates. So the true rupee leverage is **1.08×**.
>
> Less flashy. But look at the small print — the capital is placed as a facility and repaid
> at term. It is not spent. It is float. A rupee of float holding a rupee-eight of commerce
> on schedule for six days is a genuinely good trade.
>
> And forty percent of the modelled disruption is removed.

**Scroll to "Actions in the selected plan."**

> The plan has two actions, on two different merchants. Note the two columns — size, and
> capital committed. They are different numbers, because some actions move dates rather than
> money.

**Scroll to the candidate table.**

> Seven candidates were generated. One moved the objective. The rest are empty — not because
> the measurement came back zero, but because only the selected plan gets replayed end to
> end. We say so, instead of drawing six tiny bars to look busy.

**Scroll to "Why this merchant."**

> And every claim is traceable. Four merchants downstream. Systemic importance rank two of
> forty. Cover ratio 0.3× — this merchant owes three times what it can cover. Time to first
> impact, 2.8 days.
>
> No AI explanation. No generated paragraph. Just the backend values the decision was
> actually made from.

**Close.**

---

### Panel 04 — The offer *(70s)*

**Click "The offer."**

> And this is what a merchant would see.
>
> "Bridge the payment cycle." Not "you are at risk." Not "default probability." Nobody
> wants a loan that opens by telling them they are failing.
>
> ₹7.73 crore, six days, single repayment at term. Sized against ₹13.5 crore of obligations
> falling due, and their actual liquidity buffer.
>
> The indicative cost is ₹1.92 lakh — computed from a stated assumption, not quoted.

**Point at the line under the button.**

> And this line is the one I am proudest of. **Razorpay Test Mode. Recorded as a plan. No
> funds move.**
>
> We did not assume Route was enabled. We probed the account. It is not. So we say so, right
> on the offer, instead of pretending a transfer happened.

**Click "Review offer."**

> The full terms, and the backend's own disclaimer, verbatim. Model output on a synthetic
> benchmark. Not a credit approval.

**Close. Land back on the four panels.**

---

## 5 · Close — 30 seconds (9:00)

> So — a paper says a payment platform can enforce repayment through the rail.
>
> We asked the question that leaves open. Where does the money go?
>
> And the answer is a network answer. Estimate the dependencies. Simulate the shock. Replay
> the fix and measure what it actually saved. Then write one offer, for one merchant, sized
> to their own payment cycle.
>
> Four hundred and thirty-eight tests. Every figure traceable to a backend field. And one
> number we deleted because it was too good to be true.
>
> Razorpay is the only party that can see both sides of a transaction. That is not a
> credit-scoring advantage. It is a **network** advantage.
>
> That is Keystone.

---

## Numbers cheat-sheet

| | |
| --- | --- |
| Network | 100 merchants · 216 relationships · 11,413 payment events · 7 scenarios |
| Default shock | m0070 misses ₹53.05 L from m0008, due t=19h |
| Impact | 1 merchant reached · ₹17.9 L delayed · cascade depth 1 |
| Dot matrix | 1 reached · 10 under-covered · 55 clear their book · 34 nothing due |
| Plan | ₹11.2 Cr committed → ₹12.2 Cr protected → 1.08× · 40.4% disruption removed |
| Actions | m0009 ₹3.51 Cr @ 19h for 6.2d · m0005 ₹7.73 Cr for 6.2d (contract) |
| Candidates | 7 generated · 1 moved the objective · optimality gap 0.0% |
| m0009 evidence | 4 downstream · rank 2 of 40 · cover 0.3× · first impact 2.8 d |
| Offer | ₹7.73 Cr · 6.2 d · bullet · ₹1.92 L cost · 14.6% a year |
| Razorpay | Test Mode · transfers and Route not enabled · recorded as a plan |

## If you overrun

Cut in this order: the correlation line in panel 01, the multi-node scenario switch in
panel 02, and "Review offer" in panel 04. Never cut the ₹1 → ₹20 correction — it is the
strongest thing in the demo.
