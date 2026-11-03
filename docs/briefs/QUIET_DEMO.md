KEYSTONE: THE QUIET DEMO (replaces the "final stretch" brief)

Today is Nov 3. Submit the evening of Nov 11 IST; hard deadline Nov 12, 12:00 PT (Nov 13, 01:30 IST). docs/briefs/RUNBOOK.md still governs everything not changed here (git rules, no AI attribution, freeze, anchors, sandbox only, never read .env, live PayPal calls only after the human types "run live tests", no pushes to origin). Read reports/STATUS.md first and keep it updated. Usage is not a constraint: work in long, thorough runs, verify everything, and stop only at the points named below.

1. WHAT WE ARE BUILDING
One page, six quiet scenes, one idea each, mostly white space. It is a working instrument, not a video and not a gallery: every change on screen is caused by a real event or a click, and every number comes from an API response. A stranger watching for three minutes must understand: a payment fails, who feels it and when, what waiting costs, the fix, it is paid through PayPal, the webhook confirms.
The existing five-card gallery stays untouched as "Evidence", reachable from one quiet link at the bottom and by its old hashes. The new page becomes the default route.

2. DESIGN: RESTRAINT EVERYWHERE EXCEPT THE MONEY
Negative space: scenes are at least 100svh; the content column is at most 34rem; the focal object (dot field, chart, contract card) is at most 360px wide and centred; vertical gaps between scenes are at least 20vh. One headline, one sentence, one control per scene.
Copy budget: headline at most 8 words; sentence at most 20; model-written text at most 25; provenance stamps exempt. No tables, no paragraphs, no legends on the page (state is named in the sentence and exposed on hover and focus). Numbers are large and come from the API; no number is typed in the frontend.
Visual DNA kept: paper background, ink, the hundred-dot field, a display serif for headlines, the quiet provenance pills, the ember contract card. The contract card is the ONLY textured or saturated object on the page and appears only in scene 4. Fonts: OFL only; default to Instrument Serif for headlines (self-hosted through @fontsource, licence committed) unless the human names another; keep the face in one token so a licensed face can be restored. Body: the existing sans. All text, stamps included, meets 4.5:1 contrast.
Motion: nothing autonomous, nothing looping, no counting-up numbers, no scroll-jacking, no video, GIF, Lottie, canvas animation or auto-advance. Allowed: a single 160 to 200 ms opacity or colour change on the element that just changed, triggered by an event or a click; hover and focus feedback; prefers-reduced-motion removes all of it. Liveness comes from real arrival: timestamps, PayPal reference ids, and the field changing because something happened.
Chrome: a small wordmark top left, the existing currency switch top right (USD default), six static progress ticks on the right edge, a quiet "Evidence" link in the footer. Nothing else.

3. THE SIX SCENES (copy is a template; every figure is filled from the API)
 1 "One payment fails." The dot field. Before an event: one quiet button "Fire a PayPal event". After: one dot changes state and the sentence becomes "m0013 missed $3.38M." Stamp: "arrived from PayPal sandbox, 14:02:11" or "recorded PayPal event, processed live". Hosted default is recorded mode (a real recorded payload goes through the real signature check, state machine and re-entry pipeline, so the analysis is computed for real). With a demo token the button sends a real sandbox payout designed to fail.
 2 "Two more feel it." The field recolours the reached merchants; sentence "39 hours later, one step out." One text field with the placeholder "Ask: what if it lasts three days?". Enter parses it with the language model into a validated request, shows the parsed chip ("m0013 · 3 days"), then recomputes live ("computed now, 1.6 s").
 3 "Every hour costs." One chart with two lines (plan as chosen, re-planned) and a discrete delay selector. Sentence: "The same fix, landing later, does less." No cards, no breakdowns.
 4 "Cover it." The contract card, the single rich object. Sentence: "$773K to m0005 for six days." Under it, one model-written sentence labelled "written by a language model; every number matches the result". Button: "Review and sign".
 5 "Paid." A ruled ledger of two or three rows: payee, amount, status, PayPal reference. After signing, rows say "waiting on PayPal" and update themselves when the webhook arrives (a bounded live window: server-sent events or at most 90 s of 2 s polls, started by the user's click, ended at a terminal state or timeout). A failed or unclaimed payout adds one sentence and a "Plan again" button that re-enters as a new shock.
 6 "What we cannot promise." One sentence of honest limits from the proof data ("On this network a failure spreads one step at most.") and the link "See everything that failed".

4. AI THAT DOES VISIBLE WORK (bounded)
Provider interface; ANTHROPIC_API_KEY and the model name through settings; a recorded-response fallback when no key exists; token-gated and rate-limited when hosted. (a) Language to a schema-validated ShockRequest within the existing analyze limits; refuse out-of-bounds input with a one-line reason; show the parsed request before computing. (b) The scene-4 sentence: the model sees only the structured result; a test proves every number in its output appears in the result after formatting normalisation; fall back to templated text on failure. (c) When a payout fails, one grounded incident sentence. The model never chooses amounts or interventions and never executes. Log prompt hashes, no PII. Ship scripts/eval_ai.py reporting real schema-validity, refusal and grounding pass rates. Provenance gets a fifth kind, "Language model", showing the model name the backend reports.

5. PAYPAL AND HOSTING (what makes it real)
 - lce paypal doctor: a read-only report of exactly what is missing (env vars, token, mapped identities, webhook id, public URL).
 - Trigger research, one day at most, the human runs the live part: find which payout reliably yields a fast failed, held or unclaimed webhook in the sandbox (use the ERRPYO002 test note only if the docs confirm it). If none does, use PayPal's webhook simulator events and stamp them "simulated by PayPal's webhook simulator". Never stamp a simulated event as real.
 - Endpoints (token-gated and rate-limited, canonical plan only): fire event (recorded or real), parse, explain, execute, ledger window. Record real payloads as fixtures; never hand-write them.
 - Render: verify render.yaml, write exact deploy steps, and scripts/verify_public_url.py (health, snapshot read, signed simulated webhook, token gate, rate limits). Hosted analysis works with no database.

6. PROOF THAT IT IS QUIET AND THAT IT WORKS (automated; fix until green)
 - Playwright at 1440x900 and 390x844 for every scene and state; critique your own screenshots with a squint test and fix anything crowded.
 - Negative space: in every scene the visible content's bounding boxes cover at most 35% of the viewport; assert it.
 - Copy budget: assert the word limits per scene (stamps and numbers exempt).
 - Motion: after load, document.getAnimations() is empty; with prefers-reduced-motion nothing animates; assert there is no video, canvas or looping CSS.
 - Accessibility: axe clean; a keyboard-only run of the whole walk.
 - The 3-minute walk as a test: e2e/walk.spec runs the real path in recorded mode (fire the event, ask the what-if, pick a delay, review and sign, watch the ledger update). It fails if any step needs more than a few clicks or the path exceeds 90 seconds of interaction. Save its screenshots under docs/screens/ for Devpost.
 - Numbers: a test that every displayed number maps to a response field, plus the AI grounding test above.
 - Backend: tests, ruff, mypy, audit and the phase-4 anchors stay green; frozen anchors unchanged.

7. SUBMISSION READINESS (finish by Nov 9)
Licence visible; Against removed from the repo and the font swap recorded in docs/ASSETS.md; make_clean_copy.sh and audit.sh passing; README PayPal-first (three run modes, Tools used and how, stating that development was AI-assisted, limitations with intervals, what is new since Oct 1); generated docs/HACKATHON_UPDATES.md; a Devpost text draft; docs/DEMO_PATH.md, one page: the exact click path, the pre-flight checklist and what to do if PayPal is down. No video work.

8. SCHEDULE AND CUTS
Nov 3-4 scenes 1 to 3 and the recorded pipeline. Nov 5 deploy; the human registers the webhook and says "run live tests". Nov 6-7 AI and scenes 4 and 5. Nov 8 polish and the test suite. Nov 9 submission readiness. Nov 10 human rehearsal and recording. Nov 11 submit. If the live loop (fire, analyse, sign, pay, webhook) does not work end to end by the end of Nov 6, stop everything else and fix it. CUT: stress network, Render Workflows, invoicing (unless spike S3 passed and a day is spare), Edge, Fog, more plates.
REPORT after each day, under 200 words: passed, failed, unverified, what needs the human, next action.
