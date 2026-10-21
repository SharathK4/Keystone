"""Minimal PayPal REST client - sandbox only.

What it covers is what Keystone uses and nothing more: the OAuth2
client-credentials token, Payouts (create, get batch, get item), the
webhook-signature postback, and read-only probes that establish what the account
can actually do.

Three rules shape it:

1. **Sandbox by construction.** The settings are checked before the first byte
   is sent; a live mode or a non-sandbox host raises
   :class:`~lce.errors.ConfigError` from the constructor.
2. **Writes are never retried here.** A payout that times out may or may not
   have been created. Retrying it blindly is how money gets paid twice, so a
   transport failure on a write raises :class:`~lce.errors.PayPalTransportError`
   ("outcome unknown") and the caller decides. Every write carries a
   ``PayPal-Request-Id`` so that an explicit, human-initiated resubmission with
   the same id is answered by PayPal from its record instead of paying again.
3. **Credentials never leave this object.** The token and the client secret are
   not logged, not put in errors, not returned. Error context carries only the
   HTTP status, PayPal's error ``name`` and its ``debug_id``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from lce.config import PayPalSettings, get_paypal_settings
from lce.errors import PayPalError, PayPalTransportError
from lce.logging import get_logger

logger = get_logger(__name__)

#: Refresh a token this many seconds before PayPal says it expires.
TOKEN_SKEW_SECONDS = 60.0

#: A payout batch id that cannot exist. ``GET`` on it separates "the account may
#: use Payouts" (404 - the resource is missing) from "it may not" (401/403).
PROBE_BATCH_ID = "KEYSTONEPROBE0000000000000000"

#: Status codes a *read* may be retried on once. Writes are never retried.
RETRYABLE_READ_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What one read-only call established about a capability."""

    available: bool | None
    http_status: int | None
    reason: str
    endpoint: str


class PayPalClient:
    """Thin wrapper over the PayPal REST API, sandbox only."""

    def __init__(
        self,
        settings: PayPalSettings | None = None,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings or get_paypal_settings()
        # Refuse here, before any request can be built.
        self.settings.require_sandbox()
        self._client = client
        self._owns_client = client is None
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0
        self.requests_sent: list[tuple[str, str]] = []

    # --------------------------------------------------------------- lifecycle

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.settings.api_base,
                timeout=self.settings.timeout_seconds,
                headers={"Accept": "application/json"},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> PayPalClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------- auth

    def _bearer(self) -> str:
        now = self._clock()
        if self._token is not None and now < self._token_expires_at:
            return self._token
        client_id, client_secret = self.settings.require_credentials()
        try:
            response = self._http().post(
                "/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise PayPalTransportError(
                "PayPal token request failed", error=type(exc).__name__
            ) from None
        self.requests_sent.append(("POST", "/v1/oauth2/token"))
        if response.status_code != 200:
            raise _error_from(response, "/v1/oauth2/token")
        body = response.json()
        self._token = str(body["access_token"])
        expires_in = float(body.get("expires_in", 0))
        self._token_expires_at = now + max(0.0, expires_in - TOKEN_SKEW_SECONDS)
        return self._token

    # ---------------------------------------------------------------- requests

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        write = method.upper() != "GET"
        attempts = 1 if write else 2
        last: httpx.Response | None = None
        for attempt in range(1, attempts + 1):
            request_headers = {
                "Authorization": f"Bearer {self._bearer()}",
                "Content-Type": "application/json",
                **(headers or {}),
            }
            try:
                response = self._http().request(
                    method, path, json=json, params=params, headers=request_headers
                )
            except httpx.HTTPError as exc:
                self.requests_sent.append((method, path))
                if write or attempt == attempts:
                    raise PayPalTransportError(
                        "PayPal did not answer" + ("; the outcome is unknown" if write else ""),
                        method=method,
                        path=path,
                        error=type(exc).__name__,
                    ) from None
                continue
            self.requests_sent.append((method, path))
            if response.status_code == 401 and attempt < attempts:
                # An expired token is the one 401 worth a second read.
                self._token = None
            if (
                not write
                and attempt < attempts
                and response.status_code in RETRYABLE_READ_STATUS | {401}
            ):
                last = response
                continue
            return response
        assert last is not None
        return last

    def _json(self, response: httpx.Response, path: str, *, ok: tuple[int, ...]) -> dict[str, Any]:
        if response.status_code not in ok:
            raise _error_from(response, path)
        payload = response.json() if response.content else {}
        return payload if isinstance(payload, dict) else {"items": payload}

    # ----------------------------------------------------------------- payouts

    def create_payout(self, body: dict[str, Any], *, request_id: str) -> dict[str, Any]:
        """``POST /v1/payments/payouts``. Never retried; see the module docstring."""
        if not request_id:
            raise ValueError("a payout needs a PayPal-Request-Id")
        path = "/v1/payments/payouts"
        response = self._send("POST", path, json=body, headers={"PayPal-Request-Id": request_id})
        return self._json(response, path, ok=(200, 201))

    def get_payout(self, payout_batch_id: str) -> dict[str, Any]:
        path = f"/v1/payments/payouts/{quote(payout_batch_id, safe='')}"
        return self._json(self._send("GET", path), path, ok=(200,))

    def get_payout_item(self, payout_item_id: str) -> dict[str, Any]:
        path = f"/v1/payments/payouts-item/{quote(payout_item_id, safe='')}"
        return self._json(self._send("GET", path), path, ok=(200,))

    # ---------------------------------------------------------------- webhooks

    def verify_webhook_signature(self, verification: dict[str, Any]) -> bool:
        """``POST /v1/notifications/verify-webhook-signature`` (postback check).

        Read-only in effect, but a POST, so it is sent once. PayPal answers
        ``{"verification_status": "SUCCESS" | "FAILURE"}``. Note that simulator
        mock events cannot be verified this way; see :mod:`lce.paypal.webhooks`.
        """
        path = "/v1/notifications/verify-webhook-signature"
        body = self._json(self._send("POST", path, json=verification), path, ok=(200,))
        return str(body.get("verification_status", "")).upper() == "SUCCESS"

    # ------------------------------------------------------------------ probes

    def probe(self, endpoint: str, *, missing_means_available: bool = False) -> ProbeResult:
        """Call one read-only endpoint and say what the answer establishes.

        ``missing_means_available`` is for endpoints probed with an id that
        cannot exist: a 404 there means the account reached the resource
        handler, which is what availability means.
        """
        try:
            response = self._send("GET", endpoint)
        except PayPalTransportError:
            return ProbeResult(None, None, "PayPal did not answer", endpoint)
        except PayPalError as exc:
            return ProbeResult(False, _int(exc.context.get("status")), exc.message, endpoint)
        status = response.status_code
        if 200 <= status < 300:
            return ProbeResult(True, status, "endpoint answered", endpoint)
        if status == 404 and missing_means_available:
            return ProbeResult(True, status, "endpoint reachable (probe id not found)", endpoint)
        if status in (401, 403):
            name = _error_name(response)
            return ProbeResult(False, status, f"not permitted for this app ({name})", endpoint)
        return ProbeResult(None, status, f"inconclusive answer ({_error_name(response)})", endpoint)

    def probe_token(self) -> ProbeResult:
        endpoint = "/v1/oauth2/token"
        try:
            self._token = None
            self._bearer()
        except PayPalTransportError:
            return ProbeResult(None, None, "PayPal did not answer", endpoint)
        except PayPalError as exc:
            status = _int(exc.context.get("status"))
            return ProbeResult(False, status, "credentials rejected", endpoint)
        return ProbeResult(True, 200, "token issued", endpoint)


def _int(value: object) -> int | None:
    return int(value) if isinstance(value, int) else None


def _error_name(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        return str(body.get("name") or body.get("error") or f"HTTP {response.status_code}")
    return f"HTTP {response.status_code}"


def _error_from(response: httpx.Response, path: str) -> PayPalError:
    """An error carrying PayPal's diagnosis and nothing that could be a secret."""
    debug_id = response.headers.get("paypal-debug-id")
    name = _error_name(response)
    message = "PayPal rejected the request"
    try:
        body = response.json()
        if isinstance(body, dict):
            debug_id = str(body.get("debug_id") or debug_id or "") or None
            message = str(body.get("message") or body.get("error_description") or message)
    except ValueError:
        pass
    logger.warning("paypal_error", path=path, status=response.status_code, name=name,
                   debug_id=debug_id)
    return PayPalError(
        message, status=response.status_code, name=name, debug_id=debug_id, path=path
    )
