"""``lce paypal doctor`` - exactly what is missing before a live sandbox run.

Read-only. It reads settings (never printing a credential), the local
database's identity table, the walk's recording and plan, and - only when asked
- PayPal (one token request, ``--probe``) and the deployment (one health read,
``--public-url``). Each line says ``ok``, ``missing`` or ``unchecked`` and, when
something is missing, the command or setting that fixes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str  # ok | missing | unchecked | refused
    detail: str
    fix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "fix": self.fix}


def _walk_plan_merchants() -> tuple[str, list[str]]:
    """The walk's merchant, and the merchants its plan would pay.

    Read from the walk's replay at zero delay, which lists every selected
    action; the analysis response names only the first one.
    """
    from lce.config import REPO_ROOT
    from lce.snapshot.store import require_store
    from lce.walk.speed import read_walk_speed
    from lce.walk.thread import get_walk_settings

    walk = get_walk_settings()
    store = require_store()
    data = read_walk_speed(REPO_ROOT / "artifacts" / "structure", store.snapshot_id,
                           [walk.merchant], walk.multiple, 0.0)
    actions = data["delays"][0]["as_chosen"]["actions"]
    payees = sorted({a["merchant_id"] for a in actions
                     if a["action_type"] == "liquidity_injection"})
    return walk.merchant, payees


def diagnose(*, probe: bool = False, public_url: str | None = None) -> list[Check]:
    from lce.config import get_paypal_settings, get_settings

    checks: list[Check] = []
    p = get_paypal_settings()
    refused = p.refusal()
    checks.append(Check("sandbox_only", "refused" if refused else "ok",
                        refused or f"mode {p.mode}, host {p.api_host}",
                        "set PAYPAL_MODE=sandbox" if refused else None))
    checks.append(Check("credentials", "ok" if p.configured else "missing",
                        "client id and secret are set" if p.configured
                        else "PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are not both set",
                        None if p.configured else
                        "developer.paypal.com > Apps & Credentials (Sandbox) > your app; "
                        "put both values in .env"))
    provider = get_settings().execution_provider
    checks.append(Check("execution_provider", "ok" if provider == "paypal_sandbox" else "missing",
                        f"LCE_EXECUTION_PROVIDER={provider}",
                        None if provider == "paypal_sandbox"
                        else "set LCE_EXECUTION_PROVIDER=paypal_sandbox (live_sandbox.sh does)"))
    checks.append(Check("webhook_id", "ok" if p.webhook_configured else "missing",
                        "PAYPAL_WEBHOOK_ID is set" if p.webhook_configured
                        else "PAYPAL_WEBHOOK_ID is not set, so no delivery can be verified",
                        None if p.webhook_configured else
                        "register <public url>/api/v1/webhooks/paypal in the app's Webhooks "
                        "(Payouts item + batch events) and put its id in .env"))

    if probe and p.configured and not refused:
        from lce.paypal.client import PayPalClient

        with PayPalClient(p) as client:
            token = client.probe_token()
        checks.append(Check("token", "ok" if token.available else "missing", token.reason,
                            None if token.available else "check the client id and secret"))
    else:
        checks.append(Check("token", "unchecked", "PayPal was not called",
                            "lce paypal doctor --probe (one token request)"))

    try:
        from sqlalchemy import select

        from lce.data.database import session_scope
        from lce.data.orm import PayPalIdentityRow

        walk_merchant, payees = _walk_plan_merchants()
        with session_scope() as session:
            mapped = {r.merchant_id: r.receiver for r in session.execute(
                select(PayPalIdentityRow)).scalars()}
        for merchant in [walk_merchant, *payees]:
            role = ("the walk's failing payout (map it to an email with no PayPal account, "
                    "so the payout goes unclaimed)" if merchant == walk_merchant
                    else "a payee in the walk's plan (a sandbox personal account)")
            checks.append(Check(
                f"identity:{merchant}", "ok" if merchant in mapped else "missing",
                f"mapped to {mapped[merchant]}" if merchant in mapped else f"not mapped; {role}",
                None if merchant in mapped
                else f"sh scripts/live_sandbox.sh paypal map {merchant} --email <address>"))
    except Exception as exc:  # no database: say so rather than guess
        checks.append(Check("identities", "unchecked",
                            f"the database could not be read ({type(exc).__name__})",
                            "run through scripts/live_sandbox.sh, which migrates its database"))

    from lce.walk.recording import DEFAULT_FIXTURE, load_recorded
    from lce.walk.thread import get_walk_settings

    try:
        recorded = load_recorded()
        checks.append(Check("recorded_event", "ok" if recorded else "missing",
                            f"{recorded.source}, recorded {recorded.recorded_at}" if recorded
                            else f"no recording at {DEFAULT_FIXTURE}",
                            None if recorded else
                            "after a live fire: lce walk record <event id> (needs a capture)"))
    except Exception as exc:
        checks.append(Check("recorded_event", "missing", f"recording refused: {exc}",
                            "re-record it with lce walk record"))
    capture = get_walk_settings().record_deliveries_dir
    checks.append(Check("capturing_deliveries", "ok" if capture else "missing",
                        capture or "LCE_RECORD_DELIVERIES_DIR is empty",
                        None if capture else "run the API through scripts/live_sandbox.sh"))

    if public_url:
        import httpx

        url = public_url.rstrip("/") + "/api/v1/health"
        try:
            status = httpx.get(url, timeout=10.0).status_code
            ok = status == 200
            checks.append(Check("public_url", "ok" if ok else "missing", f"{url} answered {status}",
                                None if ok else "deploy, or check the address"))
        except httpx.HTTPError as exc:
            checks.append(Check("public_url", "missing", f"{url}: {type(exc).__name__}",
                                "deploy, or check the address"))
    else:
        checks.append(Check("public_url", "unchecked", "no address given",
                            "lce paypal doctor --public-url https://<your api>"))
    return checks
