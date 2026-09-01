"""Minimal Razorpay REST client.

Only the read endpoints this system needs are implemented - payments, payouts,
settlements and the account itself. It deliberately does **not** wrap any
money-moving endpoint: this backend models and predicts liquidity, it does not
disburse it, and an intervention it recommends is executed by a human on the
Razorpay dashboard. Keeping the write surface out of the codebase makes that
boundary structural rather than a matter of discipline.

Credentials come from settings and are never logged: the client redacts auth on
every log line and error path.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from lce.config import RazorpaySettings, get_settings
from lce.errors import RazorpayError
from lce.logging import get_logger

logger = get_logger(__name__)

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 100

# Razorpay's collection endpoints cap `count` at 100.
MAX_FETCH_ALL = 100_000

# Status codes worth retrying. 429 is rate limiting; 5xx are transient upstream
# failures. Everything else (401, 400, 404) is a request problem that retrying
# would only repeat.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with full jitter.

    Full jitter (a uniform draw over the whole backoff window, rather than a
    fixed delay) matters when a batch import fans out: without it, every retry
    from a burst of failed calls fires at the same instant and re-creates the
    load that caused the failure.
    """

    max_attempts: int = 5
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 20.0
    respect_retry_after: bool = True

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to wait before ``attempt`` (1-based) is retried."""
        if retry_after is not None and self.respect_retry_after:
            # The server told us how long to wait; obey it rather than guessing.
            return min(max(retry_after, 0.0), self.max_delay_seconds)
        window = min(self.base_delay_seconds * (2 ** max(0, attempt - 1)), self.max_delay_seconds)
        return random.uniform(0.0, window)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "base_delay_seconds": self.base_delay_seconds,
            "max_delay_seconds": self.max_delay_seconds,
            "respect_retry_after": self.respect_retry_after,
        }


class RazorpayClient:
    """Thin, read-only wrapper over the Razorpay v1 API."""

    def __init__(
        self,
        settings: RazorpaySettings | None = None,
        *,
        client: httpx.Client | None = None,
        retry: RetryPolicy | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.settings = settings or get_settings().razorpay
        self._client = client
        self._owns_client = client is None
        self.retry = retry or RetryPolicy()
        # Injectable so tests can assert on backoff without actually waiting.
        self._sleep = sleep
        self.attempts_made = 0

    # ------------------------------------------------------------- lifecycle

    def _http(self) -> httpx.Client:
        if self._client is None:
            key_id, key_secret = self.settings.require_credentials()
            self._client = httpx.Client(
                base_url=self.settings.api_base_url,
                auth=(key_id, key_secret),
                timeout=self.settings.timeout_seconds,
                headers={"Accept": "application/json"},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> RazorpayClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------------------------------------------------------------- helpers

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET with bounded retries on rate limits and transient upstream errors.

        Only retryable conditions are retried: a 401 or a 404 is repeated to no
        purpose and would just delay a clear failure. The final attempt's error
        is raised with its status attached so callers can react to it.
        """
        last_error: RazorpayError | None = None

        for attempt in range(1, self.retry.max_attempts + 1):
            self.attempts_made += 1
            retry_after: float | None = None
            try:
                response = self._http().get(path, params=params)
            except httpx.HTTPError as exc:
                # Timeouts and connection resets are transient by nature.
                last_error = RazorpayError(
                    f"Razorpay request failed: {exc}", path=path, attempt=attempt
                )
                if attempt >= self.retry.max_attempts:
                    raise last_error from exc
                self._backoff(attempt, None, path, reason=type(exc).__name__)
                continue

            if response.status_code < 400:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RazorpayError(f"unexpected Razorpay response shape for {path}")
                return payload

            # Razorpay returns {"error": {"code", "description"}}; surface the
            # description but never the request auth.
            detail: Any
            try:
                detail = response.json().get("error", {})
            except ValueError:
                detail = response.text[:500]

            last_error = RazorpayError(
                f"Razorpay returned {response.status_code} for {path}",
                status_code=response.status_code,
                detail=detail,
                attempt=attempt,
            )
            if response.status_code not in RETRYABLE_STATUS:
                logger.warning(
                    "razorpay_error",
                    path=path,
                    status=response.status_code,
                    detail=detail,
                )
                raise last_error
            if attempt >= self.retry.max_attempts:
                logger.warning(
                    "razorpay_retries_exhausted",
                    path=path,
                    status=response.status_code,
                    attempts=attempt,
                )
                raise last_error

            header = response.headers.get("Retry-After")
            if header:
                try:
                    retry_after = float(header)
                except ValueError:
                    retry_after = None
            self._backoff(attempt, retry_after, path, reason=str(response.status_code))

        raise last_error or RazorpayError(f"Razorpay request failed for {path}")

    def _backoff(
        self, attempt: int, retry_after: float | None, path: str, *, reason: str
    ) -> None:
        delay = self.retry.delay_for(attempt, retry_after)
        logger.info(
            "razorpay_retry", path=path, attempt=attempt, reason=reason, delay=round(delay, 3)
        )
        self._sleep(delay)

    def _paginate(
        self,
        path: str,
        *,
        count: int = DEFAULT_PAGE_SIZE,
        max_items: int | None = None,
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Walk a Razorpay collection endpoint via skip/count."""
        page = min(count, MAX_PAGE_SIZE)
        items: list[dict[str, Any]] = []
        skip = 0
        while True:
            payload = self._get(path, {**params, "count": page, "skip": skip})
            batch = payload.get("items", [])
            if not batch:
                break
            items.extend(batch)
            if max_items is not None and len(items) >= max_items:
                return items[:max_items]
            if len(batch) < page:
                break
            skip += len(batch)
        return items

    # ------------------------------------------------------------- endpoints

    def health(self) -> dict[str, Any]:
        """Cheap connectivity probe.

        Never raises: the API's health endpoint reports Razorpay status as part
        of overall readiness, and an unconfigured or unreachable Razorpay must
        not take the whole service down - the research half runs without it.
        """
        if not self.settings.configured:
            return {"status": "not_configured", "mode": str(self.settings.mode)}
        try:
            self._get("/payments", {"count": 1})
            return {"status": "ok", "mode": str(self.settings.mode)}
        except RazorpayError as exc:
            return {
                "status": "error",
                "mode": str(self.settings.mode),
                "error": exc.message,
            }

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        return self._get(f"/payments/{payment_id}")

    def list_payments(
        self,
        *,
        from_ts: int | None = None,
        to_ts: int | None = None,
        max_items: int | None = 1000,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if from_ts is not None:
            params["from"] = from_ts
        if to_ts is not None:
            params["to"] = to_ts
        return self._paginate("/payments", max_items=max_items, **params)

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._get(f"/orders/{order_id}")

    def list_orders(
        self,
        *,
        from_ts: int | None = None,
        to_ts: int | None = None,
        max_items: int | None = 1000,
    ) -> list[dict[str, Any]]:
        """Orders in a time window.

        An order is the *intent* to be paid; a payment is the cash actually
        moving. The importer reads both because an order with no captured
        payment is exactly the "expected inflow that never arrived" this system
        models as a shock.
        """
        params: dict[str, Any] = {}
        if from_ts is not None:
            params["from"] = from_ts
        if to_ts is not None:
            params["to"] = to_ts
        return self._paginate("/orders", max_items=max_items, **params)

    def list_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        """Payments captured against one order."""
        payload = self._get(f"/orders/{order_id}/payments")
        items = payload.get("items", [])
        return items if isinstance(items, list) else []

    def list_settlements(self, max_items: int | None = 500) -> list[dict[str, Any]]:
        return self._paginate("/settlements", max_items=max_items)

    def list_payouts(
        self, account_number: str, max_items: int | None = 500
    ) -> list[dict[str, Any]]:
        return self._paginate(
            "/payouts", max_items=max_items, account_number=account_number
        )
