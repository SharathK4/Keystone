"""A what-if in words, turned into a request the bounded analysis accepts.

The analysis takes three things: who is hit, how hard (a multiple of the
merchant's cushion) and when (hours into the week). A question is parsed into
exactly that, validated against the analysis's own limits, and shown to the
reader before anything is computed. Anything else is refused with one line
saying why - including duration: a shortfall here lasts the rest of the week,
so "what if it lasts three days?" asks for a parameter the analysis does not
have, and saying so is better than guessing.

Parsers sit behind :class:`ShockParser`. :class:`RuleParser` needs no key and is
always available; a language-model parser plugs in beside it and is held to the
same schema and the same refusals. Neither ever chooses an amount, an
intervention or an action.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from lce.snapshot.store import MAX_ON_DEMAND_ORIGINS

#: The size range the parser accepts, in multiples of the merchant's cushion.
#: The offline pulls replayed 1.5x to 2.5x; this allows a little either side.
MIN_MULTIPLE = 0.5
MAX_MULTIPLE = 4.0

HELP = "Ask about size (twice as large), timing (48 hours later) or another merchant (m0005 too)."
DURATION = ("A shortfall here lasts the rest of the week, so duration is not a choice; "
            "ask about size, timing or who is hit.")


class ShockRequest(BaseModel):
    """What the bounded analysis needs, and nothing else."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    merchant_ids: list[str] = Field(min_length=1, max_length=MAX_ON_DEMAND_ORIGINS)
    magnitude_multiple: float = Field(ge=MIN_MULTIPLE, le=MAX_MULTIPLE)
    onset_hours: float = Field(ge=0.0)


@dataclass(frozen=True, slots=True)
class ParseContext:
    horizon_hours: float
    merchant_exists: Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class ParseResult:
    request: ShockRequest | None
    refusal: str | None
    provider: str
    model: str | None = None
    #: What the question changed, in field names: "magnitude_multiple", ...
    changed: tuple[str, ...] = field(default=())


class ShockParser(Protocol):
    name: str
    model: str | None

    def parse(self, text: str, *, base: ShockRequest, ctx: ParseContext) -> ParseResult: ...


_WORDS = {"one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0, "six": 6.0,
          "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0, "twelve": 12.0,
          "a": 1.0, "an": 1.0}
_NUM = r"\b(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|twelve|an?)"


def _number(token: str) -> float:
    return _WORDS[token] if token in _WORDS else float(token)


def validate(
    merchant_ids: list[str], multiple: float, onset: float, ctx: ParseContext
) -> tuple[ShockRequest | None, str | None]:
    """The same limits for every parser. Returns (request, None) or (None, reason)."""
    unknown = [m for m in merchant_ids if not ctx.merchant_exists(m)]
    if unknown:
        return None, f"{unknown[0]} is not in this network."
    if len(merchant_ids) > MAX_ON_DEMAND_ORIGINS:
        return None, f"The analysis takes at most {MAX_ON_DEMAND_ORIGINS} merchants at once."
    if not MIN_MULTIPLE <= multiple <= MAX_MULTIPLE:
        return None, (f"That would be {multiple:g}x the merchant's cushion; the analysis "
                      f"covers {MIN_MULTIPLE:g}x to {MAX_MULTIPLE:g}x.")
    if not 0.0 <= onset < ctx.horizon_hours:
        return None, (f"Hour {onset:g} is past the {ctx.horizon_hours:g}-hour week "
                      "the analysis covers.")
    return ShockRequest(merchant_ids=sorted(set(merchant_ids)), magnitude_multiple=multiple,
                        onset_hours=onset), None


class RuleParser:
    """A small deterministic grammar. No key, no network, same limits."""

    name = "rules"
    model: str | None = None

    def parse(self, text: str, *, base: ShockRequest, ctx: ParseContext) -> ParseResult:
        q = " ".join(text.lower().replace("\u00d7", "x").split())
        if not q:
            return ParseResult(None, HELP, self.name)
        if re.search(r"\b(lasts?|lasting|long(er)?|persists?|drags?)\b"
                     r"|\bfor " + _NUM + r" (hours?|days?|weeks?)\b", q):
            return ParseResult(None, DURATION, self.name)

        changed: list[str] = []
        multiple = base.magnitude_multiple
        onset = base.onset_hours
        merchants = list(base.merchant_ids)

        factor: float | None = None
        if re.search(r"\b(twice|double|doubled)\b", q):
            factor = 2.0
        elif re.search(r"\b(triple|tripled|thrice)\b", q):
            factor = 3.0
        elif re.search(r"\b(half|halved)\b", q):
            factor = 0.5
        elif m := re.search(_NUM + r"\s*(x|times)\b", q):
            factor = _number(m.group(1))
        elif re.search(r"\b(bigger|larger|worse|smaller|milder)\b", q):
            return ParseResult(None, "Say how much: twice as large, half, three times.",
                               self.name)
        if factor is not None:
            multiple = round(base.magnitude_multiple * factor, 6)
            changed.append("magnitude_multiple")

        when: float | None = None
        if m := re.search(r"\b(?:at|from|by)\s+hour\s+(\d+(?:\.\d+)?)", q):
            when = float(m.group(1))
        elif m := re.search(_NUM + r"\s*hours?\s*(later|after|from now)", q) or re.search(
                r"\bin\s+" + _NUM + r"\s*hours?\b", q):
            when = base.onset_hours + _number(m.group(1))
        elif m := re.search(_NUM + r"\s*days?\s*(later|after|from now)", q) or re.search(
                r"\bin\s+" + _NUM + r"\s*days?\b", q):
            when = base.onset_hours + 24.0 * _number(m.group(1))
        elif m := re.search(r"\bon day\s+(\d+)", q):
            when = 24.0 * (float(m.group(1)) - 1.0)
        elif re.search(r"\btomorrow\b", q):
            when = base.onset_hours + 24.0
        if when is not None:
            onset = when
            changed.append("onset_hours")

        named = [m for m in re.findall(r"\bm\d{4}\b", q) if m not in merchants]
        if named:
            merchants = named if re.search(r"\binstead\b", q) else merchants + named
            changed.append("merchant_ids")

        if not changed:
            return ParseResult(None, HELP, self.name)
        request, reason = validate(merchants, multiple, onset, ctx)
        if request is None:
            return ParseResult(None, reason, self.name)
        return ParseResult(request, None, self.name, changed=tuple(changed))
