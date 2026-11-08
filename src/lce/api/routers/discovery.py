"""The rule lab over HTTP.

``GET  /discovery/setup``              the failures, budgets, clues and who writes rules
``POST /discovery/check``              is this text a rule the lab can run? (as you type)
``POST /discovery/labs``               start a lab; streams the search and the first rules
``POST /discovery/labs/{id}/evolve``   one round written by the model (or mutation)
``POST /discovery/labs/{id}/rules``    add your own rule
``POST /discovery/labs/{id}/test``     chosen rules on failures they were never tested on
``POST /discovery/labs/{id}/use``      a rule decides one rescue; returns the PayPal plan

The streams are server-sent events, one JSON object per event, ``type`` first:
``lab``, ``phase``, ``search``, ``search_done``, ``thinking``, ``proposed``,
``refused``, ``testing``, ``cell``, ``scored``, ``generation``, ``test_start``,
``test_search``, ``test_cell``, ``tested``, ``notice``, ``error``, ``done``.
Every number in them comes from a replay of the week that ran for the request
or was cached from an identical one.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from lce.config import get_agent_settings, get_demo_settings
from lce.discovery import lab as labs
from lce.discovery.formula import FormulaError, parse
from lce.discovery.world import (
    BUDGETS_INR,
    CLUE_NAMES,
    CLUES,
    DEFAULT_K,
    HUMAN_RULES,
    get_world,
)
from lce.errors import LCEError, RateLimited
from lce.execution.demo import client_key, limiter
from lce.logging import get_logger
from lce.snapshot.store import SnapshotStore, require_store

logger = get_logger(__name__)

router = APIRouter(prefix="/discovery", tags=["discovery"])

Store = Annotated[SnapshotStore, Depends(require_store)]

HOSTED_WINDOW_S = 600
HOSTED_LIMITS = {"lab": 10, "evolve": 30, "other": 60}


class ClueView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    words: str
    group: str
    money: bool


class PoolMerchant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    merchant_id: str
    missed: float = Field(description="The missed payment in this failure, INR.")
    sector: str
    tier: str
    training: bool = Field(description="In the default training set.")


class WriterView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str = Field(description='"model" when a language model writes rules, else "mutation".')
    model: str | None
    reason: str | None


class SetupView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pool: list[PoolMerchant]
    budgets: list[float]
    clues: list[ClueView]
    people: list[dict[str, str]]
    k: int
    writer: WriterView


class CheckIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formula: str = Field(max_length=400)
    banned: list[str] = Field(default_factory=list, max_length=13)


class CheckOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    formula: str | None = None
    tree: dict[str, Any] | None = None
    size: int | None = None
    clues: list[str] = Field(default_factory=list)
    error: str | None = None


class LabIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    training: list[str] | None = Field(default=None, max_length=5)
    budgets: list[float] | None = Field(default=None, max_length=4)
    k: int | None = Field(default=None, ge=1, le=4)
    banned: list[str] = Field(default_factory=list, max_length=13)
    your_rule: str | None = Field(default=None, max_length=400)


class EvolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["evolve", "breed", "push"] = "evolve"
    parents: list[str] = Field(default_factory=list, max_length=2)
    deep: bool = Field(default=False, description="Let the model reason first (slower).")


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formula: str = Field(min_length=1, max_length=400)
    idea: str | None = Field(default=None, max_length=160)


class TestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_ids: list[str] = Field(min_length=1, max_length=4)


class UseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    failing: str
    budget: float


def _limit(request: Request, kind: str) -> None:
    if get_demo_settings().mode == "off":
        return
    wait = limiter.allow(f"lab:{kind}:" + client_key(request), HOSTED_LIMITS[kind],
                         HOSTED_WINDOW_S)
    if wait is not None:
        raise RateLimited("The lab is busy for this address; try again shortly.",
                          retry_after_s=round(wait))


def _stream(lab: labs.Lab, events: Iterator[dict[str, Any]]) -> StreamingResponse:
    def body() -> Iterator[str]:
        if not lab.lock.acquire(blocking=False):
            yield _sse({"type": "error", "message": "This lab is still working on the last step."})
            return
        try:
            for event in events:
                yield _sse(event)
        except LCEError as exc:
            yield _sse({"type": "error", "message": exc.message})
        except Exception:  # never leave the page waiting on a broken stream
            logger.exception("discovery_stream_failed", lab_id=lab.id)
            yield _sse({"type": "error", "message": "The lab hit an internal error."})
        finally:
            lab.lock.release()
        yield _sse({"type": "done"})

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


@router.get("/setup", response_model=SetupView, summary="What the rule lab can test with")
def setup(store: Store) -> SetupView:
    world = get_world(store)
    training = set(world.default_training())
    mode, model, reason = labs.model_status(get_agent_settings())
    views = {m.merchant_id: m for m in store.merchants()}
    return SetupView(
        pool=[PoolMerchant(merchant_id=m, missed=round(world.shock_size[m], 2),
                           sector=views[m].sector, tier=views[m].tier,
                           training=m in training) for m in world.pool],
        budgets=list(BUDGETS_INR),
        clues=[ClueView(name=c.name, words=c.words, group=c.group, money=c.money)
               for c in CLUES],
        people=[{"formula": f, "idea": i} for f, i in HUMAN_RULES],
        k=DEFAULT_K,
        writer=WriterView(mode=mode, model=model, reason=reason),
    )


@router.post("/check", response_model=CheckOut, summary="Check a rule without running it")
def check(body: CheckIn) -> CheckOut:
    allowed = CLUE_NAMES - set(body.banned)
    try:
        formula = parse(body.formula, allowed)
    except FormulaError as exc:
        return CheckOut(ok=False, error=exc.message)
    return CheckOut(ok=True, formula=formula.text, tree=formula.tree(), size=formula.size,
                    clues=list(formula.clues))


@router.post("/labs", summary="Start a lab; streams the search and the first rules")
def start(body: LabIn, request: Request, store: Store) -> StreamingResponse:
    _limit(request, "lab")
    lab = labs.create(get_world(store), get_agent_settings(), training=body.training,
                      budgets=body.budgets, k=body.k, banned=body.banned)
    return _stream(lab, lab.start(body.your_rule))


@router.post("/labs/{lab_id}/evolve", summary="One round of new rules, tested as they come")
def evolve(lab_id: str, body: EvolveIn, request: Request) -> StreamingResponse:
    _limit(request, "evolve")
    lab = labs.get(lab_id)
    return _stream(lab, lab.evolve(body.mode, body.parents, body.deep))


@router.post("/labs/{lab_id}/rules", summary="Add your own rule to the lab")
def add(lab_id: str, body: RuleIn, request: Request) -> StreamingResponse:
    _limit(request, "other")
    lab = labs.get(lab_id)
    return _stream(lab, lab.add(body.formula, body.idea))


@router.post("/labs/{lab_id}/test", summary="Test rules on failures they never saw")
def test(lab_id: str, body: TestIn, request: Request) -> StreamingResponse:
    _limit(request, "other")
    lab = labs.get(lab_id)
    return _stream(lab, lab.test(body.rule_ids))


@router.post("/labs/{lab_id}/use", summary="Let a rule decide one rescue, ready for PayPal")
def use(lab_id: str, body: UseIn, request: Request) -> dict[str, Any]:
    _limit(request, "other")
    lab = labs.get(lab_id)
    with lab.lock:
        return lab.use(body.rule_id, body.failing.strip().lower(), float(body.budget))


_warming: threading.Thread | None = None


def warm_in_background(store: SnapshotStore) -> None:
    """Solve the default scenarios before the first visitor opens the lab."""
    global _warming
    if _warming is not None and _warming.is_alive():
        return

    def run() -> None:
        try:
            world = get_world(store)
            training = world.default_training()
            world.warm(training + world.held_out(training))
            logger.info("discovery_warm", replays=world.replays)
        except Exception:
            logger.exception("discovery_warm_failed")

    _warming = threading.Thread(target=run, name="discovery-warm", daemon=True)
    _warming.start()
