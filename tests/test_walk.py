"""The walk: the failing payment, fired through the real pipeline, and what follows.

Development events are signed in process and recorded fixtures are built from
captures signed with a test key, exactly as PayPal signs; both go through the
real verifier, state machine and bounded analysis against the committed
snapshot. Nothing here calls PayPal.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lce.config import reset_demo_settings_cache, reset_paypal_settings_cache
from lce.data.orm import Base, ExecutionRecordRow, ReentryAnalysisRow
from lce.errors import ValidationError
from lce.paypal.money import DemoScale
from lce.snapshot.store import SnapshotStore
from lce.walk import recording
from lce.walk.ask import DURATION, HELP, ParseContext, RuleParser, ShockRequest
from lce.walk.dev_events import DEV_STAMP, DEV_WEBHOOK_ID, dev_item_event, dev_signer
from lce.walk.fire import FireRefused, process, trigger_export
from lce.walk.recording import build_fixture, capture_delivery, load_recorded
from lce.walk.speed import build_walk_speed, read_walk_speed, write_walk_speed
from lce.walk.thread import (
    WALK_SCENARIO_ID,
    WalkSettings,
    ensure_trigger_record,
    reset_walk_settings_cache,
    trigger,
)
from paypal_fakes import CERT_URL, FAKE_WEBHOOK_ID, keypair, signed_headers

CANONICAL = Path(__file__).resolve().parents[1] / "artifacts" / "snapshots" / \
    "snap-a4c99d5a49a64ffd"
WALK = WalkSettings(LCE_WALK_MERCHANT="m0038", LCE_WALK_MULTIPLE=2.0)


@pytest.fixture(scope="module")
def store() -> SnapshotStore:
    return SnapshotStore(CANONICAL)


@pytest.fixture(scope="module")
def scale() -> DemoScale:
    from decimal import Decimal

    return DemoScale(rate=Decimal("0.00001"), currency="USD")


@pytest.fixture
def db(monkeypatch):
    from lce.data import database

    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "get_session_factory", lambda: factory)
    yield factory
    engine.dispose()


@pytest.fixture
def fixture_path(tmp_path, monkeypatch):
    """No committed recording leaks into a test unless the test writes one."""
    path = tmp_path / "walk.json"
    monkeypatch.setattr(recording, "DEFAULT_FIXTURE", path)
    from lce.api.routers import walk as walk_routes

    monkeypatch.setattr(walk_routes, "load_recorded", lambda: load_recorded(path))
    return path


@pytest.fixture
def client(store, db, fixture_path, monkeypatch):
    from lce.api.app import create_app
    from lce.snapshot import store as store_module

    monkeypatch.setenv("LCE_WALK_MERCHANT", "m0038")
    reset_walk_settings_cache()
    monkeypatch.setattr(store_module, "get_store", lambda *a, **k: store)
    with TestClient(create_app()) as c:
        yield c
    reset_walk_settings_cache()


# -------------------------------------------------------------------- thread


class TestTrigger:
    def test_the_missing_payment_is_the_declared_multiple_of_the_cushion(self, store, scale):
        t = trigger(store, WALK, scale)
        assert t.missing_inr == pytest.approx(2.0 * t.slack_inr)
        assert t.executed == scale.apply(t.missing_inr)
        assert t.idempotency_key.startswith("ks-")

    def test_the_fingerprint_is_stable_and_moves_with_the_amount(self, store, scale):
        a, b = trigger(store, WALK, scale), trigger(store, WALK, scale)
        other = trigger(store, WalkSettings(LCE_WALK_MERCHANT="m0038", LCE_WALK_MULTIPLE=1.5),
                        scale)
        assert a.plan_hash == b.plan_hash
        assert a.plan_hash != other.plan_hash

    def test_one_record_per_trigger(self, store, scale, db):
        t = trigger(store, WALK, scale)
        with db() as s:
            first = ensure_trigger_record(s, t, approver="test", reason="test")
            second = ensure_trigger_record(s, t, approver="test", reason="test")
            assert first.record_id == second.record_id
            assert first.scenario_id == WALK_SCENARIO_ID


# ---------------------------------------------------------------------- fire


class TestFire:
    def test_development_fire_runs_the_real_pipeline(self, client, store, db):
        walk = client.get("/api/v1/walk").json()
        assert walk["mode"] == "development" and walk["merchant_id"] == "m0038"

        body = client.post("/api/v1/walk/fire").json()
        assert body["event"]["source"] == "development"
        assert body["event"]["stamp"] == DEV_STAMP
        assert body["event"]["verification"] == "verified"
        assert body["event"]["outcome"] == "UNCLAIMED"
        assert body["event"]["record_status"] == "failed"
        assert body["shock"]["merchant_id"] == "m0038"
        assert body["shock"]["magnitude_multiple"] == pytest.approx(2.0)
        assert body["shock"]["missing_inr"] == pytest.approx(walk["missing_inr"])

        live = store.analyze(merchant_ids=["m0038"], magnitude_multiple=2.0, onset_hours=0.0)
        got = body["analysis"]["counterfactual"]
        assert got["disruption_reduction_pct"] == pytest.approx(
            live.counterfactual.disruption_reduction_pct)
        assert body["computed_in_ms"] > 0

    def test_a_second_fire_recomputes_but_records_the_reentry_once(self, client, db):
        first = client.post("/api/v1/walk/fire").json()
        second = client.post("/api/v1/walk/fire").json()
        assert second["event"]["event_id"] != first["event"]["event_id"]
        assert second["reentry"]["analysis_id"] == first["reentry"]["analysis_id"]
        assert second["analysis"]["counterfactual"] == first["analysis"]["counterfactual"]
        with db() as s:
            assert len(list(s.execute(select(ReentryAnalysisRow)).scalars())) == 1

    def test_a_tampered_event_changes_nothing(self, store, scale, db):
        t = trigger(store, WALK, scale)
        with db() as s:
            record = ensure_trigger_record(s, t, approver="test", reason="test")
            raw = json.dumps(dev_item_event(record)).encode()
            headers = dev_signer().headers(raw)
            with pytest.raises(FireRefused):
                process(s, store, raw_body=raw.replace(b"UNCLAIMED", b"FAILED"),
                        headers=headers, webhook_id=DEV_WEBHOOK_ID,
                        load_cert=dev_signer().loader, source="development", stamp=DEV_STAMP)
            row = s.execute(select(ExecutionRecordRow)).scalar_one()
            assert row.status == "submitted"

    def test_a_success_is_not_a_failure_to_analyse(self, store, scale, db):
        t = trigger(store, WALK, scale)
        with db() as s:
            record = ensure_trigger_record(s, t, approver="test", reason="test")
            event = dev_item_event(record, outcome="SUCCEEDED")
            raw = json.dumps(event).encode()
            with pytest.raises(FireRefused):
                process(s, store, raw_body=raw, headers=dev_signer().headers(raw),
                        webhook_id=DEV_WEBHOOK_ID, load_cert=dev_signer().loader,
                        source="development", stamp=DEV_STAMP)

    def test_hosted_without_a_recording_says_so(self, client, monkeypatch):
        monkeypatch.setenv("LCE_DEMO_MODE", "recorded")
        reset_demo_settings_cache()
        try:
            walk = client.get("/api/v1/walk").json()
            assert walk["mode"] == "unavailable" and walk["mode_reason"]
            assert client.post("/api/v1/walk/fire").status_code == 503
        finally:
            monkeypatch.setenv("LCE_DEMO_MODE", "off")
            reset_demo_settings_cache()


# ------------------------------------------------------------------ recorded


def _capture(tmp_path: Path, store, scale, db, *, webhook_id: str = FAKE_WEBHOOK_ID):
    """A test-signed capture of the walk's failure, plus the trigger it belongs to."""
    key, pem = keypair()
    t = trigger(store, WALK, scale)
    with db() as s:
        record = ensure_trigger_record(s, t, approver="live walk", reason="test")
        from lce.data.orm import ExecutionApprovalRow

        approval = s.execute(select(ExecutionApprovalRow)).scalar_one()
        exported = trigger_export(record, approval)
        event = dev_item_event(record)
    raw = json.dumps(event).encode()
    headers = signed_headers(key, raw, webhook_id)
    path = capture_delivery(tmp_path / "deliveries", raw_body=raw, headers=headers,
                            event_id=event["id"])
    return json.loads(path.read_text()), pem, exported, raw


class TestRecorded:
    def test_a_capture_that_does_not_verify_is_never_recorded(self, tmp_path, store, scale, db):
        capture, pem, exported, _raw = _capture(tmp_path, store, scale, db)
        capture["raw_body_b64"] = base64.b64encode(b'{"id":"forged"}').decode()
        with pytest.raises(ValidationError):
            build_fixture(capture, certificate_pem=pem, trigger=exported,
                          webhook_id=FAKE_WEBHOOK_ID)

    def test_the_capture_keeps_the_bytes_and_no_webhook_id(self, tmp_path, store, scale, db):
        capture, _pem, _exported, raw = _capture(tmp_path, store, scale, db)
        assert base64.b64decode(capture["raw_body_b64"]) == raw
        assert FAKE_WEBHOOK_ID not in json.dumps(capture)
        assert set(capture["headers"]) == {
            "paypal-transmission-id", "paypal-transmission-time", "paypal-transmission-sig",
            "paypal-cert-url", "paypal-auth-algo"}

    def test_a_changed_byte_in_the_fixture_is_refused(self, tmp_path, store, scale, db,
                                                      fixture_path):
        capture, pem, exported, _raw = _capture(tmp_path, store, scale, db)
        fixture = build_fixture(capture, certificate_pem=pem, trigger=exported,
                                webhook_id=FAKE_WEBHOOK_ID)
        fixture["trigger"]["merchant_id"] = "m0005"
        fixture_path.write_text(json.dumps(fixture))
        with pytest.raises(ValidationError):
            load_recorded(fixture_path)

    def test_recorded_mode_replays_through_a_fresh_database(
        self, tmp_path, store, scale, db, client, fixture_path, monkeypatch
    ):
        capture, pem, exported, _raw = _capture(tmp_path, store, scale, db)
        fixture_path.write_text(json.dumps(build_fixture(
            capture, certificate_pem=pem, trigger=exported, webhook_id=FAKE_WEBHOOK_ID)))
        with db() as s:  # a database that never saw the trigger
            for model in reversed(Base.metadata.sorted_tables):
                s.execute(model.delete())
            s.commit()
        monkeypatch.setenv("PAYPAL_WEBHOOK_ID", FAKE_WEBHOOK_ID)
        reset_paypal_settings_cache()
        try:
            assert client.get("/api/v1/walk").json()["mode"] == "recorded"
            body = client.post("/api/v1/walk/fire").json()
            assert body["event"]["source"] == "recorded"
            assert body["event"]["stamp"] == "recorded PayPal event, processed live"
            assert body["event"]["verification"] == "verified"
            assert body["event"]["record_status"] == "failed"
            assert body["shock"]["merchant_id"] == "m0038"
        finally:
            reset_paypal_settings_cache()

    def test_recorded_mode_needs_the_webhook_id(self, tmp_path, store, scale, db, client,
                                                fixture_path):
        capture, pem, exported, _raw = _capture(tmp_path, store, scale, db)
        fixture_path.write_text(json.dumps(build_fixture(
            capture, certificate_pem=pem, trigger=exported, webhook_id=FAKE_WEBHOOK_ID)))
        response = client.post("/api/v1/walk/fire")
        assert response.status_code == 409
        assert "PAYPAL_WEBHOOK_ID" in response.json()["message"]

    def test_the_route_captures_verified_deliveries_when_asked(self, tmp_path, store, db,
                                                               monkeypatch):
        from lce.api.app import create_app
        from lce.api.routers import webhooks as routes
        from lce.snapshot import store as store_module

        key, pem = keypair()
        monkeypatch.setenv("PAYPAL_WEBHOOK_ID", FAKE_WEBHOOK_ID)
        monkeypatch.setenv("LCE_RECORD_DELIVERIES_DIR", str(tmp_path / "caught"))
        reset_paypal_settings_cache()
        reset_walk_settings_cache()
        monkeypatch.setattr(store_module, "get_store", lambda *a, **k: store)
        app = create_app()
        app.dependency_overrides[routes.paypal_cert_loader] = lambda: (lambda _url: pem)
        raw = json.dumps({"id": "WH-CAUGHT-1", "event_type": "X", "resource": {}}).encode()
        try:
            with TestClient(app) as c:
                ok = c.post("/api/v1/webhooks/paypal", content=raw,
                            headers=signed_headers(key, raw))
                forged = c.post("/api/v1/webhooks/paypal", content=raw,
                                headers=signed_headers(keypair()[0], raw))
            assert ok.status_code == 200 and forged.status_code == 400
            caught = json.loads((tmp_path / "caught" / "WH-CAUGHT-1.json").read_text())
            assert base64.b64decode(caught["raw_body_b64"]) == raw
            assert caught["headers"]["paypal-cert-url"] == CERT_URL
            assert len(list((tmp_path / "caught").iterdir())) == 1
        finally:
            reset_paypal_settings_cache()
            reset_walk_settings_cache()


# ----------------------------------------------------------------------- ask


@pytest.fixture(scope="module")
def ctx(store) -> ParseContext:
    def exists(m: str) -> bool:
        try:
            store.merchant(m)
        except Exception:
            return False
        return True

    return ParseContext(horizon_hours=store.horizon_hours, merchant_exists=exists)


BASE = ShockRequest(merchant_ids=["m0038"], magnitude_multiple=2.0, onset_hours=0.0)


class TestAsk:
    @pytest.mark.parametrize("text, field, value", [
        ("what if it were twice as large?", "magnitude_multiple", 4.0),
        ("half the size", "magnitude_multiple", 1.0),
        ("what if it hit 48 hours later", "onset_hours", 48.0),
        ("what if it lands in 2 days", "onset_hours", 48.0),
        ("on day 3", "onset_hours", 48.0),
        ("at hour 12", "onset_hours", 12.0),
        ("what if m0005 fails too?", "merchant_ids", ["m0005", "m0038"]),
        ("m0005 instead", "merchant_ids", ["m0005"]),
    ])
    def test_supported_questions(self, ctx, text, field, value):
        r = RuleParser().parse(text, base=BASE, ctx=ctx)
        assert r.request is not None, r.refusal
        assert getattr(r.request, field) == value
        assert field in r.changed

    @pytest.mark.parametrize("text, refusal", [
        ("what if it lasts three days?", DURATION),
        ("what if it drags on for a week", DURATION),
        ("relax", HELP),
        ("", HELP),
    ])
    def test_refusals_with_the_reason(self, ctx, text, refusal):
        r = RuleParser().parse(text, base=BASE, ctx=ctx)
        assert r.request is None and r.refusal == refusal

    def test_out_of_bounds_is_refused_in_one_line(self, ctx):
        r = RuleParser().parse("three times as large", base=BASE, ctx=ctx)
        assert r.request is None and "6x" in (r.refusal or "")
        assert RuleParser().parse("m9999 too", base=BASE, ctx=ctx).refusal == \
            "m9999 is not in this network."
        assert "past" in (RuleParser().parse("at hour 500", base=BASE, ctx=ctx).refusal or "")

    def test_the_route_parses_and_computes_nothing(self, client):
        body = client.post("/api/v1/walk/ask", json={"text": "twice as large"}).json()
        assert body["ok"] and body["provider"] == "rules"
        assert body["request"] == {"merchant_ids": ["m0038"], "magnitude_multiple": 4.0,
                                   "onset_hours": 0.0}
        refused = client.post("/api/v1/walk/ask", json={"text": "lasts three days"}).json()
        assert not refused["ok"] and refused["refusal"] == DURATION


# --------------------------------------------------------------------- speed


class TestSpeed:
    def test_zero_delay_is_the_live_analysis(self, store):
        payload = build_walk_speed(store, merchant_ids=["m0038"], magnitude_multiple=2.0,
                                   delays=(0.0, 48.0))
        live = store.analyze(merchant_ids=["m0038"], magnitude_multiple=2.0, onset_hours=0.0)
        zero = payload["delays"][0]
        assert zero["as_chosen"] == zero["replanned"]
        assert zero["as_chosen"]["disruption_cut_pct"] == pytest.approx(
            live.counterfactual.disruption_reduction_pct)
        assert zero["as_chosen"]["payments_kept_on_time_inr"] == pytest.approx(
            live.counterfactual.commerce_preserved)
        late = payload["delays"][1]
        assert all(a["lands_at_hours"] >= 48.0 for a in late["as_chosen"]["actions"])

    def test_bad_grids_are_refused(self, store):
        with pytest.raises(ValidationError):
            build_walk_speed(store, merchant_ids=["m0038"], magnitude_multiple=2.0,
                             delays=(6.0, 0.0))

    def test_artifact_round_trip_and_tamper(self, store, tmp_path):
        payload = build_walk_speed(store, merchant_ids=["m0038"], magnitude_multiple=2.0,
                                   delays=(0.0,))
        path = write_walk_speed(tmp_path, payload)
        read = read_walk_speed(tmp_path, store.snapshot_id, ["m0038"], 2.0, 0.0)
        assert read["delays"] == payload["delays"]
        data = json.loads(path.read_text())
        data["delays"][0]["as_chosen"]["disruption_cut_pct"] = 99.0
        path.write_text(json.dumps(data))
        with pytest.raises(ValidationError):
            read_walk_speed(tmp_path, store.snapshot_id, ["m0038"], 2.0, 0.0)

    def test_the_route_serves_the_built_artifact(self, client, store, tmp_path, monkeypatch):
        from lce.api.routers import walk as walk_routes

        monkeypatch.setattr(walk_routes, "SPEED_ROOT", tmp_path)
        assert client.get("/api/v1/walk/speed").status_code == 404
        write_walk_speed(tmp_path, build_walk_speed(
            store, merchant_ids=["m0038"], magnitude_multiple=2.0, delays=(0.0,)))
        body = client.get("/api/v1/walk/speed").json()
        assert body["label"].startswith("Replayed offline")
        assert body["delays"][0]["delay_hours"] == 0.0
