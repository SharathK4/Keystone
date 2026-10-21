# PayPal — Gate P0 / Step 0

**Status: Step 0 complete. No feature code written.** The brief's stop condition
for (a) is *not* triggered. Two submission blockers were found in (f).

Baseline is shared with the structure brief and is green — see
[`../structure/step0.md`](../structure/step0.md#baseline-03--matches-the-brief-exactly).

---

## (a) `execution/`

- **Protocol** `ExecutionProvider` (`src/lce/execution/providers.py:101`):
  `name`, `capabilities() -> dict[str, bool]`,
  `execute(intervention, *, dry_run=True) -> ExecutionRecord`.
- **`ExecutionRecord`** (`:58`): `status ∈ {executed, planned, failed}`, `detail`,
  `latency_ms`.
- **`SimulationProvider`** (`:114`): every type "capable"; records the action and
  leaves the effect to replay.
- **`RazorpayTestProvider`** (`:164`): refuses live mode at construction
  (`ConfigError`, `:176-181`); capabilities established by calling the endpoint
  an action would use (`_endpoint_available`, `:84`), cached, probe failures
  logged as `false`; `_REQUIRED_CAPABILITY` maps each type to one capability
  (`:151`). `execute` returns `planned` with the missing capability unless the
  caller opts in **and** the capability is present.
- **`build_provider(name)`** with registry `PROVIDERS` (`:383`).
- **`/execution/status`** → `ExecutionStatus` (`src/lce/snapshot/models.py:424`):
  `provider, mode, configured, api_reachable, capabilities: dict[str, bool],
  executable_intervention_types: list[str], fallback_provider, note`.
  **`capabilities` is an open map**, so PayPal capabilities fit without changing
  the model. Two limits: (1) it describes **one** provider; (2) it has no
  per-capability **reason**. Reasons (brief §5.1) therefore need a new additive
  model and route.
- **Wiring**: `execution_status()` in `src/lce/snapshot/dashboard.py:39` is
  **hard-wired to `RazorpayTestProvider`**. Adding PayPal needs a provider
  selection there — smallest change: a setting (e.g. `LCE_EXECUTION_PROVIDER`)
  defaulting to today's behaviour, plus a test proving the default response is
  byte-identical.

## (b) Actions, offers, fee

- `InterventionType` (`src/lce/domain/enums.py:115`): `liquidity_injection`,
  `receivable_acceleration`, `supplier_term_extension`,
  `repayment_restructure`, `credit_line_increase`.
- `Intervention` (`src/lce/domain/intervention.py:49`): `t`, `amount`,
  `shift_hours`, `target_obligation_id`, `tranches`, `tranche_spacing_hours`,
  rates; `cost` differs from `amount` by type (a term extension commits almost
  nothing).
- `OfferContract` (`src/lce/snapshot/models.py:356`): offer id, merchant,
  scenario, type, `proposed_amount`, `currency`, `duration_hours`, `repayment`
  (structure, n, instalment, first due, cadence), `indicative_cost`,
  `indicative_rate_annual_pct`, `rationale`, `expected_network_benefit`,
  `eligibility`, `status`, `disclaimer`, `provenance`.
- `FACILITY_FEE_RATE_PER_DAY = 0.0004` (`src/lce/snapshot/views.py:40`), used by
  `build_offer` and recorded in the build manifest (`build.py:665`).
  `OFFERABLE` is defined at `views.py:48`.

## (c) `razorpay/`

- `client.py` — REST client; `mapper.py` — provider objects → domain events;
  `webhooks.py` — HMAC-SHA256 `compute_signature` / `verify_signature`
  (constant-time `hmac.compare_digest`) / `require_signature`, `parse_webhook` →
  `WebhookEnvelope`.
- Routes: `src/lce/api/routers/webhooks.py` (`prefix="/webhooks"`).
- Tests: `tests/test_razorpay.py` has 3 `razorpay_live` tests (deselected by
  default) and mocked tests; `tests/test_seeds_and_config.py` patches config.
- PayPal differs materially: signatures are **RSA over
  `transmission_id|time|webhook_id|crc32(body)`** with a downloaded cert, not a
  shared-secret HMAC (see `facts.md`). Nothing in `razorpay/webhooks.py` can be
  reused beyond the "verify, dedupe, persist, 2xx fast" shape.

## (d) Settings, markers, audit, migrations

- `pydantic-settings` (`src/lce/config.py`): `RazorpaySettings` (`:35`, secrets as
  `SecretStr`, aliases `RAZORPAY_*`, reads `.env`), `SimulationSettings`
  (`LCE_SIM_`), `ObjectiveSettings` (`LCE_OBJ_`), `Settings` (`:123`).
- `.env.example` keys: `DATABASE_URL, RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET,
  RAZORPAY_WEBHOOK_SECRET, RAZORPAY_ACCOUNT_ID, RAZORPAY_MODE,
  MODEL_ARTIFACT_DIR, RANDOM_SEED`. (`.env` exists; not read.)
- `pyproject.toml`: `addopts = "-q --strict-markers -m 'not razorpay_live'"`;
  markers `slow, razorpay_live, requires_torch, requires_ortools`. A
  `paypal_live` marker must be added **and** the addopts expression extended.
- `audit_backend.py` — 22 checks: `secrets_not_committed, env_is_gitignored,
  no_env_file_staged, no_key_file_in_repo_root, serving_imports_clean,
  model_artifact_loads, model_artifact_has_integrity_hash,
  model_artifact_records_calibration, snapshot_loads, snapshot_has_scenarios,
  snapshot_carries_no_payment_history, snapshot_overlay_is_estimated,
  every_scenario_has_provenance, on_demand_analysis_is_bounded,
  on_demand_analysis_is_deterministic, all_frontend_routes_present,
  openapi_schema_publishes, migrations_present, migrations_have_one_root,
  migrations_chain_unbroken, migrations_have_one_head, live_mode_refused`.
- Migrations: `0001_initial_schema`, `0002_provenance_and_import_runs`; one head
  (`0002_provenance`). A `0003` must chain from it.

## (e) Razorpay in the frontend

16 occurrences in 7 files: `RazorpayContractCard.tsx` (4, plus the component
name), `OfferExpanded.tsx` (3), `OfferPanel.tsx` (2), `InterventionExpanded.tsx`,
`TexturedTile.tsx`, `lib/format.ts`, `styles/tokens.css` (`--rzp*` tokens).
Execution status is read once and cached by URL in `src/api/client.ts` like all
other GETs — the brief's §7.4 exception (no cache for execution/audit reads)
needs a second, uncached path in the same file.

## (f) Licences — **two submission blockers**

1. **No `LICENSE` file**, and `pyproject.toml` declares
   `license = { text = "Proprietary" }`. The rules require an open-source licence
   visible in the repo's About section. Needs the owner's choice.
2. **All three fonts are "PERSONAL USE ONLY" demo fonts**, and all are tracked
   and already pushed to `origin/master`:
   - `frontend/public/fonts/against-regular.otf` — **in use** for the Keystone
     wordmark and every display heading. Licence (`AGAINST-LICENSE.txt`):
     *"This demo font is for PERSONAL USE ONLY! and NO COMMERCIAL USE ALLOWED."*
   - `fonts/against_2.zip`, `fonts/emotions-condensed-demo.zip` (same terms),
     `fonts/wolfgang.zip` (*"PERSONAL USE ONLY"*) — unused.

   A public repository under an open-source licence cannot redistribute these,
   and the rules require that the submission not violate anyone's IP. They must
   be removed from the tree and replaced with an OFL-licensed display face.
   Removing them from HEAD does not remove them from history; rewriting history
   is out of scope without the owner's say-so.

Other tracked images (`image*.png` at the root and in `frontend/`) are
screenshots of Keystone itself — no third-party IP — but they show the Razorpay
name and the demo font and will need regenerating. `frontend/src/assets/hero.png`
and `vite.svg` are Vite template assets.

## Git evidence for "significantly updated"

`origin` = `github.com/SharathK4/Keystone`. 58 commits, all dated
**2026-08-31 → 2026-09-04**; **0 commits after 2026-10-01**; working tree clean
at `0a699b0`. That history is the cleanest possible "before" for the
significant-update explanation. Suggest tagging it
(`git tag pre-paypal-hackathon 0a699b0`) so `docs/HACKATHON_UPDATES.md` can diff
against a name rather than a hash.
