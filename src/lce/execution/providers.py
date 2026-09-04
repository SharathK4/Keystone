"""Executing a chosen intervention, behind a provider boundary.

The optimiser decides *what* to do. This module decides *where that happens*, and
the two are separated so the research half never depends on a payment provider
being configured, reachable, or capable.

Providers
---------
``SimulationProvider``
    Applies the action to the simulator. The default, and the only one used by
    every experiment and benchmark in this repository.

``RazorpayTestProvider``
    Maps an action onto Razorpay **Test Mode** operations. It moves no live
    funds, and it does not assume the account can move funds at all.

What the Razorpay provider will and will not do
-----------------------------------------------
It reads credentials from the environment (never from a request, never from a
literal), probes what the account can actually do, and **plans** rather than
executes anything it cannot verify support for. Direct Transfers in particular
are a Route capability that most test accounts do not have enabled; assuming
otherwise would produce an integration that works on one account and fails
silently on another. When the capability is absent the action is returned as a
recorded, unexecuted plan with the reason attached - which is a truthful result,
not a failure to hide.

Live mode is refused outright. There is no code path in this phase that sends
real money, and the refusal is explicit rather than implicit so it cannot be
reached by configuration alone.

Secrets never reach a log line: the client redacts them, and nothing here
stringifies a settings object.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from lce.config import RazorpayMode, RazorpaySettings, get_settings
from lce.domain.enums import InterventionType
from lce.domain.intervention import Intervention
from lce.errors import ConfigError, LCEError
from lce.logging import get_logger

logger = get_logger(__name__)


class ExecutionError(LCEError):
    """The provider could not carry out the action."""

    code = "execution_error"


@dataclass(slots=True)
class ExecutionRecord:
    """What a provider did, or decided not to do, for one action."""

    intervention_id: str
    provider: str
    status: str
    """``executed``, ``planned`` (capability absent or dry run), or ``failed``."""
    detail: dict[str, Any] = field(default_factory=dict)
    external_ids: dict[str, str] = field(default_factory=dict)
    latency_ms: float = 0.0

    @property
    def executed(self) -> bool:
        return self.status == "executed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "intervention_id": self.intervention_id,
            "provider": self.provider,
            "status": self.status,
            "detail": self.detail,
            "external_ids": self.external_ids,
            "latency_ms": round(self.latency_ms, 2),
        }


def _endpoint_available(client: Any, path: str) -> bool:
    """Whether a read of ``path`` succeeds on this account.

    A 400 from Razorpay for a list endpoint means the product is not enabled -
    Route's ``/transfers`` and ``/accounts`` behave exactly this way on an
    account without Route. Any failure is treated as "not available", which is
    the safe direction: the consequence of a false negative is that an action is
    planned rather than executed, and the consequence of a false positive is an
    attempted money movement that fails.
    """
    try:
        client._get(path, {"count": 1})
    except Exception:
        return False
    return True


class ExecutionProvider(Protocol):
    """Where an intervention is carried out."""

    name: str

    def capabilities(self) -> dict[str, bool]: ...

    def execute(
        self, intervention: Intervention, *, dry_run: bool = True
    ) -> ExecutionRecord: ...


@dataclass(slots=True)
class SimulationProvider:
    """Execution inside the simulator - the only provider the research uses.

    Deliberately does not run the simulation itself. An intervention's effect is
    a property of the *whole* plan and the network it lands on, so applying one
    action in isolation would produce a number that nothing else in the system
    would agree with. Replay is the simulator's job
    (:func:`lce.intervention.evaluate.replay`); this provider records the action
    as accepted for execution and hands it on.
    """

    name: str = "simulation"

    def capabilities(self) -> dict[str, bool]:
        return dict.fromkeys((str(t) for t in InterventionType), True)

    def execute(
        self, intervention: Intervention, *, dry_run: bool = True
    ) -> ExecutionRecord:
        started = time.perf_counter()
        return ExecutionRecord(
            intervention_id=intervention.intervention_id,
            provider=self.name,
            status="planned" if dry_run else "executed",
            detail={
                "type": str(intervention.type),
                "merchant_id": intervention.merchant_id,
                "amount": intervention.amount,
                "t": intervention.t,
                "cost": intervention.cost,
                "description": intervention.describe(),
                "note": "applied by the simulator when the plan is replayed",
            },
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )


#: Which Razorpay capability each action type would need. Only the first is
#: something a Test-Mode account can be expected to have; the rest are ledger or
#: agreement changes that live in a lending system, not in a payments API.
_REQUIRED_CAPABILITY: dict[InterventionType, str] = {
    InterventionType.LIQUIDITY_INJECTION: "transfers",
    InterventionType.CREDIT_LINE_INCREASE: "credit_ledger",
    InterventionType.RECEIVABLE_ACCELERATION: "transfers",
    InterventionType.SUPPLIER_TERM_EXTENSION: "term_ledger",
    InterventionType.REPAYMENT_RESTRUCTURE: "term_ledger",
}


@dataclass(slots=True)
class RazorpayTestProvider:
    """Maps an action onto Razorpay Test Mode, executing only what is supported.

    ``settings`` is read from the environment by default. Live mode raises: this
    phase has no path that moves real money, and the check is at construction so
    a misconfigured deployment fails at startup rather than at the first payout.
    """

    settings: RazorpaySettings | None = None
    name: str = "razorpay_test"
    _capabilities: dict[str, bool] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        cfg = self.settings or get_settings().razorpay
        if cfg.mode is not RazorpayMode.TEST:
            raise ConfigError(
                "RazorpayTestProvider refuses to run outside Test Mode; this phase "
                "implements no live money movement",
                mode=str(cfg.mode),
            )
        self.settings = cfg

    @property
    def config(self) -> RazorpaySettings:
        assert self.settings is not None
        return self.settings

    # ------------------------------------------------------------ capabilities

    #: Endpoints probed to establish a capability, and the capability each
    #: proves. All are reads: a probe must never have a side effect on the
    #: account it is inspecting.
    _PROBE_ENDPOINTS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("payments", "/payments"),
        ("orders", "/orders"),
        ("settlements", "/settlements"),
        ("transfers", "/transfers"),
        ("route_accounts", "/accounts"),
    )

    def capabilities(self, *, refresh: bool = False) -> dict[str, bool]:
        """Probe what this account can actually do, once, and cache it.

        Every capability is established by *calling the endpoint that would be
        used*, not by inferring one from configuration. Route is the case that
        matters: an earlier version reported ``transfers`` available whenever a
        ``RAZORPAY_ACCOUNT_ID`` happened to be set, which is not the same
        question - Route is a product that has to be enabled on the account, and
        a test account without it returns 400 from ``/transfers`` no matter what
        is configured locally. Inferring it would mean the optimiser believing it
        could move money on an account that cannot.

        Probes are reads and are individually guarded, so one unavailable
        product never hides the availability of another. ``credit_ledger`` and
        ``term_ledger`` are always false: those are lending-ledger operations
        with no payments API behind them.
        """
        if self._capabilities is not None and not refresh:
            return dict(self._capabilities)

        capabilities = {
            "api_reachable": False,
            "payments": False,
            "orders": False,
            "settlements": False,
            "transfers": False,
            "route_accounts": False,
            "credit_ledger": False,
            "term_ledger": False,
        }
        if not self.config.configured:
            self._capabilities = capabilities
            return dict(capabilities)

        try:
            from lce.razorpay.client import RazorpayClient

            with RazorpayClient(self.config) as client:
                capabilities["api_reachable"] = client.health().get("status") == "ok"
                if capabilities["api_reachable"]:
                    for name, path in self._PROBE_ENDPOINTS:
                        capabilities[name] = _endpoint_available(client, path)
        except LCEError as exc:
            logger.warning("razorpay_capability_probe_failed", error=exc.code)
        except Exception as exc:  # network failures are expected during a probe
            logger.warning("razorpay_capability_probe_failed", error=type(exc).__name__)

        self._capabilities = capabilities
        logger.info("razorpay_capabilities_probed", **dict(capabilities))
        return dict(capabilities)

    # --------------------------------------------------------------- execution

    def execute(
        self, intervention: Intervention, *, dry_run: bool = True
    ) -> ExecutionRecord:
        """Plan or execute one action against Test Mode.

        ``dry_run`` defaults to ``True``. Nothing in this repository calls it with
        ``False``; the parameter exists so an integration that has verified its
        capabilities can opt in explicitly, one action at a time.
        """
        started = time.perf_counter()
        needed = _REQUIRED_CAPABILITY[intervention.type]
        available = self.capabilities()

        detail: dict[str, Any] = {
            "type": str(intervention.type),
            "merchant_id": intervention.merchant_id,
            "amount_inr": intervention.amount,
            "amount_paise": round(intervention.amount * 100),
            "required_capability": needed,
            "mode": str(self.config.mode),
            "account_configured": bool(self.config.account_id),
        }

        if not available.get(needed, False):
            detail["reason"] = (
                f"the account does not expose {needed!r} in test mode; the action "
                "is recorded as a plan and not attempted"
            )
            return ExecutionRecord(
                intervention_id=intervention.intervention_id,
                provider=self.name,
                status="planned",
                detail=detail,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        if dry_run:
            detail["reason"] = "dry run: the mapped request was built but not sent"
            detail["mapped_request"] = self.map_to_request(intervention)
            return ExecutionRecord(
                intervention_id=intervention.intervention_id,
                provider=self.name,
                status="planned",
                detail=detail,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        # Reached only when a caller has explicitly opted in *and* the capability
        # probe succeeded. Even then this phase performs no write: the mapped
        # request is returned for a caller to submit through its own reviewed
        # path, because an optimiser deciding to move money unattended is not a
        # thing this system should be able to do.
        detail["reason"] = (
            "execution is not performed in this phase; the mapped Test-Mode "
            "request is returned for a reviewed submission path"
        )
        detail["mapped_request"] = self.map_to_request(intervention)
        return ExecutionRecord(
            intervention_id=intervention.intervention_id,
            provider=self.name,
            status="planned",
            detail=detail,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    def map_to_request(self, intervention: Intervention) -> dict[str, Any]:
        """The Test-Mode request an action corresponds to, as data.

        Amounts are in paise, as every Razorpay money field is. Returning the
        request rather than sending it keeps the mapping reviewable and testable
        without a network call or an account.
        """
        paise = round(intervention.amount * 100)
        match intervention.type:
            case InterventionType.LIQUIDITY_INJECTION | InterventionType.RECEIVABLE_ACCELERATION:
                return {
                    "endpoint": "/transfers",
                    "method": "POST",
                    "body": {
                        "account": self.config.account_id or "<RAZORPAY_ACCOUNT_ID>",
                        "amount": paise,
                        "currency": "INR",
                        "notes": {
                            "intervention_id": intervention.intervention_id,
                            "type": str(intervention.type),
                            "merchant_id": intervention.merchant_id,
                        },
                    },
                }
            case _:
                return {
                    "endpoint": None,
                    "method": None,
                    "body": None,
                    "note": (
                        f"{intervention.type} changes a credit or repayment term, "
                        "which is a lending-ledger operation rather than a payments "
                        "API call; it has no Razorpay endpoint"
                    ),
                }


def execute_plan(
    provider: ExecutionProvider,
    interventions: list[Intervention],
    *,
    dry_run: bool = True,
) -> list[ExecutionRecord]:
    """Run a whole plan through one provider, in order."""
    records = [provider.execute(u, dry_run=dry_run) for u in interventions]
    logger.info(
        "plan_executed",
        provider=provider.name,
        n_actions=len(records),
        n_executed=sum(1 for r in records if r.executed),
        dry_run=dry_run,
    )
    return records


PROVIDERS: dict[str, type] = {
    "simulation": SimulationProvider,
    "razorpay_test": RazorpayTestProvider,
}


def build_provider(name: str) -> ExecutionProvider:
    try:
        return PROVIDERS[name]()
    except KeyError as exc:
        raise ExecutionError(
            f"unknown execution provider {name!r}", available=sorted(PROVIDERS)
        ) from exc
