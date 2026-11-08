"""Ask Keystone over HTTP.

``POST /agent/ask`` streams one answer as server-sent events: each tool call as
it runs and finishes (real computation, with its time), the analysis the page
redraws around, the PayPal payouts awaiting approval, and the grounded answer.
``GET /agent/status`` says who answers on this server: Claude (and which
model) or the rules.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from lce.agent.loop import run_agent
from lce.agent.tools import Toolbox
from lce.config import get_agent_settings, get_demo_settings
from lce.errors import RateLimited
from lce.execution.demo import client_key, limiter
from lce.snapshot.store import SnapshotStore, require_store

router = APIRouter(prefix="/agent", tags=["agent"])

Store = Annotated[SnapshotStore, Depends(require_store)]

#: Questions per address per window on the hosted demo. Each one runs seconds of
#: analysis and, with a key, a few model calls.
HOSTED_LIMIT = 12
HOSTED_WINDOW_S = 600


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=400)


class AgentStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str
    model: str | None
    effort: str | None


def _session_factory() -> Any:
    from lce.data.database import get_session_factory

    try:
        return get_session_factory()
    except Exception:
        return None


@router.get("/status", response_model=AgentStatus, summary="Who answers Ask Keystone here")
def status() -> AgentStatus:
    s = get_agent_settings()
    if s.configured:
        return AgentStatus(mode="claude", model=s.model, effort=s.effort)
    return AgentStatus(mode="rules", model=None, effort=None)


@router.post("/ask", summary="Ask Keystone a question; the answer streams as events")
def ask(body: AskIn, request: Request, store: Store) -> StreamingResponse:
    if get_demo_settings().mode != "off":
        wait = limiter.allow("agent:" + client_key(request), HOSTED_LIMIT, HOSTED_WINDOW_S)
        if wait is not None:
            raise RateLimited("Too many questions from this address; try again shortly.",
                              retry_after_s=round(wait))
    toolbox = Toolbox(store, session_factory=_session_factory())
    events = run_agent(body.question.strip(), toolbox, get_agent_settings())

    def stream() -> Iterator[str]:
        for event in events:
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
