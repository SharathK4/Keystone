"""One lab session: a population of rules, their lineage, and the actions on it.

Every action streams events, so the page can draw the work as it happens:

``start``   Keystone's search solves each test scenario (the reference line),
            then the rules people wrote, and the visitor's own, are tested.
``evolve``  the model reads every rule's score on every scenario and writes new
            rules aimed at the weak spots; each is checked, then replayed on
            every scenario. ``breed`` asks it to combine two chosen rules and
            ``push`` to improve one.
``add``     the visitor writes a rule and it competes like any other.
``test``    chosen rules, and the search, on failures no rule was tested on.
``use``     a rule decides one rescue; the plan goes to the PayPal approval.

Event shapes (``type`` first) are documented on the router.
"""

from __future__ import annotations

import threading
import time
import uuid
import zlib
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from lce.config import AgentSettings
from lce.discovery import propose
from lce.discovery.formula import Formula, FormulaError, parse
from lce.discovery.world import (
    BUDGETS_INR,
    CLUE_NAMES,
    CLUES,
    DEFAULT_K,
    HUMAN_RULES,
    Cell,
    SearchCell,
    World,
)
from lce.errors import ValidationError
from lce.seeds import config_hash

PER_ROUND = 6
MAX_RULES = 120
MAX_LABS = 24
MODEL_COOLDOWN_S = 600.0

Event = dict[str, Any]


@dataclass(slots=True)
class Rule:
    id: str
    formula: Formula
    idea: str
    origin: str  # "people", "you", "model", "mutation"
    author: str
    generation: int
    parents: list[str]
    cells: list[Cell] = field(default_factory=list)
    ms: float = 0.0
    test: dict[str, Any] | None = None

    @property
    def mean(self) -> float:
        return sum(c.pct for c in self.cells) / len(self.cells) if self.cells else 0.0

    def signature(self) -> tuple[Any, ...]:
        return tuple(tuple(m for m, _ in c.picks) for c in self.cells)

    def view(self, search: dict[str, SearchCell] | None = None) -> Event:
        wins = (sum(1 for c in self.cells if search and c.key in search
                    and c.pct > search[c.key].pct + 0.05) if search else 0)
        return {
            "id": self.id, "formula": self.formula.text, "tree": self.formula.tree(),
            "size": self.formula.size, "clues": list(self.formula.clues), "idea": self.idea,
            "origin": self.origin, "author": self.author, "generation": self.generation,
            "parents": self.parents, "mean": round(self.mean, 3), "wins": wins,
            "ms": self.ms,
            "cells": [{"key": c.key, "pct": c.pct, "kept": c.kept,
                       "picks": [[m, a] for m, a in c.picks]} for c in self.cells],
            "test": self.test,
        }


_model_down_until = 0.0
_model_down_reason = ""


def model_status(settings: AgentSettings) -> tuple[str, str | None, str | None]:
    """(mode, model, reason) - "model" when a language model writes the rules."""
    if not settings.configured:
        return "mutation", None, "no language model key on this server"
    if time.monotonic() < _model_down_until:
        return "mutation", settings.model, _model_down_reason
    return "model", settings.model, None


def _model_failed(reason: str) -> None:
    global _model_down_until, _model_down_reason
    _model_down_until = time.monotonic() + MODEL_COOLDOWN_S
    _model_down_reason = reason


@dataclass
class Lab:
    world: World
    settings: AgentSettings
    training: list[str]
    budgets: list[float]
    k: int
    banned: set[str]
    id: str = field(default_factory=lambda: "lab-" + uuid.uuid4().hex[:10])
    rules: dict[str, Rule] = field(default_factory=dict)
    refused: list[Event] = field(default_factory=list)
    generation: int = 0
    search: dict[str, SearchCell] = field(default_factory=dict)
    model_calls: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    _seq: int = 0

    # --------------------------------------------------------------- basics

    @property
    def allowed(self) -> frozenset[str]:
        return CLUE_NAMES - self.banned

    def scenarios(self, failures: list[str] | None = None) -> list[Any]:
        return [self.world.scenario(f, b) for f in (failures or self.training)
                for b in self.budgets]

    def held_out(self) -> list[str]:
        return self.world.held_out(self.training)

    def ranked(self) -> list[Rule]:
        return sorted(self.rules.values(), key=lambda r: (-r.mean, r.formula.size, r.id))

    def best(self) -> Rule | None:
        ranked = self.ranked()
        return ranked[0] if ranked else None

    def search_mean(self) -> float:
        cells = [self.search[s.key].pct for s in self.scenarios() if s.key in self.search]
        return sum(cells) / len(cells) if cells else 0.0

    def _next_id(self) -> str:
        self._seq += 1
        return f"r{self._seq}"

    def header(self) -> Event:
        mode, model, reason = model_status(self.settings)
        return {
            "type": "lab", "lab_id": self.id, "training": self.training,
            "held_out": self.held_out(), "budgets": self.budgets, "k": self.k,
            "banned": sorted(self.banned),
            "scenarios": [{"key": s.key, "failing": s.failing, "budget": s.budget,
                           "missed": round(s.shock.total_magnitude, 2),
                           "candidates": len(s.candidates)} for s in self.scenarios()],
            "writer": {"mode": mode, "model": model, "reason": reason},
        }

    # ------------------------------------------------------------- scoring

    def _score(self, rule: Rule) -> Iterator[Event]:
        yield {"type": "testing", "rule": rule.view()}
        started = time.perf_counter()
        for scenario in self.scenarios():
            cell = self.world.cell(rule.formula, scenario, self.k)
            rule.cells.append(cell)
            yield {"type": "cell", "rule_id": rule.id, "key": cell.key, "pct": cell.pct,
                   "picks": [[m, a] for m, a in cell.picks], "cached": cell.cached}
        rule.ms = round((time.perf_counter() - started) * 1000.0, 1)
        self.rules[rule.id] = rule
        self._post(rule)
        yield {"type": "scored", "rule": rule.view(self.search)}

    def is_default(self) -> bool:
        """The landing page's test: default failures, every budget, k=3, no bans."""
        return (self.training == self.world.default_training()
                and self.budgets == sorted(BUDGETS_INR) and self.k == DEFAULT_K
                and not self.banned)

    def _post(self, rule: Rule) -> None:
        """Record the rule on the world's board when this lab runs the default test."""
        if not self.is_default():
            return
        board = dict(self.world.board or {})
        board["search_mean"] = round(self.search_mean(), 3)
        entry = {"formula": rule.formula.text, "idea": rule.idea, "mean": round(rule.mean, 3),
                 "author": rule.author, "origin": rule.origin}
        key = {"people": "people_best", "model": "best"}.get(rule.origin)
        if key and (board.get(key) is None or rule.mean > board[key]["mean"]):
            board[key] = entry
        board.setdefault("best", None)
        board.setdefault("people_best", None)
        self.world.board = board

    def _admit(self, text: str, idea: str, origin: str, author: str, parents: list[str],
               seen: set[str]) -> tuple[Rule | None, Event | None]:
        try:
            formula = parse(text, self.allowed)
        except FormulaError as exc:
            banned = [c for c in self.banned if c in (text or "")]
            reason = (f"uses {banned[0]}, which you banned" if banned
                      and "is not one of the clues" in exc.message else exc.message)
            return None, {"formula": text, "reason": reason, "author": author}
        if set(formula.clues) <= {"budget", "failing"}:
            return None, {"formula": formula.text, "author": author,
                          "reason": "reads only clues that barely tell merchants apart"}
        if formula.text in seen:
            return None, {"formula": formula.text, "reason": "already tried", "author": author}
        seen.add(formula.text)
        rule = Rule(id=self._next_id(), formula=formula, idea=idea or "", origin=origin,
                    author=author, generation=self.generation,
                    parents=[p for p in parents if p in self.rules])
        return rule, None

    def _generation_summary(self, started: float, replays_before: int) -> Event:
        best = self.best()
        return {
            "type": "generation", "n": self.generation,
            "best": best.view(self.search) if best else None,
            "search_mean": round(self.search_mean(), 3),
            "replays": self.world.replays - replays_before,
            "ms": round((time.perf_counter() - started) * 1000.0, 1),
            "rules": len(self.rules),
        }

    # -------------------------------------------------------------- actions

    def start(self, your_rule: str | None) -> Iterator[Event]:
        started, before = time.perf_counter(), self.world.replays
        yield self.header()
        yield {"type": "phase", "text": "Keystone's search solves every test scenario"}
        for scenario in self.scenarios():
            cell = self.world.search(scenario, self.k)
            self.search[scenario.key] = cell
            yield {"type": "search", "key": cell.key, "pct": cell.pct, "ms": cell.ms,
                   "picks": [[m, a] for m, a in cell.picks]}
        yield {"type": "search_done", "mean": round(self.search_mean(), 3)}
        yield {"type": "phase", "text": "Testing the rules people would write"}
        seen: set[str] = set()
        for text, idea in HUMAN_RULES:
            rule, refusal = self._admit(text, idea, "people", "people", [], seen)
            if rule is not None:
                yield from self._score(rule)
            elif refusal and refusal["reason"] != "already tried":
                yield {"type": "skipped", "formula": text, "reason": refusal["reason"]}
        if your_rule and your_rule.strip():
            rule, refusal = self._admit(your_rule, "Your rule.", "you", "you", [], seen)
            if rule is not None:
                yield from self._score(rule)
            elif refusal:
                yield {"type": "refused", "generation": 0, "items": [refusal]}
        yield self._generation_summary(started, before)

    def add(self, text: str, idea: str | None) -> Iterator[Event]:
        started, before = time.perf_counter(), self.world.replays
        seen = {r.formula.text for r in self.rules.values()}
        rule, refusal = self._admit(text, idea or "Your rule.", "you", "you", [], seen)
        if refusal:
            yield {"type": "refused", "generation": self.generation, "items": [refusal]}
            return
        assert rule is not None
        yield from self._score(rule)
        yield self._generation_summary(started, before)

    def evolve(self, mode: str, parent_ids: list[str], deep: bool = False) -> Iterator[Event]:
        if len(self.rules) >= MAX_RULES:
            yield {"type": "notice", "text": f"This lab holds {MAX_RULES} rules; start a new one."}
            return
        started, before = time.perf_counter(), self.world.replays
        parents = self._parents(mode, parent_ids)
        self.generation += 1
        writer, model, reason = model_status(self.settings)
        yield {"type": "thinking", "generation": self.generation, "mode": mode,
               "writer": writer, "model": model, "parents": [p.id for p in parents],
               "reason": reason, "deep": deep and writer == "model"}
        seen = {r.formula.text for r in self.rules.values()}
        batch: propose.Batch | None = None
        if writer == "model":
            try:
                self.model_calls += 1
                batch = propose.ask_model(self.settings, self.prompt(mode, parents),
                                          PER_ROUND, deep=deep)
            except propose.ModelFailed as exc:
                _model_failed(exc.reason)
                yield {"type": "notice",
                       "text": f"The model could not write this round ({exc.reason}); "
                               "random mutation writes it instead."}
        if batch is None:
            batch = propose.mutate([(p.id, p.formula.text) for p in parents], self.allowed,
                                   PER_ROUND, set(seen),
                                   seed=zlib.crc32(f"{self.id}:{self.generation}".encode()))
        origin = "mutation" if batch.author == "random mutation" else "model"
        admitted: list[Rule] = []
        refused: list[Event] = []
        for p in batch.proposals:
            if len(admitted) >= PER_ROUND:
                break
            rule, refusal = self._admit(p.formula, p.idea, origin, batch.author,
                                        p.builds_on or [x.id for x in parents[:1]], seen)
            if rule is not None:
                if not rule.parents:
                    rule.parents = [x.id for x in parents[:1]]
                admitted.append(rule)
            elif refusal:
                refused.append(refusal)
        self.refused.extend({**r, "generation": self.generation} for r in refused)
        yield {"type": "proposed", "generation": self.generation, "author": batch.author,
               "ms": batch.ms, "reasoning": batch.reasoning,
               "rules": [{"id": r.id, "formula": r.formula.text, "idea": r.idea,
                          "parents": r.parents} for r in admitted],
               "refused": refused}
        for rule in admitted:
            yield from self._score(rule)
        yield self._generation_summary(started, before)

    def _parents(self, mode: str, parent_ids: list[str]) -> list[Rule]:
        chosen = [self.rules[i] for i in parent_ids if i in self.rules]
        if mode == "breed":
            if len(chosen) != 2:
                raise ValidationError("choose exactly two rules to breed")
            return chosen
        if mode == "push":
            if len(chosen) != 1:
                raise ValidationError("choose one rule to push further")
            return chosen
        out: list[Rule] = []
        signatures: set[tuple[Any, ...]] = set()
        for rule in self.ranked():  # the best distinct behaviours, not near-copies
            if rule.signature() in signatures:
                continue
            signatures.add(rule.signature())
            out.append(rule)
            if len(out) == 4:
                break
        return out

    def test(self, rule_ids: list[str]) -> Iterator[Event]:
        failures = self.held_out()
        scenarios = self.scenarios(failures)
        chosen = [self.rules[i] for i in rule_ids if i in self.rules][:4]
        if not chosen:
            raise ValidationError("choose at least one rule to test")
        yield {"type": "test_start", "failures": failures,
               "scenarios": [{"key": s.key, "failing": s.failing, "budget": s.budget,
                              "missed": round(s.shock.total_magnitude, 2)} for s in scenarios],
               "rules": [r.id for r in chosen]}
        search_cells = []
        for scenario in scenarios:
            cell = self.world.search(scenario, self.k)
            search_cells.append(cell)
            yield {"type": "test_search", "key": cell.key, "pct": cell.pct, "ms": cell.ms}
        search_mean = sum(c.pct for c in search_cells) / len(search_cells)
        search_ms = sum(c.ms for c in search_cells) / len(search_cells)
        rows = []
        for rule in chosen:
            cells = []
            decide = 0.0
            for scenario in scenarios:
                t = time.perf_counter()
                picks = self.world.plan(rule.formula, scenario, self.k)
                decide += (time.perf_counter() - t) * 1000.0
                outcome, _ = self.world.outcome(scenario, picks)
                cells.append({"key": scenario.key, "pct": outcome.pct,
                              "picks": [[m, a] for m, a in picks]})
                yield {"type": "test_cell", "rule_id": rule.id, "key": scenario.key,
                       "pct": outcome.pct}
            mean = sum(c["pct"] for c in cells) / len(cells)
            rule.test = {"mean": round(mean, 3), "search_mean": round(search_mean, 3),
                         "decide_ms": round(decide / len(scenarios), 4),
                         "search_ms": round(search_ms, 1),
                         "wins": sum(1 for c, s in zip(cells, search_cells, strict=True)
                                     if c["pct"] > s.pct + 0.05),
                         "n": len(cells), "cells": cells}
            rows.append({"id": rule.id, **rule.test})
        yield {"type": "tested", "failures": failures, "search_mean": round(search_mean, 3),
               "search_ms": round(search_ms, 1), "rows": rows}

    def use(self, rule_id: str, failing: str, budget: float) -> Event:
        from lce.agent import plans

        rule = self.rules.get(rule_id)
        if rule is None:
            raise ValidationError("unknown rule")
        self.world.store.merchant(failing)
        if budget not in BUDGETS_INR:
            raise ValidationError("choose one of the lab's budgets")
        scenario = self.world.scenario(failing, budget)
        t = time.perf_counter()
        picks = self.world.plan(rule.formula, scenario, self.k)
        decide_ms = (time.perf_counter() - t) * 1000.0
        if not picks:
            raise ValidationError("this rule funds nobody in that scenario")
        outcome, _ = self.world.outcome(scenario, picks)
        search = self.world.search(scenario, self.k)
        chosen = self.world.interventions(picks)
        plan_ref = "rule-" + config_hash(
            {"snapshot": self.world.store.snapshot_id, "formula": rule.formula.text,
             "failing": failing, "budget": budget, "k": self.k}, length=12)
        plans.register(plans.AgentPlan(
            plan_ref=plan_ref, merchant_ids=(failing,), magnitude_multiple=2.0,
            onset_hours=0.0, analysis=None, actions=plans.actions_from(chosen),
            computed_in_ms=round(decide_ms, 4), interventions=tuple(chosen),
            cost_inr=float(sum(u.cost for u in chosen))))
        return {
            "plan_ref": plan_ref, "rule_id": rule.id, "formula": rule.formula.text,
            "failing": failing, "budget": budget,
            "missed": round(scenario.shock.total_magnitude, 2),
            "picks": [{"merchant_id": m, "amount": a} for m, a in picks],
            "pct": outcome.pct, "kept": outcome.kept, "decide_ms": round(decide_ms, 4),
            "search": {"pct": search.pct, "ms": search.ms,
                       "picks": [{"merchant_id": m, "amount": a} for m, a in search.picks]},
        }

    # --------------------------------------------------------------- prompt

    def prompt(self, mode: str, parents: list[Rule]) -> str:
        scenarios = self.scenarios()
        scale = self.world.clue_scale(scenarios)
        fails = list(dict.fromkeys(s.failing for s in scenarios))
        budgets = ", ".join(f"{b:.3g}" for b in self.budgets)

        def grid(cells: dict[str, float]) -> str:
            return " | ".join(
                f"{f}: " + " ".join(f"{cells.get(World.key(f, b), 0.0):.1f}"
                                    for b in self.budgets) for f in fails)

        clue_lines = "\n".join(
            f"- {c.name}: {c.words} (median {scale[c.name][0]:.3g}, max {scale[c.name][1]:.3g})"
            for c in CLUES if c.name in self.allowed)
        banned = (f"\nYou may NOT use: {', '.join(sorted(self.banned))}." if self.banned
                  else "")
        tried = self.ranked()[:12]
        for p in parents:
            if p not in tried:
                tried.append(p)
        tried_lines = "\n".join(
            f"{r.id}  {r.formula.text}  -> mean {r.mean:.2f}% | "
            f"{grid({c.key: c.pct for c in r.cells})}  ({r.idea})" for r in tried)
        refused = "\n".join(f"- {x['formula']}: {x['reason']}" for x in self.refused[-6:])
        best = self.best()
        if mode == "breed" and len(parents) == 2:
            task = (f"Breed {parents[0].id} and {parents[1].id}: write {PER_ROUND} rules that "
                    "keep what each one does well where the other fails. Look at the "
                    "scenarios each one wins.")
        elif mode == "push" and parents:
            task = (f"Push {parents[0].id} further: write {PER_ROUND} variations that keep "
                    "its idea but fix its weakest scenarios.")
        else:
            task = (f"Write {PER_ROUND} new rules that beat the best so far "
                    f"({best.mean:.2f}% by {best.id} if any). " if best else
                    f"Write {PER_ROUND} new rules. ") + (
                "Find where the leading rules lose (which budgets, which failures) and "
                "target that. Mix bold ideas with refinements.")
        return f"""THE PUZZLE
One merchant misses a payment, and a rescue fund of a fixed size is available.
Keystone ranks the merchants who are short of money by your rule (highest score
first) and funds them in that order, each up to its need, until the money runs
out or {self.k} merchants are funded. Then it replays the week over its payment
simulator and measures the share of the week's damage prevented. Higher is
better. One rule must work for every failure and every budget below.

CLUES (one value per merchant; money in rupees){banned}
{clue_lines}

GRAMMAR
numbers, clues, + - * /, x ** c (c a number from -4 to 4), min(a, b), max(a, b),
log(x) meaning log(1+|x|), sqrt(x) meaning sqrt(|x|), abs(x), and one comparison
such as need <= budget (gives 1 or 0). At most 40 symbols. No other names.

TEST SCENARIOS
failures: {", ".join(f"{s} (misses {self.world.shock_size.get(s, 0.0):.3g})" for s in fails)}
budgets: {budgets}
Scores below are % of damage prevented, listed per failure, one number per budget.

KEYSTONE'S SEARCH (same money, whole gaps only): mean {self.search_mean():.2f}%
{grid({k: c.pct for k, c in self.search.items()})}

RULES TRIED (best first)
{tried_lines}
{("REFUSED EARLIER" + chr(10) + refused) if refused else ""}
TASK
{task}
Each rule needs a one-sentence idea in plain English (under 16 words) and the
ids it builds on. Every formula must differ from those above."""


# ------------------------------------------------------------------- store

_labs: OrderedDict[str, Lab] = OrderedDict()
_labs_lock = threading.Lock()


def create(world: World, settings: AgentSettings, *, training: list[str] | None,
           budgets: list[float] | None, k: int | None, banned: list[str] | None) -> Lab:
    training = list(dict.fromkeys(training or world.default_training()))
    if not 1 <= len(training) <= 5:
        raise ValidationError("choose 1 to 5 failures to train on")
    for merchant_id in training:
        world.store.merchant(merchant_id)
    if len(world.held_out(training)) < 1:
        raise ValidationError("leave at least one failure for the final test")
    budgets = sorted(set(budgets or BUDGETS_INR))
    if not budgets or any(b not in BUDGETS_INR for b in budgets):
        raise ValidationError("budgets must come from the lab's list")
    k = DEFAULT_K if k is None else int(k)
    if not 1 <= k <= 4:
        raise ValidationError("fund between 1 and 4 merchants")
    banned_set = {b for b in (banned or []) if b in CLUE_NAMES}
    if len(CLUE_NAMES - banned_set) < 3:
        raise ValidationError("leave at least three clues")
    lab = Lab(world=world, settings=settings, training=training, budgets=budgets, k=k,
              banned=banned_set)
    with _labs_lock:
        _labs[lab.id] = lab
        while len(_labs) > MAX_LABS:
            _labs.popitem(last=False)
    return lab


def get(lab_id: str) -> Lab:
    with _labs_lock:
        lab = _labs.get(lab_id)
    if lab is None:
        raise ValidationError("this lab has expired; start a new one", lab_id=lab_id)
    return lab


def clear() -> None:
    with _labs_lock:
        _labs.clear()
