"""The rule lab: safe formulas, fair plans, a model that writes rules, PayPal at the end.

Runs against the committed snapshot, so every score is a real replay of the
week. The model is a stand-in client shaped like the API (a forced
``submit_rules`` tool call); nothing calls a model provider or PayPal.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from lce.agent import plans
from lce.config import AgentSettings, reset_agent_settings_cache
from lce.discovery import lab as labs
from lce.discovery.formula import FormulaError, parse
from lce.discovery.world import CLUE_NAMES, World, get_world
from lce.snapshot.store import SnapshotStore

CANONICAL = Path(__file__).resolve().parents[1] / "artifacts" / "snapshots" / \
    "snap-a4c99d5a49a64ffd"

FAILING = "m0016"
BUDGET = 8e7


@pytest.fixture(scope="module")
def store() -> SnapshotStore:
    return SnapshotStore(CANONICAL)


@pytest.fixture(scope="module")
def world(store: SnapshotStore) -> World:
    return get_world(store)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    plans.clear()
    labs.clear()
    monkeypatch.setattr(labs, "_model_down_until", 0.0)
    yield
    plans.clear()
    labs.clear()


def _no_model() -> AgentSettings:
    return AgentSettings(ANTHROPIC_API_KEY="", OPENROUTER_API_KEY="", _env_file=None)


def _small_lab(world: World, settings: AgentSettings | None = None) -> labs.Lab:
    return labs.create(world, settings or _no_model(), training=[FAILING], budgets=[BUDGET],
                       k=3, banned=[])


def _run(events) -> list[dict[str, Any]]:
    return list(events)


# ------------------------------------------------------------------ formulas


@pytest.mark.parametrize("text, reason", [
    ("__import__('os').system('x')", "functions"),
    ("need.real", "only arithmetic"),
    ("revenue * 2", "not one of the clues"),
    ("need ** steps", "a power must be a number"),
    ("eval('1')", "functions"),
    ("need[0]", "only arithmetic"),
    ("3 + 4", "reads no clue"),
    ("", "empty"),
    ("need +", "not a formula"),
    ("lambda: need", "only arithmetic"),
    (" + ".join(["need"] * 30), "too long"),
])
def test_unsafe_or_invalid_text_is_refused_before_anything_runs(text, reason):
    with pytest.raises(FormulaError) as caught:
        parse(text, CLUE_NAMES)
    assert reason in caught.value.message


def test_a_formula_is_canonical_protected_and_typesettable():
    f = parse("need ^ 2 / (cash - cash) + log(-hit) + (need <= budget)", CLUE_NAMES)
    assert f.text == "need ** 2 / (cash - cash) + log(-hit) + (need <= budget)"
    assert set(f.clues) == {"need", "cash", "hit", "budget"}
    clues = {name: np.array([1.0, 2.0]) for name in CLUE_NAMES}
    scores = f.score(clues)
    assert scores.shape == (2,) and np.all(np.isfinite(scores) | (scores == -np.inf))
    tree = f.tree()
    assert tree["t"] == "bin" and tree["op"] == "+"
    assert parse("min(need, budget) * (failing < 1)", CLUE_NAMES).tree()["a"]["t"] == "fn"


def test_banned_clues_are_not_allowed():
    with pytest.raises(FormulaError):
        parse("importance * need", CLUE_NAMES - {"importance"})


# --------------------------------------------------------------------- world


def test_a_plan_follows_the_rule_within_the_budget_and_the_cap(world):
    scenario = world.scenario(FAILING, BUDGET)
    assert scenario.candidates and all(n >= 1e5 for n in scenario.need)
    picks = world.plan(parse("need", CLUE_NAMES), scenario, 2)
    assert 1 <= len(picks) <= 2
    assert sum(a for _, a in picks) <= BUDGET + 1e-6
    need = dict(zip(scenario.candidates, scenario.need, strict=True))
    biggest = max(need, key=need.__getitem__)
    assert picks[0][0] == biggest and picks[0][1] <= need[biggest] + 1e-6
    # The same picks are replayed once.
    first, _ = world.outcome(scenario, picks)
    again, cached_again = world.outcome(scenario, picks)
    assert first == again and cached_again


def test_the_search_reference_spends_no_more_than_the_budget(world):
    cell = world.search(world.scenario(FAILING, BUDGET), 3)
    assert sum(a for _, a in cell.picks) <= BUDGET + 1e-6
    assert 0.0 <= cell.pct <= 100.0


# ----------------------------------------------------------------------- lab


def test_a_lab_scores_the_people_then_mutation_writes_children(world):
    lab = _small_lab(world)
    events = _run(lab.start("need * (failing < 1)"))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "lab" and "search_done" in kinds and kinds[-1] == "generation"
    scored = [e["rule"] for e in events if e["type"] == "scored"]
    assert {r["origin"] for r in scored} == {"people", "you"}
    assert all(len(r["cells"]) == 1 for r in scored)
    events = _run(lab.evolve("evolve", []))
    thinking = next(e for e in events if e["type"] == "thinking")
    assert thinking["writer"] == "mutation" and thinking["parents"]
    children = [e["rule"] for e in events if e["type"] == "scored"]
    assert children and all(r["origin"] == "mutation" and r["generation"] == 1
                            for r in children)
    assert all(set(r["parents"]) <= set(lab.rules) for r in children)


def test_a_bad_rule_is_refused_with_its_reason(world):
    lab = _small_lab(world)
    _run(lab.start(None))
    events = _run(lab.add("revenue * need", None))
    assert events[0]["type"] == "refused"
    assert "not one of the clues" in events[0]["items"][0]["reason"]


class FakeModel:
    """Answers like the API with a forced submit_rules call."""

    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self.rules = rules
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **request: Any) -> SimpleNamespace:
        self.calls.append(request)
        block = SimpleNamespace(type="tool_use", id="tu_1", name="submit_rules",
                                input={"rules": self.rules})
        thought = SimpleNamespace(type="thinking", thinking="m0016 at 8e7 is weak.")
        return SimpleNamespace(stop_reason="tool_use", content=[thought, block])


def test_the_model_writes_rules_and_every_one_is_checked(world, monkeypatch):
    import anthropic

    fake = FakeModel([
        {"formula": "importance * min(need, budget)", "idea": "Important, affordable.",
         "builds_on": ["r2"]},
        {"formula": "need * (failing < 1)", "idea": "Skip the failing merchant.",
         "builds_on": []},
        {"formula": "open('x').read()", "idea": "Escape.", "builds_on": []},
        {"formula": "budget * 2", "idea": "Constant.", "builds_on": []},
    ])
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake)
    settings = AgentSettings(OPENROUTER_API_KEY="or-key", ANTHROPIC_API_KEY="", _env_file=None)
    lab = _small_lab(world, settings)
    _run(lab.start(None))
    events = _run(lab.evolve("evolve", [], deep=True))
    proposed = next(e for e in events if e["type"] == "proposed")
    assert proposed["author"] == settings.model and "weak" in proposed["reasoning"]
    assert [r["formula"] for r in proposed["rules"]] == [
        "importance * min(need, budget)", "need * (failing < 1)"]
    assert len(proposed["refused"]) == 2
    request = fake.calls[0]
    assert request["tool_choice"] == {"type": "tool", "name": "submit_rules"}
    assert request["tools"][0]["strict"] is True
    assert "THE PUZZLE" in request["messages"][0]["content"]
    assert request["extra_body"] == {"reasoning": {"effort": "low"}}
    # Quick rounds switch the model's reasoning off.
    _run(lab.evolve("push", [proposed["rules"][0]["id"]]))
    assert fake.calls[1]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_a_model_failure_hands_the_round_to_mutation(world, monkeypatch):
    import anthropic

    class Broken:
        def __init__(self, **kw: Any) -> None:
            self.messages = SimpleNamespace(create=self.create)

        def create(self, **kw: Any) -> None:
            raise anthropic.APIConnectionError(request=None)  # type: ignore[arg-type]

    monkeypatch.setattr(anthropic, "Anthropic", Broken)
    settings = AgentSettings(OPENROUTER_API_KEY="or-key", ANTHROPIC_API_KEY="", _env_file=None)
    lab = _small_lab(world, settings)
    _run(lab.start(None))
    events = _run(lab.evolve("evolve", []))
    assert any(e["type"] == "notice" for e in events)
    assert next(e for e in events if e["type"] == "proposed")["author"] == "random mutation"


def test_breeding_needs_two_rules(world):
    lab = _small_lab(world)
    _run(lab.start(None))
    with pytest.raises(Exception, match="exactly two"):
        _run(lab.evolve("breed", ["r1"]))


def test_held_out_test_and_paypal_plan(world, store):
    from lce.execution.service import ExecutionService

    lab = _small_lab(world)
    _run(lab.start("need * (failing < 1)"))
    best = lab.best()
    assert best is not None
    events = _run(lab.test([best.id]))
    tested = events[-1]
    assert tested["type"] == "tested" and set(tested["failures"]).isdisjoint({FAILING})
    row = tested["rows"][0]
    assert row["n"] == len(tested["failures"]) and row["decide_ms"] < row["search_ms"]

    used = lab.use(best.id, FAILING, BUDGET)
    assert used["plan_ref"].startswith("rule-") and used["picks"]
    plan = ExecutionService(None, store=store).plan(used["plan_ref"])
    assert [a.merchant_id for a in plan.actions] == [p["merchant_id"] for p in used["picks"]]
    assert plan.replayed_cost_inr == pytest.approx(sum(p["amount"] for p in used["picks"]),
                                                   rel=0.01)


# --------------------------------------------------------------------- route


def test_the_routes(store, monkeypatch):
    from lce.api.app import create_app
    from lce.snapshot import store as store_module

    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    reset_agent_settings_cache()
    monkeypatch.setattr(store_module, "get_store", lambda *a, **k: store)
    try:
        with TestClient(create_app()) as client:
            setup = client.get("/api/v1/discovery/setup").json()
            assert setup["writer"]["mode"] == "mutation" and len(setup["pool"]) >= 5
            assert {c["name"] for c in setup["clues"]} == set(CLUE_NAMES)
            ok = client.post("/api/v1/discovery/check", json={"formula": "need/cash"}).json()
            bad = client.post("/api/v1/discovery/check", json={"formula": "x"}).json()
            assert ok["ok"] and ok["tree"]["op"] == "/" and not bad["ok"]
            with client.stream("POST", "/api/v1/discovery/labs",
                               json={"training": [FAILING], "budgets": [BUDGET]}) as r:
                assert r.headers["content-type"].startswith("text/event-stream")
                lines = [json.loads(line[6:]) for line in r.iter_lines()
                         if line.startswith("data: ")]
            assert lines[0]["type"] == "lab" and lines[-1]["type"] == "done"
            lab_id = lines[0]["lab_id"]
            bad_lab = client.post("/api/v1/discovery/labs/nope/evolve", json={})
            assert bad_lab.status_code in (400, 404, 422)
            used = client.post(f"/api/v1/discovery/labs/{lab_id}/use",
                               json={"rule_id": "r1", "failing": FAILING, "budget": BUDGET})
            assert used.status_code == 200 and used.json()["plan_ref"].startswith("rule-")
    finally:
        reset_agent_settings_cache()
