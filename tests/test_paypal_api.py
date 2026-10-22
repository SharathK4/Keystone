"""The execution routes over HTTP, and what must not change.

* ``/execution/status`` keeps its shipped behaviour unless
  ``LCE_EXECUTION_PROVIDER=paypal_sandbox`` is set.
* ``/execution/paypal`` answers with no credentials and no database, and says why.
* Approve -> execute -> ledger works end to end against a fake PayPal.
* Without a database the execution routes return 503 with the reason, and the
  analysis routes are unaffected.
* No new field name carries simulator vocabulary (the scan in
  ``test_snapshot.py`` covers snapshot payloads; this covers these models).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from lce.config import reset_paypal_settings_cache, reset_settings_cache
from lce.data.orm import Base
from lce.execution import models as execution_models
from lce.execution.paypal import reset_report_cache
from lce.execution.service import ExecutionService
from paypal_fakes import ExtractStore, FakePayPal, settings


def _forbidden_tokens() -> tuple[str, ...]:
    """Read the list from test_snapshot.py so the two scans cannot drift apart."""
    source = (Path(__file__).parent / "test_snapshot.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "FORBIDDEN_FIELD_TOKENS" for t in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("FORBIDDEN_FIELD_TOKENS not found in test_snapshot.py")


@pytest.fixture
def extract() -> ExtractStore:
    return ExtractStore()


@pytest.fixture
def memory_session() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def app_client(extract, monkeypatch) -> Iterator[TestClient]:
    from lce.api.app import create_app
    from lce.snapshot import store as store_module

    monkeypatch.setattr(store_module, "get_store", lambda *a, **k: extract)
    reset_report_cache()
    with TestClient(create_app()) as client:
        yield client
    reset_report_cache()


@pytest.fixture
def wired(app_client, extract, memory_session):
    """The app with its execution service pointed at a fake PayPal."""
    from lce.api.routers import execution as routes

    fake = FakePayPal()

    def service() -> Iterator[ExecutionService]:
        with memory_session() as s:
            yield ExecutionService(s, store=extract, settings=settings(),
                                   client_factory=lambda st: fake.client(st))

    app = app_client.app
    app.dependency_overrides[routes._service] = service
    with memory_session() as s:
        svc = ExecutionService(s, store=extract, settings=settings())
        for m in ("m0005", "m0009"):
            svc.map_identity(m, recipient_type="EMAIL", receiver=f"sb-{m}@personal.example.com")
        s.commit()
    yield app_client, fake
    app.dependency_overrides.clear()


class TestStatusIsUnchangedByDefault:
    def test_default_provider_is_still_razorpay(self, app_client, monkeypatch):
        monkeypatch.delenv("LCE_EXECUTION_PROVIDER", raising=False)
        reset_settings_cache()
        body = app_client.get("/api/v1/execution/status", params={"probe": False}).json()
        assert body["provider"] == "razorpay_test"
        assert body["fallback_provider"] == "simulation"
        assert "Razorpay" in body["note"]

    def test_the_switch_reports_paypal_in_the_same_contract(self, app_client, monkeypatch):
        monkeypatch.setenv("LCE_EXECUTION_PROVIDER", "paypal_sandbox")
        reset_settings_cache()
        try:
            body = app_client.get("/api/v1/execution/status", params={"probe": False}).json()
        finally:
            monkeypatch.delenv("LCE_EXECUTION_PROVIDER")
            reset_settings_cache()
        assert body["provider"] == "paypal_sandbox"
        assert body["executable_intervention_types"] == []
        assert "not configured" in body["note"]
        assert set(body) == {"provider", "mode", "configured", "api_reachable", "capabilities",
                             "executable_intervention_types", "fallback_provider", "note"}


class TestPayPalStatus:
    def test_answers_with_no_credentials_and_says_why(self, app_client):
        body = app_client.get("/api/v1/execution/paypal").json()
        assert body["configured"] is False and body["probed"] is False
        assert body["refused"] is None
        assert {c["name"] for c in body["capabilities"]} == {
            "api_reachable", "payouts", "invoicing", "webhooks"}
        assert all(c["available"] is None and c["reason"] == "credentials not configured"
                   for c in body["capabilities"])
        assert body["demo"]["currency"] == "USD" and body["demo"]["scale"] == "0.00001"

    def test_live_mode_is_reported_as_refused(self, app_client, monkeypatch):
        monkeypatch.setenv("PAYPAL_MODE", "live")
        reset_paypal_settings_cache()
        try:
            body = app_client.get("/api/v1/execution/paypal").json()
        finally:
            monkeypatch.delenv("PAYPAL_MODE")
            reset_paypal_settings_cache()
        assert "refused" in body["refused"]
        assert body["capabilities"] == []


class TestThePlanRoute:
    def test_plan_is_served_with_its_hash(self, app_client, extract):
        sid = extract.by_family("single_missed_inflow").scenario_id
        body = app_client.get(f"/api/v1/execution/plans/{sid}").json()
        assert body["complete"] is True and len(body["plan_hash"]) == 64
        assert [a["executed"]["value"] for a in body["actions"]] == ["350.71", "772.69"]
        assert body["demo"]["unit"] == "USD per analytic INR"

    def test_no_plan_is_a_404(self, app_client, extract):
        sid = extract.by_family("supplier_failure").scenario_id
        assert app_client.get(f"/api/v1/execution/plans/{sid}").status_code == 404


class TestApproveExecuteLedger:
    def test_the_whole_lifecycle(self, wired, extract):
        client, fake = wired
        sid = extract.by_family("single_missed_inflow").scenario_id
        plan = client.get(f"/api/v1/execution/plans/{sid}").json()

        refused = client.post(f"/api/v1/execution/plans/{sid}/approvals",
                              json={"approver": "Approver One", "plan_hash": "f" * 64})
        assert refused.status_code == 409

        approval = client.post(f"/api/v1/execution/plans/{sid}/approvals",
                               json={"approver": "Approver One", "plan_hash": plan["plan_hash"]})
        assert approval.status_code == 201
        approval_id = approval.json()["approval_id"]
        assert approval.json()["executed_total"] == {"currency": "USD", "value": "1123.40"}

        records = client.post(f"/api/v1/execution/plans/{sid}/executions",
                              json={"approval_id": approval_id}).json()
        assert [r["status"] for r in records] == ["pending", "pending"]
        assert all(r["paypal_payout_batch_id"] for r in records)

        again = client.post(f"/api/v1/execution/plans/{sid}/executions",
                            json={"approval_id": approval_id}).json()
        assert [r["record_id"] for r in again] == [r["record_id"] for r in records]
        assert len(fake.batches) == 2

        fake.item_status = "UNCLAIMED"
        refreshed = client.post(f"/api/v1/execution/records/{records[0]['record_id']}/refresh")
        assert refreshed.json()["status"] == "failed"

        ledger = client.get("/api/v1/execution/ledger", params={"scenario_id": sid}).json()
        assert len(ledger["approvals"]) == 1 and len(ledger["records"]) == 2
        kinds = [e["kind"] for e in ledger["events"]]
        assert kinds[0] == "approval_refused" and "provider_status_applied" in kinds

    def test_execute_without_approval_is_409(self, wired, extract):
        client, fake = wired
        sid = extract.by_family("single_missed_inflow").scenario_id
        response = client.post(f"/api/v1/execution/plans/{sid}/executions",
                               json={"approval_id": "apr_nope"})
        assert response.status_code == 409
        assert response.json()["code"] == "execution_refused"
        assert fake.payout_posts == []


class TestNoDatabase:
    def test_execution_routes_say_why_and_analysis_keeps_working(
        self, app_client, extract, monkeypatch, tmp_path
    ):
        from lce.data import database

        dead = create_engine(f"sqlite:///{(tmp_path / 'missing' / 'x.sqlite').as_posix()}")
        monkeypatch.setattr(database, "get_session_factory", lambda: sessionmaker(bind=dead))
        sid = extract.by_family("single_missed_inflow").scenario_id

        ledger = app_client.get("/api/v1/execution/ledger", params={"scenario_id": sid})
        assert ledger.status_code == 503
        assert "need a database" in ledger.json()["message"]

        plan = app_client.get(f"/api/v1/execution/plans/{sid}")
        assert plan.status_code == 200
        assert all(a["recipient"] is None for a in plan.json()["actions"])

        assert app_client.get("/api/v1/execution/paypal").status_code == 200


class TestVocabulary:
    def test_no_simulator_vocabulary_in_new_models(self):
        tokens = _forbidden_tokens()
        offenders = []
        for name in dir(execution_models):
            model = getattr(execution_models, name)
            if not isinstance(model, type) or not hasattr(model, "model_fields"):
                continue
            for field in model.model_fields:
                if any(t in field.lower() for t in tokens):
                    offenders.append(f"{name}.{field}")
        assert offenders == []
