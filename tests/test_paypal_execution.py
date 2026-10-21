"""P2 - money invariants for approval, execution and reconciliation.

Each test runs against its own in-memory database and a fake PayPal, so every
count of "requests sent" and "payouts created" is exact.

The invariants:

* nothing is sent without an approval, and an approval is for one exact plan;
* an action is paid at most once - across repeats, races, lost responses and
  explicit resubmission - which is checked as the number of payouts the fake
  PayPal actually created, not just the number of records;
* the executed amount is the analytic amount times the declared scale, by the
  stated rounding rule;
* every submitted action can be reconciled to a PayPal object and status;
* credentials never appear in a record, an event, a log line or an error.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_EVEN, Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lce.data.orm import (
    Base,
    ExecutionApprovalRow,
    ExecutionEventRow,
    ExecutionRecordRow,
)
from lce.errors import ConfigError, ExecutionRefused, NotFoundError
from lce.execution.paypal import Capability, CapabilityReport, PayPalSandboxProvider
from lce.execution.service import ExecutionService
from lce.paypal.client import PayPalClient
from paypal_fakes import (
    FAKE_CLIENT_ID,
    FAKE_SECRET,
    FAKE_TOKEN,
    ExtractStore,
    FakePayPal,
    settings,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        yield s
    engine.dispose()


@pytest.fixture(scope="module")
def store() -> ExtractStore:
    return ExtractStore()


@pytest.fixture
def fake() -> FakePayPal:
    return FakePayPal()


def _service(session, store, fake, *, report=None, s=None) -> ExecutionService:
    s = s or settings()
    return ExecutionService(session, store=store, settings=s,
                            client_factory=lambda st: fake.client(st),
                            capability_report=report)


def _map_both(svc: ExecutionService) -> None:
    svc.map_identity("m0005", recipient_type="EMAIL",
                     receiver="sb-m0005@personal.example.com", label="m0005")
    svc.map_identity("m0009", recipient_type="EMAIL",
                     receiver="sb-m0009@personal.example.com", label="m0009")
    svc.session.commit()


@pytest.fixture
def single(store):
    return store.by_family("single_missed_inflow").scenario_id


@pytest.fixture
def multi(store):
    return store.by_family("multi_node_shock").scenario_id


def _approve(svc, scenario_id):
    plan = svc.plan(scenario_id)
    return svc.approve(scenario_id, approver="Approver One", plan_hash=plan.plan_hash)


def _rows(session, model):
    return list(session.execute(select(model)).scalars())


# ------------------------------------------------------------------- approval


class TestNothingMovesWithoutApproval:
    def test_execute_without_an_approval_is_refused(self, session, store, fake, single):
        svc = _service(session, store, fake)
        _map_both(svc)
        with pytest.raises(ExecutionRefused):
            svc.execute(single, approval_id="apr_does_not_exist")
        assert fake.payout_posts == []
        assert _rows(session, ExecutionRecordRow) == []

    def test_an_approval_is_for_one_exact_hash(self, session, store, fake, single):
        svc = _service(session, store, fake)
        _map_both(svc)
        with pytest.raises(ExecutionRefused):
            svc.approve(single, approver="Approver One", plan_hash="0" * 64)
        kinds = [e.kind for e in _rows(session, ExecutionEventRow)]
        assert "approval_refused" in kinds
        assert _rows(session, ExecutionApprovalRow) == []

    def test_changing_a_recipient_after_approval_invalidates_it(
        self, session, store, fake, single
    ):
        svc = _service(session, store, fake)
        _map_both(svc)
        approval = _approve(svc, single)
        svc.map_identity("m0005", recipient_type="EMAIL",
                         receiver="someone-else@personal.example.com")
        session.commit()
        with pytest.raises(ExecutionRefused):
            svc.execute(single, approval_id=approval.approval_id)
        assert fake.payout_posts == []

    def test_an_approval_does_not_carry_to_another_scenario(
        self, session, store, fake, single, multi
    ):
        svc = _service(session, store, fake)
        _map_both(svc)
        approval = _approve(svc, single)
        with pytest.raises(ExecutionRefused):
            svc.execute(multi, approval_id=approval.approval_id)
        assert fake.payout_posts == []

    def test_an_unmapped_paid_action_blocks_approval(self, session, store, fake, single):
        svc = _service(session, store, fake)
        svc.map_identity("m0005", recipient_type="EMAIL",
                         receiver="sb-m0005@personal.example.com")
        session.commit()
        with pytest.raises(ExecutionRefused) as refused:
            _approve(svc, single)
        assert refused.value.context["unmapped"] == ["m0009"]

    def test_an_incomplete_plan_cannot_be_approved(self, session, fake, single):
        store = ExtractStore()
        scenario = store.scenario(single)
        tampered = scenario.model_copy(update={"counterfactual": scenario.counterfactual
                                                .model_copy(update={"cost": 1.0})})
        store._scenarios[single] = tampered
        svc = _service(session, store, fake)
        _map_both(svc)
        plan = svc.plan(single)
        assert not plan.complete
        with pytest.raises(ExecutionRefused):
            svc.approve(single, approver="Approver One", plan_hash=plan.plan_hash)

    def test_a_scenario_with_no_plan_has_nothing_to_execute(self, session, store, fake):
        svc = _service(session, store, fake)
        with pytest.raises(NotFoundError):
            svc.plan(store.by_family("supplier_failure").scenario_id)


# --------------------------------------------------------------- at most once


class TestPaidAtMostOnce:
    def test_execute_sends_one_payout_per_action(self, session, store, fake, single):
        svc = _service(session, store, fake)
        _map_both(svc)
        approval = _approve(svc, single)
        records = svc.execute(single, approval_id=approval.approval_id)
        assert [r.status for r in records] == ["pending", "pending"]
        assert len(fake.payout_posts) == 2
        assert len(fake.batches) == 2
        assert all(r.provider_batch_id for r in records)

    def test_executing_again_sends_nothing(self, session, store, fake, single):
        svc = _service(session, store, fake)
        _map_both(svc)
        approval = _approve(svc, single)
        first = svc.execute(single, approval_id=approval.approval_id)
        second = svc.execute(single, approval_id=approval.approval_id)
        assert [r.record_id for r in second] == [r.record_id for r in first]
        assert len(fake.payout_posts) == 2 and len(fake.batches) == 2
        kinds = [e.kind for e in _rows(session, ExecutionEventRow)]
        assert kinds.count("duplicate_request_ignored") == 2

    def test_a_second_approval_of_the_same_plan_still_pays_once(
        self, session, store, fake, single
    ):
        svc = _service(session, store, fake)
        _map_both(svc)
        first = _approve(svc, single)
        second = _approve(svc, single)
        svc.execute(single, approval_id=first.approval_id)
        svc.execute(single, approval_id=second.approval_id)
        assert len(fake.batches) == 2
        assert len(_rows(session, ExecutionRecordRow)) == 2

    def test_a_lost_response_is_unknown_and_is_not_retried(self, session, store, fake, single):
        svc = _service(session, store, fake)
        _map_both(svc)
        approval = _approve(svc, single)
        fake.failing_drop_response()
        records = svc.execute(single, approval_id=approval.approval_id)
        statuses = sorted(r.status for r in records)
        assert statuses == ["pending", "unknown"]
        # PayPal created both payouts; the client saw one answer and one silence.
        assert len(fake.batches) == 2
        assert len(fake.payout_posts) == 2, "a write is never retried automatically"

    def test_reconciling_a_lost_response_does_not_pay_twice(self, session, store, fake, single):
        svc = _service(session, store, fake)
        _map_both(svc)
        approval = _approve(svc, single)
        fake.failing_drop_response()
        records = svc.execute(single, approval_id=approval.approval_id)
        unknown = next(r for r in records if r.status == "unknown")
        posts_before = list(fake.payout_posts)

        reconciled = svc.refresh(unknown.record_id)

        assert len(fake.batches) == 2, "PayPal answered from its record; no new payout"
        resent = fake.payout_posts[len(posts_before):][0]
        original = next(p for p in posts_before
                        if p.headers["paypal-request-id"] == unknown.idempotency_key)
        assert resent.headers["paypal-request-id"] == original.headers["paypal-request-id"]
        assert json.loads(resent.content) == json.loads(original.content)
        assert reconciled.provider_batch_id is not None
        assert reconciled.status == "pending"


# ------------------------------------------------------------------ amounts


class TestAmounts:
    def test_executed_is_analytic_times_scale(self, session, store, fake, single):
        s = settings()
        svc = _service(session, store, fake, s=s)
        _map_both(svc)
        approval = _approve(svc, single)
        for record in svc.execute(single, approval_id=approval.approval_id):
            exact = Decimal(record.analytic_amount_inr) * s.demo_scale
            expected = exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
            assert Decimal(record.executed_minor) / 100 == expected
            sent = json.loads(next(
                p.content for p in fake.payout_posts
                if p.headers["paypal-request-id"] == record.idempotency_key
            ))
            assert Decimal(sent["items"][0]["amount"]["value"]) == expected

    def test_the_approval_records_the_total_it_approved(self, session, store, fake, single):
        svc = _service(session, store, fake)
        _map_both(svc)
        approval = _approve(svc, single)
        assert approval.executed_total_minor == 112340  # USD 1,123.40
        assert approval.currency == "USD" and approval.demo_scale == "0.00001"


# ---------------------------------------------------- what is not executed


class TestRecordedAsPlan:
    def test_an_unmappable_action_is_recorded_not_sent(self, session, store, fake, multi):
        svc = _service(session, store, fake)
        _map_both(svc)
        approval = _approve(svc, multi)
        records = {r.action_type: r for r in svc.execute(multi, approval_id=approval.approval_id)}
        assert records["supplier_term_extension"].status == "planned"
        assert "no PayPal mapping" in records["supplier_term_extension"].reason
        assert records["liquidity_injection"].status == "pending"
        assert len(fake.payout_posts) == 1

    def test_no_payouts_capability_means_nothing_is_sent(self, session, store, fake, single):
        report = CapabilityReport(
            provider="paypal_sandbox", mode="sandbox", configured=True, refused=None,
            probed=True, probed_at=None,
            capabilities=(Capability("payouts", False, "not permitted for this app",
                                     "/v1/payments/payouts/x", 403, False),),
        )
        svc = _service(session, store, fake, report=lambda: report)
        _map_both(svc)
        approval = _approve(svc, single)
        records = svc.execute(single, approval_id=approval.approval_id)
        assert {r.status for r in records} == {"planned"}
        assert all("not permitted" in r.reason for r in records)
        assert fake.payout_posts == []

    def test_unconfigured_credentials_refuse_execution(self, session, store, fake, single):
        svc = _service(session, store, fake,
                       s=settings(PAYPAL_CLIENT_ID="", PAYPAL_CLIENT_SECRET=""))
        _map_both(svc)
        approval = _approve(svc, single)
        with pytest.raises(ExecutionRefused):
            svc.execute(single, approval_id=approval.approval_id)
        assert fake.calls == []


# ------------------------------------------------------------ reconciliation


class TestReconciliation:
    @pytest.mark.parametrize(
        ("paypal_status", "expected"),
        [("SUCCESS", "succeeded"), ("ONHOLD", "held"), ("PENDING", "pending"),
         ("UNCLAIMED", "failed"), ("RETURNED", "failed"), ("BLOCKED", "failed"),
         ("FAILED", "failed")],
    )
    def test_item_status_maps_onto_the_record(
        self, session, store, fake, single, paypal_status, expected
    ):
        svc = _service(session, store, fake)
        _map_both(svc)
        approval = _approve(svc, single)
        records = svc.execute(single, approval_id=approval.approval_id)
        fake.item_status = paypal_status
        for r in records:
            refreshed = svc.refresh(r.record_id)
            assert refreshed.status == expected
            assert refreshed.provider_batch_id and refreshed.provider_item_id
            assert refreshed.provider_status == paypal_status
            if expected == "failed":
                assert paypal_status.lower() in refreshed.reason

    def test_a_rejection_is_failed_with_paypals_diagnosis(self, session, store, fake, single):
        svc = _service(session, store, fake)
        _map_both(svc)
        approval = _approve(svc, single)
        fake.fail_next_post = (422, {"name": "UNPROCESSABLE_ENTITY",
                                     "message": "receiver unknown", "debug_id": "dbg42"})
        records = svc.execute(single, approval_id=approval.approval_id)
        failed = [r for r in records if r.status == "failed"]
        assert len(failed) == 1
        assert "UNPROCESSABLE_ENTITY" in failed[0].reason
        assert failed[0].debug_id == "dbg42"

    def test_the_ledger_tells_the_whole_story(self, session, store, fake, single):
        svc = _service(session, store, fake)
        _map_both(svc)
        approval = _approve(svc, single)
        records = svc.execute(single, approval_id=approval.approval_id)
        svc.refresh(records[0].record_id)
        kinds = [e.kind for e in svc.ledger(single)]
        assert kinds[0] == "approval_recorded"
        assert kinds.count("submission_started") == 2
        assert kinds.count("submitted") == 2
        assert "reconciled" in kinds


# ----------------------------------------------------------------- guardrails


class TestGuardrails:
    def test_live_mode_is_refused_at_construction(self):
        live = settings(PAYPAL_MODE="live")
        with pytest.raises(ConfigError):
            PayPalClient(live)
        with pytest.raises(ConfigError):
            PayPalSandboxProvider(live)

    def test_a_production_host_is_refused_even_in_sandbox_mode(self):
        prod = settings(PAYPAL_API_BASE="https://api-m.paypal.com")
        with pytest.raises(ConfigError):
            PayPalClient(prod)
        plain_http = settings(PAYPAL_API_BASE="http://api-m.sandbox.paypal.com")
        with pytest.raises(ConfigError):
            PayPalClient(plain_http)

    def test_the_generic_provider_interface_cannot_move_money(self, store):
        from lce.domain.enums import InterventionType
        from lce.domain.intervention import Intervention

        provider = PayPalSandboxProvider(settings())
        action = Intervention(type=InterventionType.LIQUIDITY_INJECTION, merchant_id="m0005",
                              t=0.0, amount=1000.0)
        assert provider.execute(action, dry_run=True).status == "planned"
        with pytest.raises(ExecutionRefused):
            provider.execute(action, dry_run=False)

    def test_credentials_never_leave(self, session, store, fake, single, capsys, caplog):
        svc = _service(session, store, fake)
        _map_both(svc)
        approval = _approve(svc, single)
        fake.fail_next_post = (500, {"name": "INTERNAL_SERVICE_ERROR", "message": "boom",
                                     "debug_id": "dbg1"})
        records = svc.execute(single, approval_id=approval.approval_id)
        svc.refresh(records[1].record_id)

        dumped = json.dumps(
            [
                {c.name: str(getattr(row, c.name)) for c in row.__table__.columns}
                for model in (ExecutionApprovalRow, ExecutionRecordRow, ExecutionEventRow)
                for row in _rows(session, model)
            ]
        )
        out = capsys.readouterr()
        haystack = dumped + out.out + out.err + caplog.text + repr(settings())
        for secret in (FAKE_SECRET, FAKE_TOKEN, FAKE_CLIENT_ID):
            assert secret not in haystack
