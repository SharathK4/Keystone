"""Who writes the next rules: a language model, or random mutation.

The model sees the puzzle, the clues with their real scales, the grammar, the
search's score on every scenario and every rule tried so far with its score on
every scenario, and is asked for new rules through one strict tool,
``submit_rules``. It reads where each rule is weak (a budget, a failure) and
writes rules aimed at those weaknesses, each with a one-sentence idea. What it
returns is only text: every formula goes through :func:`formula.parse` before
anything runs, and a refused one comes back with its reason.

Without a model (no key, no credit, rate limited) the same loop runs on random
mutation - swap a clue, wrap in log or min, splice two parents - which is the
classic genetic-programming baseline, and the page says which one wrote what.
"""

from __future__ import annotations

import ast
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from lce.config import AgentSettings
from lce.discovery.formula import FUNCTIONS, FormulaError, parse

TOOL = {
    "name": "submit_rules",
    "description": "Submit the new rules for Keystone to test.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["rules"],
        "properties": {
            "rules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["formula", "idea", "builds_on"],
                    "properties": {
                        "formula": {"type": "string",
                                    "description": "One formula in the grammar."},
                        "idea": {"type": "string",
                                 "description": "Plain English, under 16 words."},
                        "builds_on": {"type": "array", "items": {"type": "string"},
                                      "description": "Ids of the rules it builds on."},
                    },
                },
            }
        },
    },
}

SYSTEM = (
    "You are a research scientist inventing rules for an emergency rescue fund in a "
    "payment network. You think about why a rule wins in one scenario and loses in "
    "another, and you write new rules that fix those weaknesses. Be inventive: combine "
    "clues in ways a person would not think of. Every rule must be one valid formula in "
    "the grammar you are given. Answer only by calling submit_rules."
)


@dataclass(slots=True)
class Proposal:
    formula: str
    idea: str
    builds_on: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Batch:
    proposals: list[Proposal]
    author: str  # model id, or "random mutation"
    ms: float
    reasoning: str = ""  # the model's own reasoning, when it shares it


class ModelFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------- the model


def ask_model(settings: AgentSettings, prompt: str, n: int, *, deep: bool = False) -> Batch:
    """One call to the model; raises :class:`ModelFailed` with a plain reason.

    ``deep`` lets the model reason before it answers (tens of seconds, and its
    reasoning is shown); otherwise reasoning is switched off and a round takes a
    few seconds.
    """
    from lce.agent.loop import _api_reason, make_client

    started = time.perf_counter()
    client = make_client(settings)
    request: dict[str, Any] = {
        "model": settings.model, "max_tokens": 9000, "system": SYSTEM,
        "messages": [{"role": "user", "content": prompt}], "tools": [TOOL],
        "tool_choice": {"type": "tool", "name": "submit_rules"},
    }
    if not deep:
        request["extra_body"] = {"thinking": {"type": "disabled"}}
    elif settings.provider == "openrouter":
        request["extra_body"] = {"reasoning": {"effort": "low"}}
    try:
        try:
            response = client.messages.create(**request)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 400:
                raise
            request.pop("tool_choice")  # some models refuse a forced tool; ask instead
            response = client.messages.create(**request)
    except Exception as exc:
        if hasattr(exc, "status_code"):
            raise ModelFailed(_api_reason(exc)) from exc
        raise ModelFailed("the language model could not be reached") from exc
    proposals = _read(response)[: n + 2]
    if not proposals:
        if getattr(response, "stop_reason", "") == "max_tokens":
            raise ModelFailed("the model ran out of room before answering")
        raise ModelFailed("the model answered without any rules")
    return Batch(proposals=proposals, author=settings.model,
                 ms=round((time.perf_counter() - started) * 1000.0, 1),
                 reasoning=_reasoning(response))


def _reasoning(response: Any) -> str:
    """The model's visible reasoning, trimmed for the page; empty if it shares none."""
    parts = [str(getattr(b, "thinking", "") or "") for b in getattr(response, "content", []) or []
             if getattr(b, "type", "") == "thinking"]
    text = " ".join(" ".join(parts).split())
    return text[:1400] + ("…" if len(text) > 1400 else "")


def _read(response: Any) -> list[Proposal]:
    rules: list[Any] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "tool_use":
            payload = getattr(block, "input", {}) or {}
            rules.extend(payload.get("rules", []) if isinstance(payload, dict) else [])
        elif getattr(block, "type", "") == "text" and not rules:
            rules.extend(_rules_in_text(getattr(block, "text", "")))
    out = []
    for item in rules:
        if not isinstance(item, dict) or not str(item.get("formula", "")).strip():
            continue
        out.append(Proposal(formula=str(item["formula"]).strip(),
                            idea=" ".join(str(item.get("idea", "")).split())[:160],
                            builds_on=[str(x) for x in item.get("builds_on", []) or []][:3]))
    return out


def _rules_in_text(text: str) -> list[Any]:
    match = re.search(r"\{.*\}|\[.*\]", text, flags=re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = data.get("rules", [])
    return data if isinstance(data, list) else []


# ---------------------------------------------------------- random mutation

_WRAPS = ("log({})", "sqrt({})", "min({}, budget)", "({}) * (failing < 1)",
          "({}) / (1 + steps)", "({}) * importance")


def mutate(parents: list[tuple[str, str]], allowed: set[str] | frozenset[str], n: int,
           seen: set[str], seed: int) -> Batch:
    """``n`` new valid rules by mutating and splicing the parents' formulas."""
    started = time.perf_counter()
    rng = random.Random(seed)
    clues = sorted(allowed)
    out: list[Proposal] = []
    attempts = 0
    while len(out) < n and attempts < 400 and parents:
        attempts += 1
        pid, text = rng.choice(parents)
        op = rng.choice(("swap", "wrap", "scale", "splice", "combine"))
        try:
            node = ast.parse(text, mode="eval").body
            names = [x for x in ast.walk(node) if isinstance(x, ast.Name)
                     and x.id not in FUNCTIONS]
            builds = [pid]
            if op == "swap" and names:
                target, new = rng.choice(names), rng.choice(clues)
                idea = f"Mutation: reads {new} where it read {target.id}."
                target.id = new
                candidate = ast.unparse(node)
            elif op == "wrap":
                wrap = rng.choice(_WRAPS)
                candidate = wrap.format(ast.unparse(node))
                idea = f"Mutation: wrapped as {wrap.format('rule')}."
            elif op == "scale":
                clue = rng.choice(clues)
                sign = rng.choice(("*", "/"))
                candidate = f"({ast.unparse(node)}) {sign} (1 + {clue})"
                idea = f"Mutation: {'multiplied' if sign == '*' else 'divided'} by {clue}."
            else:
                others = [q for q in parents if q[0] != pid] or parents
                qid, other = rng.choice(others)
                if other == text:
                    continue
                builds = list(dict.fromkeys([pid, qid]))
                joiner = rng.choice(("+", "*", "min", "max"))
                a, b = ast.unparse(node), other
                candidate = (f"{joiner}({a}, {b})" if joiner in ("min", "max")
                             else f"({a}) {joiner} ({b})")
                idea = f"Splice: {joiner} of two parents."
            checked = parse(candidate, allowed)
        except (FormulaError, SyntaxError, ValueError):
            continue
        if checked.text in seen:
            continue
        seen.add(checked.text)
        out.append(Proposal(formula=checked.text, idea=idea, builds_on=builds))
    return Batch(proposals=out, author="random mutation",
                 ms=round((time.perf_counter() - started) * 1000.0, 1))
