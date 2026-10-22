"""Hosted-demo safety and the Render blueprint.

With ``LCE_DEMO_MODE=recorded`` nobody starts a sandbox payout without the demo
token; a token holder is rate-limited and limited to the canonical plan; reads
are untouched. ``lce demo reset`` clears a run and keeps identities. The
blueprint carries no secret and serves the analysis without a database.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from lce.config import reset_demo_settings_cache
from lce.data.orm import Base, ExecutionApprovalRow, ExecutionRecordRow, PayPalIdentityRow
from lce.execution import demo
from lce.execution.paypal import reset_report_cache
from lce.execution.service import ExecutionService
from paypal_fakes import ExtractStore, FakePayPal, settings

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = "scn-07c647ec1459"
TOKEN = "demo-token-for-tests-only"


@pytest.fixture
def extract() -> ExtractStore:
    return ExtractStore()


@pytest.fixture
def sessions() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def recorded(monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("LCE_DEMO_MODE", "recorded")
    monkeypatch.setenv("LCE_DEMO_TOKEN", TOKEN)
    monkeypatch.setenv("LCE_DEMO_RATE_LIMIT", "3")
    reset_demo_settings_cache()
    demo.limiter.clear()
    yield
    reset_demo_settings_cache()
    demo.limiter.clear()


@pytest.fixture
def client(extract, sessions, monkeypatch) -> Iterator[tuple[TestClient, FakePayPal]]:
    from lce.api.app import create_app
    from lce.api.routers import execution as routes
    from lce.snapshot import store as store_module

    monkeypatch.setattr(store_module, "get_store", lambda *a, **k: extract)
    reset_report_cache()
    fake = FakePayPal()

    def service() -> Iterator[ExecutionService]:
        with sessions() as s:
            yield ExecutionService(s, store=extract, settings=settings(),
                                   client_factory=lambda st: fake.client(st))

    with sessions() as s:
        svc = ExecutionService(s, store=extract, settings=settings())
        for m in ("m0005", "m0009"):
            svc.map_identity(m, recipient_type="EMAIL", receiver=f"sb-{m}@personal.example.com")
        s.commit()
    app = create_app()
    app.dependency_overrides[routes._service] = service
    with TestClient(app) as c:
        yield c, fake
    reset_report_cache()


def _approve(c: TestClient, scenario_id: str = CANONICAL, headers: dict | None = None):
    plan = c.get(f"/api/v1/execution/plans/{scenario_id}").json()
    return c.post(f"/api/v1/execution/plans/{scenario_id}/approvals",
                  json={"approver": "Demo Viewer", "plan_hash": plan["plan_hash"]},
                  headers=headers or {})


class TestRecordedMode:
    def test_off_by_default_changes_nothing(self, client):
        c, _fake = client
        assert c.get("/api/v1/execution/demo").json()["mode"] == "off"
        assert _approve(c).status_code == 201

    def test_without_the_token_nothing_starts(self, client, recorded):
        c, fake = client
        status = c.get("/api/v1/execution/demo").json()
        assert status["mode"] == "recorded" and status["token_required"] is True
        assert status["executes_only"] == CANONICAL
        refused = _approve(c)
        assert refused.status_code == 403 and refused.json()["code"] == "demo_refused"
        wrong = _approve(c, headers={demo.TOKEN_HEADER: "guess"})
        assert wrong.status_code == 403
        executed = c.post(f"/api/v1/execution/plans/{CANONICAL}/executions",
                          json={"approval_id": "apr-anything"})
        assert executed.status_code == 403
        assert fake.payout_posts == []

    def test_reads_are_never_limited(self, client, recorded):
        c, _fake = client
        for _ in range(10):
            assert c.get(f"/api/v1/execution/plans/{CANONICAL}").status_code == 200
            assert c.get("/api/v1/execution/paypal").status_code == 200

    def test_a_token_holder_can_run_the_canonical_plan_once(self, client, recorded):
        c, fake = client
        h = {demo.TOKEN_HEADER: TOKEN}
        approval = _approve(c, headers=h)
        assert approval.status_code == 201
        records = c.post(f"/api/v1/execution/plans/{CANONICAL}/executions",
                         json={"approval_id": approval.json()["approval_id"]}, headers=h)
        assert records.status_code == 200 and len(records.json()) == 2
        assert len(fake.payout_posts) == 2

    def test_only_the_canonical_plan(self, client, extract, recorded):
        c, _fake = client
        other = next(s.scenario_id for s in extract.scenarios() if s.scenario_id != CANONICAL)
        refused = c.post(f"/api/v1/execution/plans/{other}/approvals",
                         json={"approver": "Demo Viewer", "plan_hash": "0" * 64},
                         headers={demo.TOKEN_HEADER: TOKEN})
        assert refused.status_code == 403
        assert "only the canonical plan" in refused.json()["message"]

    def test_token_holders_are_rate_limited_per_address(self, client, recorded):
        c, _fake = client
        h = {demo.TOKEN_HEADER: TOKEN, "X-Forwarded-For": "203.0.113.7"}
        codes = [_approve(c, headers=h).status_code for _ in range(4)]
        assert codes == [201, 201, 201, 429]
        other = {demo.TOKEN_HEADER: TOKEN, "X-Forwarded-For": "198.51.100.9"}
        assert _approve(c, headers=other).status_code == 201


class TestRateLimiter:
    def test_the_window_slides(self):
        now = [0.0]
        limiter = demo.RateLimiter(clock=lambda: now[0])
        assert [limiter.allow("a", 2, 60) for _ in range(2)] == [None, None]
        assert limiter.allow("a", 2, 60) == pytest.approx(60.0)
        now[0] = 61.0
        assert limiter.allow("a", 2, 60) is None


class TestReset:
    def test_reset_clears_a_run_and_keeps_identities(self, client, sessions):
        c, _fake = client
        approval = _approve(c).json()
        c.post(f"/api/v1/execution/plans/{CANONICAL}/executions",
               json={"approval_id": approval["approval_id"]})
        with sessions() as s:
            assert s.scalar(select(func.count()).select_from(ExecutionRecordRow)) == 2
            counts = demo.reset(s)
            assert counts["execution_records"] == 2 and counts["execution_approvals"] == 1
            assert s.scalar(select(func.count()).select_from(ExecutionRecordRow)) == 0
            assert s.scalar(select(func.count()).select_from(ExecutionApprovalRow)) == 0
            assert s.scalar(select(func.count()).select_from(PayPalIdentityRow)) == 2

    def test_the_command_needs_yes(self, capsys):
        from lce.cli import build_parser

        args = build_parser().parse_args(["demo", "reset"])
        assert args.func(args) == 2
        assert "--yes" in capsys.readouterr().out

    def test_the_existing_demo_command_parses_as_before(self):
        from lce.cli import build_parser, cmd_demo

        args = build_parser().parse_args(["demo", "-n", "12"])
        assert args.func is cmd_demo and args.n == 12


class TestBlueprint:
    @pytest.fixture(scope="class")
    def blueprint(self) -> dict:
        return yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))

    def test_no_secret_has_a_value(self, blueprint):
        secret = ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID",
                  "LCE_DEMO_TOKEN")
        for service in blueprint["services"]:
            for var in service.get("envVars", []):
                if var["key"] in secret:
                    assert var == {"key": var["key"], "sync": False}, var

    def test_the_api_is_sandbox_and_recorded_with_a_health_check(self, blueprint):
        api = next(s for s in blueprint["services"] if s["name"] == "keystone-api")
        env = {v["key"]: v.get("value") for v in api["envVars"]}
        assert api["healthCheckPath"] == "/api/v1/health/ready"
        assert env["PAYPAL_MODE"] == "sandbox" and env["LCE_DEMO_MODE"] == "recorded"
        assert api["region"] == "oregon"
        assert (ROOT / api["startCommand"].split()[-1]).exists()

    def test_the_start_script_survives_a_missing_database(self):
        script = (ROOT / "scripts" / "render_start.sh").read_text(encoding="utf-8")
        assert "\r" not in script
        assert "set -e" not in script  # a failed migration must not stop the analysis
        assert "postgresql+psycopg://" in script

    def test_the_frontend_is_static_and_routes_the_api(self, blueprint):
        web = next(s for s in blueprint["services"] if s["name"] == "keystone-web")
        assert web["runtime"] == "static" and web["staticPublishPath"] == "frontend/dist"
        assert web["routes"][0]["source"] == "/api/*"
