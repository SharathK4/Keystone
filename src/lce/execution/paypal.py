"""PayPal sandbox as an execution provider.

Implements the existing :class:`~lce.execution.providers.ExecutionProvider`
protocol so the rest of the system can treat it like any other provider - with
one deliberate difference. The protocol's ``execute(intervention,
dry_run=False)`` has no notion of a human approval, so through *that* door this
provider refuses to move money. Real execution goes only through
:class:`~lce.execution.service.ExecutionService`, which requires an approval
bound to the exact plan hash. There is no way to pay out by calling the generic
interface, and a test pins that.

Capabilities are established by calling the endpoint each action would use, or
a read-only equivalent, never assumed:

* ``api_reachable`` - an OAuth token is issued for these credentials;
* ``payouts`` - ``GET`` on a payout batch id that cannot exist: 404 means the app
  reaches the Payouts handler, 401/403 means it may not use Payouts;
* ``invoicing`` - ``GET /v2/invoicing/invoices`` (list, read-only);
* ``webhooks`` - ``GET /v1/notifications/webhooks`` (list, read-only).

Each comes back with the HTTP status and a reason, so "not available" always
says why. The 404-means-available reading of the payouts probe is taken from
PayPal's documented error behaviour and is **unverified in the sandbox** until
spike S2 runs; it is labelled as such.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lce.config import PayPalSettings, get_paypal_settings
from lce.domain.intervention import Intervention
from lce.errors import ExecutionRefused
from lce.execution.plan import PAYPAL_MAPPINGS
from lce.execution.providers import ExecutionRecord
from lce.paypal.client import PROBE_BATCH_ID, PayPalClient, ProbeResult

PROVIDER_NAME = "paypal_sandbox"

#: Probing makes several live calls; cache the answer briefly so a page load
#: does not re-probe PayPal every time /execution/status is read.
PROBE_TTL_SECONDS = 300.0

#: Which capability each mapping needs.
MAPPING_CAPABILITY = {"payout": "payouts", "invoice": "invoicing"}


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    available: bool | None
    reason: str
    endpoint: str | None
    http_status: int | None
    verified_in_sandbox: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "reason": self.reason,
            "endpoint": self.endpoint,
            "http_status": self.http_status,
            "verified_in_sandbox": self.verified_in_sandbox,
        }


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    provider: str
    mode: str
    configured: bool
    refused: str | None
    probed: bool
    probed_at: datetime | None
    capabilities: tuple[Capability, ...] = field(default_factory=tuple)

    def available(self, name: str) -> bool:
        return any(c.name == name and c.available is True for c in self.capabilities)

    @property
    def executable_intervention_types(self) -> list[str]:
        return sorted(
            action_type
            for action_type, mapping in PAYPAL_MAPPINGS.items()
            if self.available(MAPPING_CAPABILITY.get(mapping, mapping))
        )


def _capability(name: str, result: ProbeResult, *, verified: bool) -> Capability:
    return Capability(
        name=name,
        available=result.available,
        reason=result.reason,
        endpoint=result.endpoint,
        http_status=result.http_status,
        verified_in_sandbox=verified,
    )


class PayPalSandboxProvider:
    """PayPal sandbox; refuses construction in anything but sandbox."""

    name = PROVIDER_NAME

    def __init__(
        self, settings: PayPalSettings | None = None, *, client: PayPalClient | None = None
    ) -> None:
        self.settings = settings or get_paypal_settings()
        self.settings.require_sandbox()
        self._client = client

    @property
    def client(self) -> PayPalClient:
        if self._client is None:
            self._client = PayPalClient(self.settings)
        return self._client

    # ------------------------------------------------------------ capabilities

    def report(self, *, probe: bool = True) -> CapabilityReport:
        mode = str(self.settings.mode)
        configured = self.settings.configured
        refused = self.settings.refusal()
        if not probe or not self.settings.configured:
            reason = "not probed" if self.settings.configured else "credentials not configured"
            return CapabilityReport(
                provider=PROVIDER_NAME,
                mode=mode,
                configured=configured,
                refused=refused,
                probed=False,
                probed_at=None,
                capabilities=tuple(
                    Capability(n, None, reason, None, None, False)
                    for n in ("api_reachable", "payouts", "invoicing", "webhooks")
                ),
            )

        token = self.client.probe_token()
        caps = [_capability("api_reachable", token, verified=True)]
        if token.available:
            caps += [
                _capability(
                    "payouts",
                    self.client.probe(
                        f"/v1/payments/payouts/{PROBE_BATCH_ID}", missing_means_available=True
                    ),
                    verified=False,
                ),
                _capability(
                    "invoicing",
                    self.client.probe("/v2/invoicing/invoices?page=1&page_size=1"),
                    verified=False,
                ),
                _capability(
                    "webhooks", self.client.probe("/v1/notifications/webhooks"), verified=False
                ),
            ]
        else:
            caps += [
                Capability(n, None, "no token, so not probed", None, None, False)
                for n in ("payouts", "invoicing", "webhooks")
            ]
        return CapabilityReport(
            provider=PROVIDER_NAME,
            mode=mode,
            configured=configured,
            refused=refused,
            probed=True,
            probed_at=datetime.now(UTC),
            capabilities=tuple(caps),
        )

    def capabilities(self) -> dict[str, bool]:
        """The protocol's flat view: a capability is true only if probed available."""
        return {c.name: c.available is True for c in cached_report(self).capabilities}

    # --------------------------------------------------------------- execution

    def execute(self, intervention: Intervention, *, dry_run: bool = True) -> ExecutionRecord:
        """A dry run records the action as a plan. A real run is refused here.

        Money moves only through an approved plan
        (:class:`~lce.execution.service.ExecutionService`); the generic protocol
        carries no approval, so it cannot be the door.
        """
        if not dry_run:
            raise ExecutionRefused(
                "PayPal execution requires an approved plan; "
                "the generic provider interface cannot move money",
                intervention_id=intervention.intervention_id,
            )
        mapping = PAYPAL_MAPPINGS.get(str(intervention.type))
        return ExecutionRecord(
            intervention_id=intervention.intervention_id,
            provider=PROVIDER_NAME,
            status="planned",
            detail={
                "type": str(intervention.type),
                "merchant_id": intervention.merchant_id,
                "mapping": mapping,
                "note": "dry run; execution requires an approved plan",
            },
            latency_ms=0.0,
        )


# ------------------------------------------------------------------ status cache

_cache_lock = threading.Lock()
_cache: tuple[float, CapabilityReport] | None = None


def cached_report(
    provider: PayPalSandboxProvider, *, probe: bool = True, refresh: bool = False
) -> CapabilityReport:
    global _cache
    if not probe:
        return provider.report(probe=False)
    with _cache_lock:
        now = time.monotonic()
        if not refresh and _cache is not None and now - _cache[0] < PROBE_TTL_SECONDS:
            return _cache[1]
        report = provider.report(probe=True)
        _cache = (now, report)
        return report


def reset_report_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def refused_report(settings: PayPalSettings) -> CapabilityReport:
    """What status says when the configuration itself is refused."""
    return CapabilityReport(
        provider=PROVIDER_NAME,
        mode=str(settings.mode),
        configured=settings.configured,
        refused=settings.refusal(),
        probed=False,
        probed_at=None,
        capabilities=(),
    )


def paypal_execution_status(*, probe: bool = True) -> Any:
    """``/execution/status`` when ``LCE_EXECUTION_PROVIDER=paypal_sandbox``.

    Same response model as the default provider, so the frontend reads one
    contract. Per-capability reasons live on ``/execution/paypal``.
    """
    from lce.snapshot.models import ExecutionStatus

    settings = get_paypal_settings()
    refused = settings.refusal()
    if refused is not None:
        report = refused_report(settings)
    else:
        report = cached_report(PayPalSandboxProvider(settings), probe=probe)
    capabilities = {c.name: c.available is True for c in report.capabilities}
    executable = report.executable_intervention_types
    if refused is not None:
        note = f"PayPal provider refused: {refused}. Every action is recorded as a plan."
    elif not settings.configured:
        note = (
            "PayPal sandbox credentials are not configured, so every action is "
            "recorded as a plan. No funds move."
        )
    else:
        note = (
            "Recommendations are chosen offline and measured by replay on a "
            "synthetic network. Approved plans execute in the PayPal sandbox at a "
            f"declared demo scale ({settings.demo_scale:f} {settings.demo_currency} "
            "per analytic rupee); no real money moves. "
            + (
                f"{len(executable)} intervention type(s) map to an available "
                "PayPal sandbox operation."
                if executable
                else "No intervention type maps to an available PayPal sandbox "
                "operation, so every action is recorded as a plan."
            )
        )
    return ExecutionStatus(
        provider=PROVIDER_NAME,
        mode=report.mode,
        configured=report.configured,
        api_reachable=capabilities.get("api_reachable", False),
        capabilities=capabilities,
        executable_intervention_types=executable,
        fallback_provider="simulation",
        note=note,
    )
