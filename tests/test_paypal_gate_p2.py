"""Gate P2 - the webhook receiver, the state machine, and failure re-entry.

Deliveries are signed with a real RSA key and certificate exactly as PayPal
signs them, and posted to the real route. The database is a fresh in-memory one
per test; the snapshot is the committed canonical one, so the re-entry analysis
is the real bounded analysis.
"""

from __future__ import annotations

import itertools
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lce.config import reset_paypal_settings_cache
from lce.data.orm import (
    Base,
    ExecutionRecordRow,
    PayPalWebhookEventRow,
    ReentryAnalysisRow,
)
from lce.execution.reentry import failed_payout_shock, reenter
from lce.execution.service import ExecutionService
from lce.execution.state import RANK, TERMINAL, advance
from lce.execution.webhooks import ingest
from lce.paypal.webhooks import Verification
from lce.snapshot.analysis import _liquidity_slack
from lce.snapshot.store import SnapshotStore
from paypal_fakes import (
    FAKE_WEBHOOK_ID,
    FakePayPal,
    item_event,
    keypair,
    settings,
    signed_headers,
)

CANONICAL = Path(__file__).resolve().parents[1] / "artifacts" / "snapshots" / \
    "snap-a4c99d5a49a64ffd"
SINGLE = "scn-07c647ec1459"  # single missed inflow: pays m0005 and m0009


@pytest.fixture(scope="module")
def store() -> SnapshotStore:
    return SnapshotStore(CANONICAL)


@pytest.fixture(scope="module")
def signer():
    return keypair()


@pytest.fixture
def db(monkeypatch):
    """A fresh database the route, the service and the re-entry all share."""
    from lce.data import database

    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "get_session_factory", lambda: factory)
    yield factory
    engine.dispose()


@pytest.fixture
def webhook_env(monkeypatch):
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", FAKE_WEBHOOK_ID)
    reset_paypal_settings_cache()
    yield
    reset_paypal_settings_cache()


@pytest.fixture
def client(store, signer, db, webhook_env, monkeypatch):
    from lce.api.app import create_app
    from lce.api.routers import webhooks as routes
    from lce.snapshot import store as store_module

    monkeypatch.setattr(store_module, "get_store", lambda *a, **k: store)
    app = create_app()
    _key, pem = signer
    app.dependency_overrides[routes.paypal_cert_loader] = lambda: (lambda _url: pem)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def executed(db, store):
    """The canonical plan, approved and executed against a fake PayPal."""
    fake = FakePayPal()
    with db() as s:
        svc = ExecutionService(s, store=store, settings=settings(),
                               client_factory=lambda st: fake.client(st))
        svc.map_identity("m0005", recipient_type="EMAIL", receiver="sb-m0005@personal.example.com")
        svc.map_identity("m0009", recipient_type="EMAIL", receiver="sb-m0009@personal.example.com")
        s.commit()
        plan = svc.plan(SINGLE)
        approval = svc.approve(SINGLE, approver="Approver One", plan_hash=plan.plan_hash)
        records = svc.execute(SINGLE, approval_id=approval.approval_id)
        by_merchant = {r.merchant_id: (r.record_id, r.idempotency_key, r.provider_batch_id)
                       for r in records}
    return fake, by_merchant


def _post(client, signer, event, *, webhook_id=FAKE_WEBHOOK_ID, body=None, headers=None):
    key, _pem = signer
    raw = body if body is not None else json.dumps(event).encode()
    h = headers if headers is not None else signed_headers(key, raw, webhook_id)
    return client.post("/api/v1/webhooks/paypal", content=raw, headers=h)


def _record(db, record_id):
    with db() as s:
        return s.execute(select(ExecutionRecordRow).where(
            ExecutionRecordRow.record_id == record_id)).scalar_one()


def _count(db, model):
    with db() as s:
        return len(list(s.execute(select(model)).scalars()))


# ------------------------------------------------------------------- receiver


class TestReceiver:
    def test_a_genuine_event_is_recorded_and_applied(self, client, signer, executed, db):
        _fake, recs = executed
        record_id, key, batch = recs["m0005"]
        response = _post(client, signer, item_event("SUCCEEDED", sender_item_id=key,
                                                    batch_id=batch))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "accepted" and body["applied"] is True
        assert body["record_status"] == "succeeded" and body["reentry_scheduled"] is False
        assert _record(db, record_id).status == "succeeded"
        with db() as s:
            row = s.execute(select(PayPalWebhookEventRow)).scalar_one()
            assert row.verification["verified"] is True
            assert row.transmission["paypal-auth-algo"] == "SHA256withRSA"
            assert len(row.raw_sha256) == 64 and row.processed

    def test_a_tampered_body_is_refused_and_nothing_is_recorded(self, client, signer, executed,
                                                                db):
        _fake, recs = executed
        key, _pem = signer
        event = item_event("FAILED", sender_item_id=recs["m0005"][1])
        raw = json.dumps(event).encode()
        headers = signed_headers(key, raw)
        tampered = raw.replace(b"FAILED", b"SUCCEEDED")
        response = _post(client, signer, None, body=tampered, headers=headers)
        assert response.status_code == 400
        assert _count(db, PayPalWebhookEventRow) == 0
        assert _record(db, recs["m0005"][0]).status == "pending"

    def test_a_delivery_for_another_listener_is_refused(self, client, signer, executed, db):
        _fake, recs = executed
        response = _post(client, signer, item_event("FAILED", sender_item_id=recs["m0005"][1]),
                         webhook_id="WH-SOMEONE-ELSE")
        assert response.status_code == 400
        assert _count(db, PayPalWebhookEventRow) == 0

    def test_missing_headers_are_refused(self, client, signer, executed, db):
        key, _pem = signer
        event = item_event("FAILED", sender_item_id=executed[1]["m0005"][1])
        raw = json.dumps(event).encode()
        headers = signed_headers(key, raw)
        del headers["PAYPAL-TRANSMISSION-SIG"]
        response = _post(client, signer, None, body=raw, headers=headers)
        assert response.status_code == 400 and "missing headers" in response.text
        assert _count(db, PayPalWebhookEventRow) == 0

    def test_a_replayed_event_is_acknowledged_once(self, client, signer, executed, db):
        _fake, recs = executed
        event = item_event("SUCCEEDED", sender_item_id=recs["m0005"][1], event_id="WH-ONCE")
        first = _post(client, signer, event).json()
        second = _post(client, signer, event).json()
        assert first["status"] == "accepted" and second["status"] == "duplicate"
        assert _count(db, PayPalWebhookEventRow) == 1

    def test_out_of_order_delivery_never_regresses(self, client, signer, executed, db):
        _fake, recs = executed
        record_id, key, _batch = recs["m0005"]
        _post(client, signer, item_event("SUCCEEDED", sender_item_id=key))
        late = _post(client, signer, item_event("HELD", sender_item_id=key)).json()
        assert late["applied"] is False and "already succeeded" in late["reason"]
        assert late["reentry_scheduled"] is False
        assert _record(db, record_id).status == "succeeded"

    def test_an_unknown_type_is_recorded_and_ignored(self, client, signer, db):
        event = {"id": "WH-OTHER-1", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {}}
        body = _post(client, signer, event).json()
        assert body["status"] == "ignored"
        with db() as s:
            row = s.execute(select(PayPalWebhookEventRow)).scalar_one()
            assert row.processed and "ignored" in row.outcome

    def test_no_webhook_id_configured_refuses_everything(self, client, signer, monkeypatch, db):
        monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "")
        reset_paypal_settings_cache()
        response = _post(client, signer, {"id": "WH-X", "event_type": "X", "resource": {}})
        assert response.status_code == 503
        assert _count(db, PayPalWebhookEventRow) == 0

    def test_simulator_events_only_when_enabled(self, client, signer, monkeypatch):
        event = {"id": "WH-SIM-1", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {}}
        refused = _post(client, signer, event, webhook_id="WEBHOOK_ID")
        assert refused.status_code == 400
        monkeypatch.setenv("PAYPAL_ACCEPT_SIMULATOR_EVENTS", "true")
        reset_paypal_settings_cache()
        accepted = _post(client, signer, event, webhook_id="WEBHOOK_ID")
        assert accepted.status_code == 200


class TestConcurrentDelivery:
    def test_concurrent_duplicates_record_one_event(self, tmp_path):
        """Eight threads deliver one event at once: one row, one 'accepted'."""
        engine = create_engine(f"sqlite:///{(tmp_path / 'c.sqlite').as_posix()}",
                               connect_args={"timeout": 30, "check_same_thread": False})
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        event = {"id": "WH-RACE", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {}}
        raw = json.dumps(event).encode()
        barrier = threading.Barrier(8)
        results: list[str] = []
        lock = threading.Lock()

        def deliver():
            barrier.wait()
            with factory() as s:
                r = ingest(s, event=event, raw_body=raw, headers={},
                           verification=Verification(True, "verified"))
            with lock:
                results.append(r.status)

        threads = [threading.Thread(target=deliver) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results).count("ignored") == 1
        assert results.count("duplicate") == 7
        with factory() as s:
            assert len(list(s.execute(select(PayPalWebhookEventRow)).scalars())) == 1
        engine.dispose()


# -------------------------------------------------------------- state machine


INCOMING = ["unknown", "submitted", "pending", "held", "succeeded", "failed"]


def _run(start, events):
    status, path = start, [start]
    for e in events:
        status = advance(status, e).status
        path.append(status)
    return status, path


class TestStateMachineProperties:
    """Every sequence of up to five events, from every starting state."""

    @staticmethod
    def _sequences():
        for n in range(1, 6):
            yield from itertools.product(INCOMING, repeat=n)

    def test_rank_never_decreases_and_terminal_absorbs(self):
        for start in ["unknown", "submitted", "pending", "held"]:
            for seq in self._sequences():
                _final, path = _run(start, seq)
                ranks = [RANK[s] for s in path]
                assert ranks == sorted(ranks), (start, seq, path)
                for i, s in enumerate(path):
                    if s in TERMINAL:
                        assert all(p == s for p in path[i:]), (start, seq, path)

    def test_duplicates_are_harmless(self):
        for start in ["unknown", "pending"]:
            for seq in self._sequences():
                doubled = [e for e in seq for _ in (0, 1)]
                assert _run(start, seq)[0] == _run(start, doubled)[0], (start, seq)

    def test_the_outcome_is_the_first_terminal_fact_or_the_highest_rank(self):
        for seq in self._sequences():
            final, _ = _run("unknown", seq)
            terminal = [e for e in seq if e in TERMINAL]
            if terminal:
                assert final == terminal[0], seq
            else:
                assert RANK[final] == max([0, *(RANK[e] for e in seq)]), seq

    def test_without_contradiction_order_does_not_matter(self):
        events = ["submitted", "pending", "held", "failed"]
        finals = {_run("unknown", p)[0] for p in itertools.permutations(events)}
        assert finals == {"failed"}

    def test_planned_never_moves(self):
        for e in INCOMING:
            assert advance("planned", e).status == "planned"


# ----------------------------------------------------------------- re-entry


class TestShockMapping:
    def test_the_shock_is_sized_by_the_failed_payouts_analytic_amount(self, db, store,
                                                                      executed):
        _fake, recs = executed
        record = _record(db, recs["m0005"][0])
        shock = failed_payout_shock(record, store, trigger="UNCLAIMED", source_event_id="WH-1")
        assert shock.merchant_id == "m0005" and shock.kind == "failed_payout"
        assert shock.missing_inr == float(record.analytic_amount_inr) == 77269030.83519812
        slack = max(_liquidity_slack(store, "m0005"), 1.0)
        assert shock.magnitude_multiple == pytest.approx(shock.missing_inr / slack)
        assert shock.onset_hours == 0.0

    def test_a_merchant_outside_the_live_subset_is_not_analysed(self, db, store, executed):
        from lce.data.orm import PayPalIdentityRow

        _fake, recs = executed
        with db() as s:
            s.query(PayPalIdentityRow).filter_by(merchant_id="m0009").delete()
            s.commit()
            record = s.execute(select(ExecutionRecordRow).where(
                ExecutionRecordRow.record_id == recs["m0009"][0])).scalar_one()
            row = reenter(s, store, record, trigger="FAILED")
            assert row.status == "skipped" and "live subset" in row.reason
            assert row.result == {}

    def test_a_receiver_that_does_not_match_is_not_attributed(self, db, store, executed):
        _fake, recs = executed
        with db() as s:
            record = s.execute(select(ExecutionRecordRow).where(
                ExecutionRecordRow.record_id == recs["m0005"][0])).scalar_one()
            row = reenter(s, store, record, trigger="FAILED", receiver="stranger@example.com")
            assert row.status == "skipped" and "does not match" in row.reason

    def test_one_analysis_per_record_and_trigger(self, db, store, executed):
        _fake, recs = executed
        with db() as s:
            record = s.execute(select(ExecutionRecordRow).where(
                ExecutionRecordRow.record_id == recs["m0005"][0])).scalar_one()
            first = reenter(s, store, record, trigger="UNCLAIMED")
            second = reenter(s, store, record, trigger="UNCLAIMED")
            assert first.analysis_id == second.analysis_id
        assert _count(db, ReentryAnalysisRow) == 1


class TestFullFailurePath:
    def test_failed_payout_to_shock_to_bounded_analysis_to_new_plan(
        self, client, signer, executed, db
    ):
        fake, recs = executed
        record_id, key, batch = recs["m0005"]
        posts_before = len(fake.payout_posts)

        response = _post(client, signer, item_event(
            "UNCLAIMED", sender_item_id=key, batch_id=batch,
            receiver="sb-m0005@personal.example.com"))
        body = response.json()
        assert body["record_status"] == "failed" and body["reentry_scheduled"] is True

        # The analysis ran after the response (TestClient runs background tasks).
        rows = client.get("/api/v1/execution/reentries",
                          params={"scenario_id": SINGLE}).json()
        assert len(rows) == 1
        row = rows[0]
        assert row["record_id"] == record_id and row["trigger"] == "UNCLAIMED"
        assert row["status"] == "analysed" and row["merchant_id"] == "m0005"
        assert row["missing_inr"] == "77269030.83519812"
        assert row["snapshot_id"] == "snap-a4c99d5a49a64ffd"
        assert row["computed_in_ms"] > 0
        assert row["adhoc_scenario_id"].startswith("adhoc-")
        assert row["new_plan"], "the bounded analysis proposes a new plan"

        # Nothing was paid: the new plan needs its own approval.
        assert len(fake.payout_posts) == posts_before
        with db() as s:
            stored = s.execute(select(ReentryAnalysisRow)).scalar_one()
            assert stored.provenance["kind"] == "computed_now"
            assert stored.provenance["limits"] == {"max_origins": 5, "max_candidates": 12,
                                                   "max_actions": 2}
            assert stored.result["recommended_intervention"] is not None

        # A redelivery of the same failure does not analyse twice.
        _post(client, signer, item_event("UNCLAIMED", sender_item_id=key, batch_id=batch,
                                         receiver="sb-m0005@personal.example.com"))
        assert _count(db, ReentryAnalysisRow) == 1
