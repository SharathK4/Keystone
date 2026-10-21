"""P1 - the contract with PayPal: request bodies, money on the wire, idempotency.

Every Payouts request Keystone would send is validated against PayPal's own
published OpenAPI schema (``fixtures/paypal/payments_payouts_batch_v1.json``,
Apache-2.0, pinned to a commit). The validator is checked to be *live* - it must
reject deliberately broken bodies - so a pass means something.
"""

from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_EVEN, Decimal

import jsonschema
import pytest

from lce.execution.plan import (
    PAYPAL_MAPPINGS,
    Recipient,
    build_plan,
    idempotency_key,
    plan_hash,
)
from lce.execution.service import payout_request
from lce.paypal.money import DemoScale, Money
from paypal_fakes import FIXTURES, ExtractStore

SPEC = json.loads((FIXTURES / "payments_payouts_batch_v1.json").read_text(encoding="utf-8"))
SCALE = DemoScale(Decimal("0.00001"), "USD")
RECIPIENTS = {
    "m0005": Recipient("EMAIL", "sb-merchant-m0005@personal.example.com", "m0005"),
    "m0009": Recipient("EMAIL", "sb-merchant-m0009@personal.example.com", "m0009"),
}


def _validator(component: str) -> jsonschema.Validator:
    """Validate against one component, resolving ``$ref`` inside PayPal's document."""
    root = dict(SPEC)
    root["$ref"] = f"#/components/schemas/{component}"
    return jsonschema.Draft4Validator(root)


def _param_schema(name: str) -> dict:
    return SPEC["components"]["parameters"][name]["schema"]


@pytest.fixture(scope="module")
def store() -> ExtractStore:
    return ExtractStore()


@pytest.fixture(scope="module")
def plans(store):
    built = [
        build_plan(s, snapshot_id=store.snapshot_id, provider="paypal_sandbox", scale=SCALE,
                   recipients=RECIPIENTS)
        for s in store.scenarios()
    ]
    return [p for p in built if p is not None]


def _payouts(plans):
    for plan in plans:
        for action in plan.actions:
            if action.executable:
                yield plan, action, payout_request(plan, action)


class TestAgainstPayPalsPublishedSchema:
    def test_the_validator_is_live(self):
        validator = _validator("create_payout_request")
        assert list(validator.iter_errors({})), "an empty body must fail"
        broken = {
            "sender_batch_header": {"sender_batch_id": "x"},
            "items": [{"amount": {"currency": "USD", "value": 7.5}, "receiver": "a@b.c"}],
        }
        assert list(validator.iter_errors(broken)), "a numeric value must fail"
        no_receiver = {
            "sender_batch_header": {"sender_batch_id": "x"},
            "items": [{"amount": {"currency": "USD", "value": "7.50"}}],
        }
        assert list(validator.iter_errors(no_receiver)), "a missing receiver must fail"

    def test_every_request_validates(self, plans):
        validator = _validator("create_payout_request")
        checked = 0
        for _plan, _action, prepared in _payouts(plans):
            errors = sorted(validator.iter_errors(prepared.body), key=str)
            assert not errors, [e.message for e in errors]
            checked += 1
        # single_missed_inflow pays two merchants, multi_node_shock pays one.
        assert checked == 3

    def test_request_id_fits_the_header_schema(self, plans):
        header = _param_schema("paypal_request_id")
        for _plan, _action, prepared in _payouts(plans):
            assert header["minLength"] <= len(prepared.request_id) <= header["maxLength"]
            assert re.fullmatch(header["pattern"], prepared.request_id)


class TestMoneyOnTheWire:
    def test_amounts_are_decimal_strings_with_a_currency(self, plans):
        for _plan, _action, prepared in _payouts(plans):
            amount = prepared.body["items"][0]["amount"]
            assert amount["currency"] == "USD"
            assert isinstance(amount["value"], str)
            assert re.fullmatch(r"\d+\.\d{2}", amount["value"])

    def test_executed_is_analytic_times_scale_by_the_stated_rule(self, plans):
        for _plan, action, prepared in _payouts(plans):
            exact = Decimal(repr(action.analytic_amount_inr)) * SCALE.rate
            expected = exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
            sent = Decimal(prepared.body["items"][0]["amount"]["value"])
            assert sent == expected
            assert abs(sent - exact) <= Decimal("0.005")

    def test_canonical_amounts(self, plans):
        """The two canonical payouts, at one dollar per rupee lakh."""
        single = next(p for p in plans if p.scenario_id.startswith("scn-07c6"))
        sent = {a.merchant_id: a.executed for a in single.actions}
        assert sent["m0005"] == Money.of("772.69", "USD")
        assert sent["m0009"] == Money.of("350.71", "USD")
        assert single.executed_total == Money.of("1123.40", "USD")

    def test_items_sum_to_the_plan_total(self, plans):
        for plan in plans:
            items = [a.executed for a in plan.actions if a.executable]
            total = Money.of(0, "USD")
            for m in items:
                total = total + m
            assert total == plan.executed_total

    def test_money_refuses_floats_and_excess_precision(self):
        with pytest.raises(TypeError):
            Money(772.69, "USD")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            Money(Decimal("772.691"), "USD")
        with pytest.raises(ValueError):
            DemoScale(Decimal("1"), "INR")


class TestIdempotencyFields:
    def test_one_key_carries_through_every_paypal_id(self, plans):
        for plan, action, prepared in _payouts(plans):
            key = idempotency_key(plan.plan_hash, action.key)
            assert prepared.request_id == key
            assert prepared.body["sender_batch_header"]["sender_batch_id"] == key
            assert prepared.body["items"][0]["sender_item_id"] == key[:63]

    def test_keys_are_unique_across_actions_and_plans(self, plans):
        keys = [prepared.request_id for _p, _a, prepared in _payouts(plans)]
        assert len(keys) == len(set(keys))

    def test_requests_are_deterministic(self, plans):
        """A resubmission must be byte-identical, or PayPal's dedupe cannot help."""
        for plan, action, prepared in _payouts(plans):
            again = payout_request(plan, action)
            assert json.dumps(again.body, sort_keys=True) == json.dumps(prepared.body,
                                                                        sort_keys=True)
            assert again.request_id == prepared.request_id


class TestThePlan:
    def test_plan_bearing_scenarios_are_complete(self, plans):
        assert plans, "the extract has plan-bearing scenarios"
        for plan in plans:
            assert plan.complete, plan.incomplete_reason

    def test_no_plan_no_execution(self, store):
        no_plan = store.by_family("supplier_failure")
        assert build_plan(no_plan, snapshot_id=store.snapshot_id, provider="paypal_sandbox",
                          scale=SCALE, recipients=RECIPIENTS) is None

    def test_unmappable_types_are_planned_with_a_reason(self, plans):
        multi = next(p for p in plans if any(a.action_type == "supplier_term_extension"
                                             for a in p.actions))
        term = next(a for a in multi.actions if a.action_type == "supplier_term_extension")
        assert term.mapping is None and not term.executable
        assert "recorded as a plan" in (term.not_executable_reason or "")
        assert set(PAYPAL_MAPPINGS) == {"liquidity_injection"}

    def test_hash_covers_everything_an_approver_sees(self, store):
        scenario = store.by_family("single_missed_inflow")

        def h(**kw):
            args = {"snapshot_id": store.snapshot_id, "provider": "paypal_sandbox",
                    "scale": SCALE, "recipients": RECIPIENTS, **kw}
            plan = build_plan(scenario, **args)
            assert plan is not None
            assert plan.plan_hash == plan_hash(plan)
            return plan.plan_hash

        base = h()
        assert h() == base
        assert h(scale=DemoScale(Decimal("0.00002"), "USD")) != base
        assert h(scale=DemoScale(Decimal("0.00001"), "EUR")) != base
        assert h(snapshot_id="snap-other") != base
        moved = dict(RECIPIENTS, m0005=Recipient("EMAIL", "someone-else@personal.example.com"))
        assert h(recipients=moved) != base
        assert h(recipients={"m0005": RECIPIENTS["m0005"]}) != base
