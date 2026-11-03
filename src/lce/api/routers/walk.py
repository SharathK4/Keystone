"""The walk: fire the failing payment, ask a what-if, read the cost of waiting.

``GET /walk`` says which merchant the walk follows, what its missing payment is
worth, and where the event comes from (a recorded PayPal delivery, a locally
signed development event, or nothing yet). ``POST /walk/fire`` runs that event
through the real verifier, state machine and bounded analysis. ``POST
/walk/ask`` turns a question into a validated request without computing
anything. ``GET /walk/speed`` reads the walk's offline cost-of-waiting artifact.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from lce.config import (
    REPO_ROOT,
    Environment,
    get_demo_settings,
    get_paypal_settings,
    get_settings,
)
from lce.errors import RateLimited, ServiceUnavailable, ValidationError
from lce.execution.demo import client_key, limiter
from lce.execution.models import MoneyView
from lce.execution.service import NO_DATABASE
from lce.paypal.money import DemoScale
from lce.snapshot.models import ScenarioSnapshot
from lce.snapshot.store import SnapshotStore, require_store
from lce.walk.ask import ParseContext, RuleParser, ShockRequest
from lce.walk.fire import FireOutcome, fire_development, fire_recorded
from lce.walk.recording import RecordedEvent, load_recorded
from lce.walk.speed import read_walk_speed
from lce.walk.thread import get_walk_settings, trigger

router = APIRouter(prefix="/walk", tags=["walk"])

Store = Annotated[SnapshotStore, Depends(require_store)]
SPEED_ROOT = REPO_ROOT / "artifacts" / "structure"

Mode = Literal["recorded", "development", "unavailable"]


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WalkRecordedView(_View):
    source: str
    recorded_at: str
    event_type: str


class WalkView(_View):
    snapshot_id: str
    merchant_id: str
    magnitude_multiple: float
    slack_inr: float
    missing_inr: float
    executed: MoneyView
    mode: Mode
    mode_reason: str | None
    recorded: WalkRecordedView | None


class WalkEventView(_View):
    source: str
    stamp: str
    event_id: str
    event_type: str
    outcome: str
    transmission_time: str | None
    processed_at: str
    verification: str
    ingest_status: str
    record_status: str | None


class WalkShockView(_View):
    merchant_id: str
    trigger: str
    missing_inr: float
    slack_inr: float
    magnitude_multiple: float
    onset_hours: float


class WalkReentryView(_View):
    status: str
    reason: str | None
    analysis_id: str | None


class WalkFireView(_View):
    event: WalkEventView
    shock: WalkShockView
    analysis: ScenarioSnapshot
    computed_in_ms: float
    reentry: WalkReentryView


class WalkAskIn(_View):
    text: str = Field(min_length=1, max_length=280)


class WalkAskView(_View):
    ok: bool
    request: ShockRequest | None
    refusal: str | None
    provider: str
    model: str | None
    changed: list[str]


class WalkSpeedAction(_View):
    action_type: str
    merchant_id: str
    amount_inr: float
    lands_at_hours: float


class WalkSpeedArm(_View):
    disruption_cut_pct: float
    payments_kept_on_time_inr: float
    merchants_protected: int
    cost_inr: float
    actions: list[WalkSpeedAction]


class WalkSpeedRow(_View):
    delay_hours: float
    as_chosen: WalkSpeedArm
    replanned: WalkSpeedArm


class WalkSpeedView(_View):
    label: str
    content_hash: str
    provenance: dict[str, Any]
    shock: dict[str, Any]
    unplanned: dict[str, Any]
    delays: list[WalkSpeedRow]


# ------------------------------------------------------------------ helpers


def _scale() -> DemoScale:
    p = get_paypal_settings()
    return DemoScale(rate=p.demo_scale, currency=p.demo_currency)


def _mode() -> tuple[Mode, str | None, RecordedEvent | None]:
    try:
        recorded = load_recorded()
    except ValidationError as exc:
        return "unavailable", exc.message, None
    if recorded is not None:
        return "recorded", None, recorded
    if get_settings().env in (Environment.LOCAL, Environment.TEST) and \
            get_demo_settings().mode == "off":
        return "development", None, None
    return "unavailable", "No PayPal event has been recorded for this walk yet.", None


def _session() -> Iterator[Session]:
    from lce.data.database import get_session_factory

    try:
        session = get_session_factory()()
    except Exception as exc:
        raise ServiceUnavailable(NO_DATABASE, error=type(exc).__name__) from None
    try:
        yield session
    finally:
        session.close()


DB = Annotated[Session, Depends(_session)]


def _fire_view(o: FireOutcome) -> WalkFireView:
    return WalkFireView(
        event=WalkEventView(
            source=o.source, stamp=o.stamp, event_id=o.event_id, event_type=o.event_type,
            outcome=o.outcome, transmission_time=o.transmission_time,
            processed_at=o.processed_at, verification=o.verification,
            ingest_status=o.ingest_status, record_status=o.record_status,
        ),
        shock=WalkShockView(
            merchant_id=o.shock.merchant_id, trigger=o.shock.trigger,
            missing_inr=o.shock.missing_inr, slack_inr=o.shock.slack_inr,
            magnitude_multiple=o.shock.magnitude_multiple, onset_hours=o.shock.onset_hours,
        ),
        analysis=o.analysis,
        computed_in_ms=o.computed_in_ms,
        reentry=WalkReentryView(status=o.reentry_status, reason=o.reentry_reason,
                                analysis_id=o.reentry_id),
    )


# ------------------------------------------------------------------- routes


@router.get("", response_model=WalkView, summary="The merchant the walk follows, and its event")
def walk(store: Store) -> WalkView:
    settings = get_walk_settings()
    trig = trigger(store, settings, _scale())
    mode, reason, recorded = _mode()
    return WalkView(
        snapshot_id=store.snapshot_id,
        merchant_id=trig.merchant_id,
        magnitude_multiple=trig.multiple,
        slack_inr=trig.slack_inr,
        missing_inr=trig.missing_inr,
        executed=MoneyView(**trig.executed.to_wire()),
        mode=mode,
        mode_reason=reason,
        recorded=(
            WalkRecordedView(source=recorded.source, recorded_at=recorded.recorded_at,
                             event_type=str(recorded.event.get("event_type", "")))
            if recorded else None
        ),
    )


@router.post("/fire", response_model=WalkFireView,
             summary="Process the walk's failed payout through the real pipeline")
def fire(request: Request, store: Store, session: DB) -> WalkFireView:
    demo = get_demo_settings()
    if demo.mode != "off":
        wait = limiter.allow("walk:" + client_key(request), demo.rate_limit, demo.rate_window_s)
        if wait is not None:
            raise RateLimited("Too many events fired from this address; try again later.",
                              retry_after_s=round(wait))
    mode, reason, recorded = _mode()
    try:
        if mode == "recorded" and recorded is not None:
            p = get_paypal_settings()
            wid = p.webhook_id.get_secret_value() or None
            return _fire_view(fire_recorded(session, store, recorded, webhook_id=wid))
        if mode == "development":
            return _fire_view(fire_development(session, store, scale=_scale(),
                                               walk=get_walk_settings()))
    except (OperationalError, ProgrammingError) as exc:
        raise ServiceUnavailable(NO_DATABASE, error=type(exc.orig).__name__) from None
    raise ServiceUnavailable(reason or "the walk has no event to fire", mode=mode)


@router.post("/ask", response_model=WalkAskView,
             summary="Turn a what-if into a validated request (computes nothing)")
def ask(body: WalkAskIn, store: Store) -> WalkAskView:
    settings = get_walk_settings()
    base = ShockRequest(merchant_ids=[settings.merchant],
                        magnitude_multiple=settings.multiple, onset_hours=0.0)

    def exists(merchant_id: str) -> bool:
        try:
            store.merchant(merchant_id)
        except Exception:
            return False
        return True

    parser = RuleParser()
    result = parser.parse(body.text, base=base,
                          ctx=ParseContext(horizon_hours=store.horizon_hours,
                                           merchant_exists=exists))
    return WalkAskView(ok=result.request is not None, request=result.request,
                       refusal=result.refusal, provider=result.provider, model=result.model,
                       changed=list(result.changed))


@router.get("/speed", response_model=WalkSpeedView,
            summary="The walk's cost of waiting, replayed offline")
def speed(store: Store) -> WalkSpeedView:
    settings = get_walk_settings()
    data = read_walk_speed(SPEED_ROOT, store.snapshot_id, [settings.merchant],
                           settings.multiple, 0.0)
    return WalkSpeedView.model_validate({k: v for k, v in data.items() if k != "kind"})
