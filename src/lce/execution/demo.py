"""Hosted-demo safety: who may start a sandbox payout, how often, and on what.

Applied to the routes that start or re-check a payout. With
``LCE_DEMO_MODE=off`` it does nothing. With ``recorded`` it refuses unless the
request carries the demo token, limits each address to a few requests per
window, and allows only the canonical plan. Reads are never limited.

The rate limit is per process and in memory: a courtesy limit for token
holders on a single small instance, not a defence against a determined client
(who would need the token first).
"""

from __future__ import annotations

import argparse
import hmac
import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import Request
from sqlalchemy import delete
from sqlalchemy.orm import Session

from lce.config import DemoSettings, get_demo_settings
from lce.errors import DemoRefused, RateLimited

TOKEN_HEADER = "X-Keystone-Demo-Token"
RECORDED_NOTE = (
    "This hosted demo shows a sandbox run recorded earlier. Starting a new one "
    "needs the demo token."
)


class RateLimiter:
    """Sliding window per key. Thread-safe; the clock is injectable for tests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_s: float) -> float | None:
        """``None`` if allowed (and counted); otherwise seconds until it would be."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - window_s:
                hits.popleft()
            if len(hits) >= limit:
                return max(0.0, hits[0] + window_s - now)
            hits.append(now)
            return None

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_key(request: Request) -> str:
    """The caller's address: the first ``X-Forwarded-For`` hop behind a proxy."""
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    return first or (request.client.host if request.client else "unknown")


def check(request: Request, settings: DemoSettings) -> None:
    if settings.mode == "off":
        return
    expected = settings.token.get_secret_value()
    supplied = request.headers.get(TOKEN_HEADER, "")
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise DemoRefused(RECORDED_NOTE, mode=settings.mode)
    scenario_id = request.path_params.get("scenario_id")
    if scenario_id is not None and scenario_id != settings.scenario_id:
        raise DemoRefused("The hosted demo executes only the canonical plan.",
                          scenario_id=scenario_id, allowed=settings.scenario_id)
    wait = limiter.allow(client_key(request), settings.rate_limit, settings.rate_window_s)
    if wait is not None:
        raise RateLimited("Too many execution requests from this address; try again later.",
                          retry_after_s=round(wait), limit=settings.rate_limit,
                          window_s=settings.rate_window_s)


def demo_guard(request: Request) -> None:
    """FastAPI dependency for the routes that start or re-check a payout."""
    check(request, get_demo_settings())


# ------------------------------------------------------------------- reset


def reset(session: Session) -> dict[str, int]:
    """Delete every approval, record, ledger event, webhook event and re-entry.

    Sandbox identities are kept, so the canonical plan can be executed again
    straight away. Returns rows deleted per table.
    """
    from lce.data.orm import (
        ExecutionApprovalRow,
        ExecutionEventRow,
        ExecutionRecordRow,
        PayPalWebhookEventRow,
        ReentryAnalysisRow,
    )

    counts = {}
    for row in (ExecutionEventRow, ReentryAnalysisRow, ExecutionRecordRow,
                ExecutionApprovalRow, PayPalWebhookEventRow):
        result = session.execute(delete(row))
        counts[row.__tablename__] = int(getattr(result, "rowcount", 0) or 0)
    session.commit()
    limiter.clear()
    return counts


def cmd_reset(args: argparse.Namespace) -> int:
    import json

    from lce.data.database import session_scope

    if not args.yes:
        print("Deletes every sandbox approval, payout record, ledger event, webhook "
              "event and re-entry analysis (identities are kept). Re-run with --yes.")
        return 2
    with session_scope() as session:
        counts = reset(session)
    print(json.dumps({"deleted": counts}, indent=2))
    return 0


def register(demo_parser: argparse.ArgumentParser) -> None:
    sub = demo_parser.add_subparsers(dest="demo_command")
    r = sub.add_parser("reset", help="Clear the hosted demo's sandbox run (keeps identities)")
    r.add_argument("--yes", action="store_true", help="Really delete")
    r.set_defaults(func=cmd_reset)
