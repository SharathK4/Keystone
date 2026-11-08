"""The Ask Keystone loop: a question in, a stream of real steps and an answer out.

Events, in order, as plain dicts (the route sends each as one server-sent
event):

* ``start`` - who is answering: Claude (model name) or the rules;
* ``step`` - a tool call, first ``running`` then ``done`` with a one-line
  result and its measured time. Each is real computation;
* ``scenario`` - the analysis the page redraws its cards around;
* ``payment`` - the PayPal payouts a person can approve;
* ``answer`` - two or three sentences, grounded in the tool results;
* ``done`` (or ``error``).

Claude chooses which tools to call and words the answer. It never chooses an
amount or a merchant to fund - the search does - and it cannot pay. If the
model is not configured, refuses, or the API fails, the same tools run from a
rule-based reading of the question and ``start``/``notice`` say so.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterator
from typing import Any

from lce.agent.grounding import ungrounded
from lce.agent.tools import Toolbox, ToolResult, hours_text
from lce.config import AgentSettings
from lce.logging import get_logger

logger = get_logger(__name__)

#: Models that accept the server-side refusal fallback ("default" form).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

SYSTEM = """You are Keystone, an analyst for the risk team of a payment platform such as PayPal. \
The platform sees who pays whom, so it can tell which merchant to fund to stop one missed \
payment from making other merchants miss theirs.

How to work:
- Learn every fact from the tools. Never invent a merchant, a number or a plan.
- For "what if X misses a payment": identify the merchant (call find_merchants when the user \
describes it instead of naming an id like m0038), call simulate_missed_payment, then call \
prepare_paypal_payout with the plan_ref it returns. Call check_delay when the user asks about \
timing, speed or the money arriving late.
- The search chooses which merchants to fund and how much, not you. You never send money; a \
person approves the payout on screen.

How to answer:
- At most three short sentences in plain English for a busy executive. Lead with the decision: \
who to fund, how much, and what it protects.
- Quote figures exactly as the tools print them (for example "$1.94M" or "42%"). Use no other \
numbers; write counts as words.
- Never use the words cascade, simulator, simulation, engine, contagion, node or graph."""


def _step_label(name: str, args: dict[str, Any]) -> str:
    if name == "find_merchants":
        bits = [b for b in (args.get("tier"), args.get("sector")) if b and b != "any"]
        return "Finding the merchant" + (f" ({' '.join(bits)})" if bits else "")
    if name == "simulate_missed_payment":
        ids = ", ".join(args.get("merchant_ids") or [])
        return f"Replaying the week without {ids}'s payment"
    if name == "check_delay":
        late = hours_text(float(args.get("delay_hours") or 0))
        return f"Checking what arriving {late} late costs"
    if name == "prepare_paypal_payout":
        return "Preparing the PayPal payout"
    return name


class _Run:
    """Collects what the tools produced, for grounding and the final answer."""

    def __init__(self, toolbox: Toolbox) -> None:
        self.toolbox = toolbox
        self.sources: list[str] = []
        self.paid_ref: str | None = None
        self.n = 0

    def call(self, name: str, args: dict[str, Any]) -> Iterator[dict[str, Any]]:
        self.n += 1
        sid = f"s{self.n}"
        yield {"type": "step", "id": sid, "tool": name, "label": _step_label(name, args),
               "status": "running"}
        result = self.toolbox.run(name, args)
        self.sources.append(json.dumps(result.data))
        yield {"type": "step", "id": sid, "tool": name, "status": "error" if result.error
               else "done", "summary": result.summary, "ms": result.ms}
        if result.plan is not None:
            yield {"type": "scenario", "plan_ref": result.plan.plan_ref,
                   "scenario": result.plan.analysis.model_dump(mode="json")}
        if result.payment is not None:
            self.paid_ref = result.payment["plan_ref"]
            yield {"type": "payment", **result.payment}
        self._last = result

    @property
    def last(self) -> ToolResult:
        return self._last


def template_answer(toolbox: Toolbox) -> str:
    """Two sentences built only from the last analysis's tool figures."""
    plan = toolbox.last_plan
    data = toolbox.last_summary
    if plan is None or data is None:
        return ("Ask about a merchant missing a payment, for example: what if m0038 misses "
                "a payment this week?")
    others = data["others_who_run_short"]
    who = " and ".join(plan.merchant_ids)
    rescue = " and ".join(f"{r['merchant_id']} ({r['amount']})" for r in data["rescue"])
    if not rescue:
        return (f"If {who} misses this payment, no rescue the search tried reduces the "
                "damage, so there is nothing to fund.")
    reach = {0: "no one else runs short", 1: "one more merchant runs short"}.get(
        len(others), f"{len(others)} more merchants run short")
    return (f"Fund {rescue}. If {who} misses this payment, {reach}; this prevents "
            f"{data['damage_prevented']} of the damage and keeps "
            f"{data['payments_kept_on_time']} of payments on time.")


def _finish(run: _Run, text: str | None, source: str) -> Iterator[dict[str, Any]]:
    toolbox = run.toolbox
    if toolbox.last_plan is not None and run.paid_ref != toolbox.last_plan.plan_ref:
        yield from run.call("prepare_paypal_payout", {"plan_ref": toolbox.last_plan.plan_ref})
    if text and not ungrounded(text, run.sources):
        yield {"type": "answer", "text": text, "source": source, "grounded": True}
        return
    if text:
        logger.warning("agent_answer_ungrounded", bad=ungrounded(text, run.sources))
    # Built only from tool figures, so grounded by construction; "replaced" says
    # a model answer existed and failed the number check.
    yield {"type": "answer", "text": template_answer(toolbox), "grounded": True,
           "source": "rules" if source == "rules" else "template", "replaced": bool(text)}


# ---------------------------------------------------------------- Claude


class ModelUnavailable(Exception):
    """The model could not answer; the rules take over."""


def _claude(question: str, run: _Run, settings: AgentSettings) -> Iterator[dict[str, Any]]:
    import anthropic

    client = make_client(settings)
    tools = run.toolbox.definitions()
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    extra: dict[str, Any] = {}
    if settings.provider == "anthropic" and settings.fallbacks \
            and settings.model in FALLBACK_MODELS:
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

    yield {"type": "step", "id": "s0", "tool": "read", "label": "Reading your question",
           "status": "running"}
    started = time.perf_counter()
    text = ""
    for turn in range(settings.max_turns):
        try:
            request: dict[str, Any] = {
                "model": settings.model, "max_tokens": 4000, "system": SYSTEM, "tools": tools,
                "messages": messages, "output_config": {"effort": settings.effort}, **extra}
            response = client.beta.messages.create(**request)
        except anthropic.APIStatusError as exc:
            raise ModelUnavailable(_api_reason(exc)) from None
        except anthropic.APIConnectionError:
            raise ModelUnavailable("the language model could not be reached") from None
        if turn == 0:
            yield {"type": "step", "id": "s0", "tool": "read", "status": "done",
                   "summary": f"understood by {response.model}",
                   "ms": round((time.perf_counter() - started) * 1000.0, 1)}
        if response.stop_reason == "refusal":
            raise ModelUnavailable("the language model declined this question")
        messages.append({"role": "assistant", "content": response.content})
        uses = [b for b in response.content if b.type == "tool_use"]
        if not uses:
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            break
        results = []
        for use in uses:
            args = dict(use.input) if isinstance(use.input, dict) else {}
            yield from run.call(use.name, args)
            results.append({"type": "tool_result", "tool_use_id": use.id,
                            "content": run.sources[-1], "is_error": run.last.error})
        messages.append({"role": "user", "content": results})
    yield from _finish(run, text, settings.model)


def make_client(settings: AgentSettings) -> Any:
    """The Anthropic SDK client, pointed at the Claude API or at OpenRouter."""
    import anthropic

    from lce.config import OPENROUTER_BASE_URL

    if settings.provider == "openrouter":
        return anthropic.Anthropic(base_url=OPENROUTER_BASE_URL, auth_token=settings.key,
                                   api_key=None, timeout=settings.timeout_s, max_retries=1)
    return anthropic.Anthropic(api_key=settings.key, timeout=settings.timeout_s, max_retries=1)


def _api_reason(exc: Any) -> str:
    message = str(getattr(exc, "message", "") or "")
    if "credit balance" in message or getattr(exc, "status_code", None) == 402:
        return "the model account has no credit"
    if getattr(exc, "status_code", None) == 401:
        return "the Anthropic API key was rejected"
    if getattr(exc, "status_code", None) == 429:
        return "the language model is rate limited"
    return f"the language model returned an error ({getattr(exc, 'status_code', '?')})"


# ----------------------------------------------------------------- rules

_SECTOR_WORDS = {"wholesal": "wholesale", "retail": "retail", "manufactur": "manufacturing",
                 "construct": "construction", "logistic": "logistics", "farm": "agri",
                 "agri": "agri", "service": "services"}
_TIERS = ("anchor", "large", "medium", "small", "micro")
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "a": 1, "an": 1}


def _rules(question: str, run: _Run) -> Iterator[dict[str, Any]]:
    q = question.lower()
    toolbox = run.toolbox
    sectors = {m.sector for m in toolbox.store.merchants()}
    ids = sorted(set(re.findall(r"\bm\d{4}\b", q)))
    if not ids:
        sector = next((s for w, s in _SECTOR_WORDS.items() if w in q and s in sectors), "any")
        tier = next((t for t in _TIERS if re.search(rf"\b{t}\b", q)), "any")
        order = "size" if re.search(r"\b(biggest|largest)\b", q) else (
            "thinnest_cushion" if re.search(r"\b(weakest|thinnest|fragile)\b", q)
            else "importance")
        yield from run.call("find_merchants", {"sector": sector, "tier": tier,
                                               "order_by": order, "limit": 3})
        found = run.last.data.get("merchants") or []
        if not found:
            yield {"type": "answer", "text": "No merchant matches that description.",
                   "source": "rules", "grounded": True}
            return
        ids = [found[0]["merchant_id"]]
    size = 2.0
    if re.search(r"\b(twice|double|doubled)\b", q):
        size = 4.0
    elif re.search(r"\b(half|halved)\b", q):
        size = 1.0
    onset = 0.0
    if m := re.search(r"\bin\s+(\d+|one|two|three|four|five|six)\s+days?\b", q):
        onset = 24.0 * float(_WORDS.get(m.group(1), m.group(1)))
    elif m := re.search(r"\bat hour\s+(\d+)", q):
        onset = float(m.group(1))
    yield from run.call("simulate_missed_payment", {"merchant_ids": ids, "size_multiple": size,
                                                    "starts_at_hour": onset})
    plan = toolbox.last_plan
    if m := re.search(r"(\d+|one|two|three|four)\s+(days?|hours?)\s+late", q):
        n = float(_WORDS.get(m.group(1), m.group(1)))
        if plan is not None:
            yield from run.call("check_delay", {"plan_ref": plan.plan_ref,
                                                "delay_hours": n * (24 if "day" in m.group(2)
                                                                    else 1)})
    yield from _finish(run, None, "rules")


# ------------------------------------------------------------------ entry


def run_agent(question: str, toolbox: Toolbox, settings: AgentSettings) -> Iterator[dict[str, Any]]:
    """Answer one question as a stream of events. Never raises past ``error``."""
    started = time.perf_counter()
    rid = uuid.uuid4().hex[:12]
    logger.info("agent_question", run=rid,
                question_sha256=hashlib.sha256(question.encode()).hexdigest()[:16])
    run = _Run(toolbox)
    try:
        if settings.configured:
            yield {"type": "start", "mode": "claude", "model": settings.model,
                   "provider": settings.provider}
            try:
                yield from _claude(question, run, settings)
            except ModelUnavailable as exc:
                yield {"type": "step", "id": "s0", "tool": "read", "status": "error",
                       "summary": f"Claude unavailable: {exc}"}
                yield {"type": "notice", "message": f"Claude is unavailable: {exc}. The same "
                                                    "tools ran from a rule-based reading."}
                run = _Run(toolbox)
                yield from _rules(question, run)
        else:
            yield {"type": "start", "mode": "rules", "model": None,
                   "reason": "no language model key on this server"}
            yield from _rules(question, run)
    except Exception as exc:  # the stream must end cleanly whatever happened
        logger.error("agent_failed", run=rid, error=type(exc).__name__)
        yield {"type": "error", "message": "Keystone could not finish this question."}
    yield {"type": "done", "ms": round((time.perf_counter() - started) * 1000.0, 1)}
