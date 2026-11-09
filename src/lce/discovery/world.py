"""The lab's world: test scenarios, clues, plans and the replay that judges them.

A *scenario* is one merchant missing a payment (the same 2x shock as every
on-demand analysis) plus a rescue budget. Its *candidates* are the merchants
that would need money that week: their coverage gap (payables due, plus the
missed payment for the merchant that misses it, minus cash) is at least
``MIN_NEED``. That is the yardstick Keystone's search sizes its own cash
injections with.

A rule turns into a plan the same way every time, so the rule is the only thing
being compared: rank the candidates by the rule, fund each in turn up to what it
needs until the money runs out, at most ``k`` merchants. The plan is replayed
over the true simulator; the score is the share of the week's damage it
prevents. Plans that pick the same merchants are replayed once (``_outcomes``).

The reference is Keystone's search at the same money, restricted to the same
kind of action (cash injections): greedy selection over the simulator, which
funds whole gaps only. Both are cached per process.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from lce.discovery.formula import Formula
from lce.domain.enums import InterventionType
from lce.domain.intervention import Intervention
from lce.domain.shock import Shock
from lce.intervention.actions import generate_actions, value_at_risk
from lce.intervention.evaluate import Replay, replay
from lce.intervention.problem import InterventionConstraints, ObjectiveSpec
from lce.intervention.scalable import greedy_solve
from lce.models.propagation import LinearThresholdPropagator, PropagationConfig
from lce.simulation.counterfactual import CounterfactualEvaluator
from lce.simulation.engine import LiquiditySimulator
from lce.snapshot.store import SnapshotStore
from lce.walk.speed import _shock

SHOCK_MULTIPLE = 2.0
MIN_NEED = 1e5  # INR; below this a merchant does not need rescuing
MIN_AMOUNT = 1e5
POOL_SIZE = 10
BUDGETS_INR: tuple[float, ...] = (3e7, 8e7, 15e7, 25e7)
DEFAULT_K = 3
NOT_REACHED = 9.0


@dataclass(frozen=True, slots=True)
class Clue:
    """One thing a rule may read about a merchant, in plain words."""

    name: str
    words: str
    group: str  # "merchant" or "this failure"
    money: bool = False


CLUES: tuple[Clue, ...] = (
    Clue("need", "money that would make it whole this week (what it owes, plus any missed "
                 "payment, minus its cash)", "merchant", money=True),
    Clue("owes", "payments it must make this week", "merchant", money=True),
    Clue("owed", "payments due to it this week", "merchant", money=True),
    Clue("cash", "its cash cushion", "merchant", money=True),
    Clue("credit", "its credit line", "merchant", money=True),
    Clue("passes_on", "share of the money it receives that it passes on, learned from "
                      "payments (0 to 1)", "merchant"),
    Clue("pays", "how many merchants it pays", "merchant"),
    Clue("paid_by", "how many merchants pay it", "merchant"),
    Clue("importance", "Keystone's learned importance, 0 to 1", "merchant"),
    Clue("budget", "the money in the rescue fund (same for every merchant)", "this failure",
         money=True),
    Clue("failing", "1 for the merchant that missed the payment, otherwise 0", "this failure"),
    Clue("hit", "extra payments it makes late because of this failure", "this failure",
         money=True),
    Clue("steps", "steps from the failing merchant (0 is itself, 9 is not reached)",
         "this failure"),
    Clue("exposure", "what the failing merchant owes it this week", "this failure", money=True),
)
CLUE_NAMES: frozenset[str] = frozenset(c.name for c in CLUES)

#: The rules a person would write, scored first so the model has a bar to beat.
HUMAN_RULES: tuple[tuple[str, str], ...] = (
    ("need", "Fund the biggest gap first."),
    ("importance", "Fund the most important merchant first."),
    ("need * (need <= budget)", "Fund whoever the money can fully fix, biggest first."),
    ("importance / need", "Most importance per dollar."),
    ("hit", "Fund whoever this failure hurts most."),
    ("owes + owed", "Fund the biggest business."),
)


@dataclass(slots=True)
class Scenario:
    key: str
    failing: str
    budget: float
    shock: Shock
    shocked: Replay
    candidates: list[str]
    clues: dict[str, np.ndarray]
    need: np.ndarray


@dataclass(frozen=True, slots=True)
class Outcome:
    pct: float
    kept: float  # INR of payments kept on time


@dataclass(frozen=True, slots=True)
class Cell:
    """One rule on one scenario."""

    key: str
    pct: float
    kept: float
    picks: tuple[tuple[str, float], ...]
    cached: bool


@dataclass(frozen=True, slots=True)
class SearchCell:
    key: str
    pct: float
    kept: float
    picks: tuple[tuple[str, float], ...]
    ms: float
    simulations: int


@dataclass(slots=True)
class _Failure:
    shock: Shock
    shocked: Replay
    hit: dict[str, float]
    steps: dict[str, float]
    exposure: dict[str, float]


@dataclass
class World:
    """Everything the lab needs about one snapshot, computed once and cached."""

    store: SnapshotStore
    pool: list[str] = field(default_factory=list)
    shock_size: dict[str, float] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _failures: dict[str, _Failure] = field(default_factory=dict)
    _scenarios: dict[tuple[str, float], Scenario] = field(default_factory=dict)
    _outcomes: dict[tuple[str, tuple[tuple[str, float], ...]], Outcome] = field(
        default_factory=dict)
    _search: dict[tuple[str, int], SearchCell] = field(default_factory=dict)
    _no_shock: Any = None
    replays: int = 0
    #: The standing result on the default test: the best rule people wrote, the
    #: best anyone else wrote, and the search. Shown on the landing page.
    board: dict[str, Any] | None = None
    _var: dict[str, float] = field(default_factory=dict)
    _views: dict[str, Any] = field(default_factory=dict)
    _importance: dict[str, float] = field(default_factory=dict)
    _passes_on: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        graph = self.store.graph
        self._var = {m: value_at_risk(graph, m, self.store.horizon_hours)
                     for m in graph.merchant_ids}
        self._views = {m.merchant_id: m for m in self.store.merchants()}
        self._importance = {e.merchant_id: float(e.importance)
                            for e in self.store.systemic().entries}
        forwarded: dict[str, list[float]] = {}
        for d in self.store.dependencies():
            forwarded.setdefault(d.source_id, []).append(float(d.pass_through))
        self._passes_on = {m: sum(v) / len(v) for m, v in forwarded.items()}
        sizes = {m: _shock(self.store, [m], SHOCK_MULTIPLE, 0.0).total_magnitude
                 for m in graph.merchant_ids}
        ranked = sorted(sizes, key=lambda m: (-sizes[m], m))[:60]
        step = (len(ranked) - 1) / (POOL_SIZE - 1)
        self.pool = list(dict.fromkeys(ranked[round(i * step)] for i in range(POOL_SIZE)))
        self.shock_size = {m: sizes[m] for m in self.pool}

    # ---------------------------------------------------------------- setup

    def default_training(self) -> list[str]:
        return [self.pool[i] for i in (0, 4, 8) if i < len(self.pool)]

    def held_out(self, training: list[str], n: int = 4) -> list[str]:
        rest = [m for m in self.pool if m not in set(training)]
        if len(rest) <= n:
            return rest
        step = (len(rest) - 1) / (n - 1)
        return list(dict.fromkeys(rest[round(i * step)] for i in range(n)))

    @staticmethod
    def key(failing: str, budget: float) -> str:
        return f"{failing}@{budget:.0f}"

    # ------------------------------------------------------------ scenarios

    def _failure(self, failing: str) -> _Failure:
        with self._lock:
            if failing in self._failures:
                return self._failures[failing]
            graph, cfg = self.store.graph, self.store.sim_config
            if self._no_shock is None:
                self._no_shock = LiquiditySimulator(graph, cfg).run(None, run_id="lab:no_shock")
            shock = _shock(self.store, [failing], SHOCK_MULTIPLE, 0.0)
            shocked = replay(graph, shock, [], config=cfg, run_id="lab:shocked")
            self.replays += 1
            base = self._no_shock.outcomes
            hit, steps = {}, {}
            for m, o in shocked.cascade.outcomes.items():
                before = base[m].value_delayed if m in base else 0.0
                hit[m] = max(0.0, o.value_delayed - before)
                steps[m] = float(o.hop_distance) if o.hop_distance is not None else NOT_REACHED
            steps[failing] = 0.0
            exposure: dict[str, float] = {}
            for owed in graph.payables_of(failing):
                if owed.due_t <= self.store.horizon_hours:
                    exposure[owed.creditor_id] = exposure.get(owed.creditor_id, 0.0) + owed.amount
            state = _Failure(shock=shock, shocked=shocked, hit=hit, steps=steps,
                             exposure=exposure)
            self._failures[failing] = state
            return state

    def scenario(self, failing: str, budget: float) -> Scenario:
        with self._lock:
            cached = self._scenarios.get((failing, budget))
            if cached is not None:
                return cached
            f = self._failure(failing)
            graph = self.store.graph
            shocked_by = {c.merchant_id: c.magnitude for c in f.shock.components}
            rows: list[tuple[str, dict[str, float]]] = []
            for m in sorted(graph.merchant_ids):
                cash = graph.merchant(m).initial_buffer
                need = self._var[m] + shocked_by.get(m, 0.0) - cash
                if need < MIN_NEED:
                    continue
                view = self._views[m]
                rows.append((m, {
                    "need": need, "owes": self._var[m],
                    "owed": float(view.receivables_in_horizon), "cash": cash,
                    "credit": float(view.credit_limit),
                    "passes_on": self._passes_on.get(m, 0.0), "pays": float(view.out_degree),
                    "paid_by": float(view.in_degree),
                    "importance": self._importance.get(m, 0.0), "budget": budget,
                    "failing": 1.0 if m == failing else 0.0, "hit": f.hit.get(m, 0.0),
                    "steps": f.steps.get(m, NOT_REACHED),
                    "exposure": f.exposure.get(m, 0.0),
                }))
            clues = {c.name: np.array([r[c.name] for _, r in rows], dtype=float)
                     for c in CLUES}
            built = Scenario(key=self.key(failing, budget), failing=failing, budget=budget,
                             shock=f.shock, shocked=f.shocked,
                             candidates=[m for m, _ in rows], clues=clues,
                             need=clues["need"].copy())
            self._scenarios[(failing, budget)] = built
            return built

    def clue_scale(self, scenarios: list[Scenario]) -> dict[str, tuple[float, float]]:
        """Median and maximum of each clue over these scenarios, for the model."""
        out = {}
        for c in CLUES:
            values: np.ndarray = (np.concatenate([s.clues[c.name] for s in scenarios])
                                  if scenarios else np.zeros(0))
            out[c.name] = ((float(np.median(values)), float(np.max(values)))
                           if len(values) else (0.0, 0.0))
        return out

    # ---------------------------------------------------------------- plans

    def plan(self, formula: Formula, scenario: Scenario, k: int
             ) -> tuple[tuple[str, float], ...]:
        """Rank by the rule; fund each in turn up to its need until the money runs out."""
        scores = formula.score(scenario.clues)
        order = sorted(range(len(scenario.candidates)),
                       key=lambda i: (-scores[i], scenario.candidates[i]))
        left = scenario.budget
        picks: list[tuple[str, float]] = []
        for i in order:
            if len(picks) >= k or left < MIN_AMOUNT:
                break
            if not np.isfinite(scores[i]):
                break
            amount = float(min(scenario.need[i], left))
            if amount < MIN_AMOUNT:
                continue
            picks.append((scenario.candidates[i], round(amount, 2)))
            left -= amount
        return tuple(picks)

    @staticmethod
    def interventions(picks: tuple[tuple[str, float], ...]) -> list[Intervention]:
        return [Intervention(type=InterventionType.LIQUIDITY_INJECTION, merchant_id=m,
                             t=0.0, amount=a) for m, a in picks]

    def outcome(self, scenario: Scenario, picks: tuple[tuple[str, float], ...]
                ) -> tuple[Outcome, bool]:
        key = (scenario.key, picks)
        with self._lock:
            hit = self._outcomes.get(key)
            if hit is not None:
                return hit, True
            shocked = scenario.shocked
            treated = (replay(self.store.graph, scenario.shock, self.interventions(picks),
                              config=self.store.sim_config, run_id="lab:rule")
                       if picks else shocked)
            self.replays += 1 if picks else 0
            result = _outcome(shocked, treated)
            self._outcomes[key] = result
            return result, False

    def cell(self, formula: Formula, scenario: Scenario, k: int) -> Cell:
        picks = self.plan(formula, scenario, k)
        result, cached = self.outcome(scenario, picks)
        return Cell(key=scenario.key, pct=result.pct, kept=result.kept, picks=picks,
                    cached=cached)

    # --------------------------------------------------------------- search

    def search(self, scenario: Scenario, k: int) -> SearchCell:
        """Keystone's search at this budget, cash injections only."""
        with self._lock:
            cached = self._search.get((scenario.key, k))
            if cached is not None:
                return cached
            started = time.perf_counter()
            graph, cfg, horizon = self.store.graph, self.store.sim_config, self.store.horizon_hours
            constraints = InterventionConstraints(max_actions=k, horizon_hours=horizon,
                                                  decision_time=0.0, budget=scenario.budget)
            prediction = LinearThresholdPropagator(
                PropagationConfig(horizon_hours=horizon)).predict(graph, scenario.shock)
            actions = generate_actions(graph, scenario.shock, prediction,
                                       constraints=constraints, max_candidates=12)
            cash = [u for u in actions.interventions
                    if u.type == InterventionType.LIQUIDITY_INJECTION]
            solved = greedy_solve(
                CounterfactualEvaluator(graph=graph, shock=scenario.shock, config=cfg),
                cash, graph, constraints=constraints, objective=ObjectiveSpec())
            chosen = list(solved.interventions)
            picks = tuple((str(u.merchant_id), round(float(u.amount), 2)) for u in chosen)
            treated = (replay(graph, scenario.shock, chosen, config=cfg, run_id="lab:search")
                       if chosen else scenario.shocked)
            result = _outcome(scenario.shocked, treated)
            cell = SearchCell(key=scenario.key, pct=result.pct, kept=result.kept, picks=picks,
                              ms=round((time.perf_counter() - started) * 1000.0, 1),
                              simulations=int(getattr(solved, "simulations", 0) or 0))
            self._search[(scenario.key, k)] = cell
            return cell

    def warm(self, failures: list[str], k: int = DEFAULT_K,
             should_stop: Callable[[], bool] | None = None) -> None:
        """Compute scenarios and search references ahead of the first visitor."""
        for failing in failures:
            for budget in BUDGETS_INR:
                if should_stop is not None and should_stop():
                    return
                self.search(self.scenario(failing, budget), k)


HALL_OF_FAME = "discovery/hall_of_fame.json"


def hall_of_fame_path() -> Any:
    from lce.config import get_settings

    return get_settings().model_artifact_dir / HALL_OF_FAME


def read_hall_of_fame(snapshot_id: str) -> dict[str, Any] | None:
    """The best rule a model has written for this snapshot, as text only.

    Its score is never read from the file: :func:`score_hall_of_fame` replays it.
    """
    import json

    try:
        data = json.loads(hall_of_fame_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    best = data.get("best") if data.get("snapshot_id") == snapshot_id else None
    return best if isinstance(best, dict) and best.get("formula") else None


def write_hall_of_fame(snapshot_id: str, best: dict[str, Any]) -> None:
    import json

    keep = {k: best[k] for k in ("formula", "idea", "author", "origin") if k in best}
    try:
        path = hall_of_fame_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"snapshot_id": snapshot_id, "best": keep}, indent=2)
        path.write_text(text + chr(10), encoding="utf-8")
    except OSError:  # a read-only disk keeps the board in memory only
        pass


def score_hall_of_fame(world: World, k: int = DEFAULT_K) -> None:
    """Replay the remembered best rule on the default test and put it on the board."""
    from lce.discovery.formula import FormulaError, parse

    best = read_hall_of_fame(world.store.snapshot_id)
    if best is None:
        return
    try:
        formula = parse(str(best["formula"]), CLUE_NAMES)
    except FormulaError:
        return
    cells = [world.cell(formula, world.scenario(f, b), k)
             for f in world.default_training() for b in BUDGETS_INR]
    mean = sum(c.pct for c in cells) / len(cells)
    board = dict(world.board or {"people_best": None, "search_mean": None})
    current = board.get("best")
    if current is None or mean > float(current.get("mean", 0.0)):
        board["best"] = {**best, "formula": formula.text, "mean": round(mean, 3)}
    world.board = board


def _outcome(shocked: Replay, treated: Replay) -> Outcome:
    prevented = shocked.disruption - treated.disruption
    pct = 100.0 * prevented / shocked.disruption if shocked.disruption > 0 else 0.0
    return Outcome(pct=round(pct, 3), kept=round(shocked.value_delayed - treated.value_delayed, 2))


_worlds: dict[str, World] = {}
_worlds_lock = threading.Lock()


def get_world(store: SnapshotStore) -> World:
    with _worlds_lock:
        world = _worlds.get(store.snapshot_id)
        if world is None:
            world = World(store=store)
            _worlds.clear()
            _worlds[store.snapshot_id] = world
        return world


def reset_worlds() -> None:
    with _worlds_lock:
        _worlds.clear()
