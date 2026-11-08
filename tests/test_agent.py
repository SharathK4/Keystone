"""Ask Keystone: real tools, grounded answers, plans PayPal can pay.

Runs against the committed snapshot, so every analysis here is the real bounded
analysis. The Claude path is exercised with a stand-in client that behaves like
the API (tool_use, then text); nothing calls Anthropic or PayPal.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lce.agent import plans
from lce.agent.grounding import ungrounded
from lce.agent.loop import run_agent, template_answer
from lce.agent.tools import Toolbox, usd
from lce.config import AgentSettings, reset_agent_settings_cache
from lce.snapshot.store import SnapshotStore

CANONICAL = Path(__file__).resolve().parents[1] / "artifacts" / "snapshots" / \
    "snap-a4c99d5a49a64ffd"


@pytest.fixture(scope="module")
def store() -> SnapshotStore:
    return SnapshotStore(CANONICAL)


@pytest.fixture(autouse=True)
def _fresh_plans():
    plans.clear()
    yield
    plans.clear()


def _no_model() -> AgentSettings:
    return AgentSettings(ANTHROPIC_API_KEY="", _env_file=None)


def _events(question: str, store: SnapshotStore, settings: AgentSettings) -> list[dict[str, Any]]:
    return list(run_agent(question, Toolbox(store), settings))


# ------------------------------------------------------------------ analysis


def test_analyze_with_plan_returns_the_whole_plan(store):
    plain = store.analyze(merchant_ids=["m0038"], magnitude_multiple=2.0)
    snapshot, chosen = store.analyze_with_plan(merchant_ids=["m0038"], magnitude_multiple=2.0)
    strip = {"computed_in_ms", "recommended_intervention", "alternatives", "offer"}
    assert snapshot.model_dump(exclude=strip) == plain.model_dump(exclude=strip)
    # The snapshot names one action; the plan has every action the search chose,
    # and together they cost exactly what the replay committed.
    assert [u.merchant_id for u in chosen] == ["m0005", "m0009"]
    assert sum(u.cost for u in chosen) == pytest.approx(snapshot.counterfactual.cost)


# --------------------------------------------------------------------- tools


def test_find_merchants_orders_by_importance(store):
    result = Toolbox(store).run("find_merchants", {"sector": "any", "tier": "any",
                                                   "order_by": "importance", "limit": 3})
    ranks = [m["importance_rank"] for m in result.data["merchants"]]
    assert ranks == sorted(ranks) and ranks[0] == 1


def test_simulate_registers_a_payable_plan(store):
    box = Toolbox(store)
    result = box.run("simulate_missed_payment",
                     {"merchant_ids": ["m0038"], "size_multiple": 2.0, "starts_at_hour": 0.0})
    assert not result.error and result.plan is not None
    data = result.data
    assert data["plan_ref"].startswith("adhoc-")
    assert [r["merchant_id"] for r in data["rescue"]] == ["m0005", "m0009"]
    assert data["damage_prevented"].endswith("%")
    assert plans.get(data["plan_ref"]) is result.plan
    assert result.ms > 0


def test_tool_inputs_are_bounded(store):
    box = Toolbox(store)
    too_big = box.run("simulate_missed_payment",
                      {"merchant_ids": ["m0038"], "size_multiple": 9.0, "starts_at_hour": 0.0})
    unknown = box.run("simulate_missed_payment",
                      {"merchant_ids": ["m9999"], "size_multiple": 2.0, "starts_at_hour": 0.0})
    assert too_big.error and unknown.error
    assert box.run("no_such_tool", {}).error


def test_check_delay_costs_something(store):
    box = Toolbox(store)
    ref = box.run("simulate_missed_payment", {"merchant_ids": ["m0038"], "size_multiple": 2.0,
                                              "starts_at_hour": 0.0}).data["plan_ref"]
    on_time = box.run("check_delay", {"plan_ref": ref, "delay_hours": 0}).data
    late = box.run("check_delay", {"plan_ref": ref, "delay_hours": 48}).data
    assert on_time["payments_kept_on_time_if_late"] == on_time["payments_kept_on_time_if_on_time"]
    assert late["payments_kept_on_time_if_late"] != late["payments_kept_on_time_if_on_time"]


def test_prepare_payout_never_pays_and_needs_mapped_accounts(store):
    box = Toolbox(store)  # no database: nothing is mapped
    ref = box.run("simulate_missed_payment", {"merchant_ids": ["m0038"], "size_multiple": 2.0,
                                              "starts_at_hour": 0.0}).data["plan_ref"]
    result = box.run("prepare_paypal_payout", {"plan_ref": ref})
    assert result.payment is not None and result.payment["complete"]
    assert all(not p["payable"] for p in result.payment["payouts"])
    assert result.data["needs"] == "a person to approve on screen"


# ----------------------------------------------------------------- grounding


def test_grounding_accepts_quoted_figures_and_rejects_invented_ones():
    sources = [json.dumps({"rescue_cost": "$1.94M", "damage_prevented": "42%",
                           "runs_short_after": "about 4 days"})]
    assert ungrounded("Fund m0005 and m0009 ($1.94M); it prevents 42%.", sources) == []
    assert ungrounded("It takes about 4 days.", sources) == []
    assert ungrounded("It prevents 55% for $2M.", sources) == ["$2M", "55%"]
    assert ungrounded("m0038 and m0005 are named, not counted.", sources) == []


# ---------------------------------------------------------------- rules loop


def test_rules_answer_streams_real_steps_and_a_grounded_answer(store):
    events = _events("What if m0038 misses a payment, and the money arrives 2 days late?",
                     store, _no_model())
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and events[0]["mode"] == "rules"
    assert kinds[-1] == "done"
    tools = [e["tool"] for e in events if e["type"] == "step" and e["status"] == "done"]
    assert tools == ["simulate_missed_payment", "check_delay", "prepare_paypal_payout"]
    assert "scenario" in kinds and "payment" in kinds
    answer = next(e for e in events if e["type"] == "answer")
    assert answer["grounded"] and answer["source"] == "rules"
    assert answer["text"].startswith("Fund m0005")


def test_rules_resolve_a_described_merchant(store):
    events = _events("What if our most important merchant can't pay?", store, _no_model())
    found = next(e for e in events if e.get("tool") == "find_merchants" and e["status"] == "done")
    assert found["summary"].startswith("m0006")


# --------------------------------------------------------------- Claude path


def _block(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**kw)


class FakeClaude:
    """Answers like the API: first a tool call, then text built from the result."""

    def __init__(self, final_text: str) -> None:
        self.final_text = final_text
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **request: Any) -> SimpleNamespace:
        self.calls.append(request)
        if len(self.calls) == 1:
            return SimpleNamespace(model=request["model"], stop_reason="tool_use", content=[
                _block(type="tool_use", id="tu_1", name="simulate_missed_payment",
                       input={"merchant_ids": ["m0038"], "size_multiple": 2.0,
                              "starts_at_hour": 0.0})])
        result = json.loads(request["messages"][-1]["content"][0]["content"])
        text = self.final_text.format(**result)
        return SimpleNamespace(model=request["model"], stop_reason="end_turn",
                               content=[_block(type="text", text=text)])


def _claude_run(store, monkeypatch, final_text: str) -> tuple[list[dict[str, Any]], FakeClaude]:
    import anthropic

    fake = FakeClaude(final_text)
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: fake)
    settings = AgentSettings(ANTHROPIC_API_KEY="test-key", LCE_AGENT_MODEL="claude-opus-5-5",
                             _env_file=None)
    return _events("What if m0038 misses a payment?", store, settings), fake


def test_claude_calls_the_tools_and_its_grounded_answer_is_shown(store, monkeypatch):
    events, fake = _claude_run(
        store, monkeypatch, "Fund m0005 and m0009 ({rescue_cost}); it prevents {damage_prevented}.")
    answer = next(e for e in events if e["type"] == "answer")
    assert answer["source"] == "claude-opus-5-5" and answer["grounded"]
    assert "prevents" in answer["text"]
    # Tools are strict, the model never forced, and the refusal fallback is on.
    first = fake.calls[0]
    assert all(t["strict"] for t in first["tools"])
    assert "tool_choice" not in first
    assert first["fallbacks"] == "default"
    # The PayPal step runs even though the model did not ask for it.
    assert any(e.get("tool") == "prepare_paypal_payout" for e in events)


def test_an_invented_number_is_replaced_by_the_tool_figures(store, monkeypatch):
    events, _ = _claude_run(store, monkeypatch, "Fund m0005 with $5M; it prevents 99% of it.")
    answer = next(e for e in events if e["type"] == "answer")
    assert answer["source"] == "template" and answer["replaced"]
    assert "$5M" not in answer["text"] and "99%" not in answer["text"]


def test_model_failure_falls_back_to_the_rules(store, monkeypatch):
    import anthropic

    class Broken:
        def __init__(self, **kw: Any) -> None:
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

        def create(self, **kw: Any) -> None:
            raise anthropic.APIConnectionError(request=None)  # type: ignore[arg-type]

    monkeypatch.setattr(anthropic, "Anthropic", Broken)
    settings = AgentSettings(ANTHROPIC_API_KEY="k", _env_file=None)
    events = _events("What if m0038 misses a payment?", store, settings)
    assert any(e["type"] == "notice" for e in events)
    assert next(e for e in events if e["type"] == "answer")["source"] == "rules"


# ----------------------------------------------------------------- execution


def test_the_execution_layer_prices_an_agent_plan_like_any_other(store):
    from lce.execution.service import ExecutionService

    box = Toolbox(store)
    ref = box.run("simulate_missed_payment", {"merchant_ids": ["m0038"], "size_multiple": 2.0,
                                              "starts_at_hour": 0.0}).data["plan_ref"]
    service = ExecutionService(None, store=store)
    plan = service.plan(ref)
    again = service.plan(ref)
    assert plan.complete and plan.plan_hash == again.plan_hash
    assert [a.merchant_id for a in plan.actions] == ["m0005", "m0009"]
    assert all(a.mapping == "payout" for a in plan.actions)
    assert template_answer(box).startswith("Fund m0005")
    assert usd(plan.replayed_cost_inr).startswith("$")


# --------------------------------------------------------------------- route


def test_the_route_streams_events(store, monkeypatch):
    from lce.api.app import create_app
    from lce.snapshot import store as store_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    reset_agent_settings_cache()
    monkeypatch.setattr(store_module, "get_store", lambda *a, **k: store)
    try:
        with TestClient(create_app()) as client:
            assert client.get("/api/v1/agent/status").json()["mode"] == "rules"
            with client.stream("POST", "/api/v1/agent/ask",
                               json={"question": "What if m0038 misses a payment?"}) as r:
                assert r.headers["content-type"].startswith("text/event-stream")
                lines = [json.loads(line[6:]) for line in r.iter_lines()
                         if line.startswith("data: ")]
        assert lines[0]["type"] == "start" and lines[-1]["type"] == "done"
        assert any(e["type"] == "answer" for e in lines)
        assert client.post("/api/v1/agent/ask", json={"question": "x"}).status_code == 422
    finally:
        reset_agent_settings_cache()
