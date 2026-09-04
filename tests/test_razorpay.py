"""Razorpay integration: mocked by default, live only when opted into.

The default suite must pass on a machine with no credentials and no network, so
everything here except the final class is driven by a mock transport. That is not
a compromise - the mock is where the *contract* is tested: pagination, retry and
backoff behaviour, error mapping, signature verification, and the guarantee that
a secret never reaches a log line or a response body. Those are properties of our
code, and a live call would test Razorpay's uptime instead.

The live class is marked ``razorpay_live`` and deselected by ``addopts``. Run it
deliberately:

    pytest -m razorpay_live

It skips itself when no Test-Mode credentials are configured, and it only ever
*reads*. Nothing in this file can move money: there is no write call, and the
provider under test refuses live mode at construction.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from conftest import razorpay_credentials_present
from lce.config import RazorpayMode, RazorpaySettings
from lce.domain.enums import InterventionType
from lce.domain.intervention import Intervention
from lce.errors import ConfigError, RazorpayError, SignatureVerificationError
from lce.execution.providers import RazorpayTestProvider, _endpoint_available
from lce.razorpay.client import RazorpayClient, RetryPolicy

TEST_KEY = "rzp_test_mocked000000"
TEST_SECRET = "mock-secret-value-000"


def _settings(**overrides) -> RazorpaySettings:
    payload = {
        "RAZORPAY_KEY_ID": TEST_KEY,
        "RAZORPAY_KEY_SECRET": TEST_SECRET,
        "RAZORPAY_WEBHOOK_SECRET": "mock-webhook-secret",
        "RAZORPAY_MODE": RazorpayMode.TEST,
    }
    payload.update(overrides)
    return RazorpaySettings(**payload)


def _client(handler, *, settings: RazorpaySettings | None = None, retry=None) -> RazorpayClient:
    """A client whose transport is a callable, so no socket is ever opened."""
    cfg = settings or _settings()
    return RazorpayClient(
        cfg,
        client=httpx.Client(
            base_url=cfg.api_base_url,
            transport=httpx.MockTransport(handler),
            auth=(cfg.key_id, cfg.key_secret.get_secret_value()),
        ),
        retry=retry,
        sleep=lambda _seconds: None,  # never actually wait in a test
    )


# --------------------------------------------------------------- the contract


class TestClientContract:
    def test_a_successful_read_returns_the_payload(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/payments")
            return httpx.Response(200, json={"entity": "collection", "count": 0, "items": []})

        with _client(handler) as client:
            assert client.health()["status"] == "ok"

    def test_pagination_walks_until_the_page_is_short(self):
        pages = [
            {"items": [{"id": f"pay_{i}"} for i in range(100)], "count": 100},
            {"items": [{"id": "pay_last"}], "count": 1},
        ]
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            skip = int(request.url.params.get("skip", 0))
            seen.append(skip)
            return httpx.Response(200, json=pages[0] if skip == 0 else pages[1])

        with _client(handler) as client:
            items = client.list_payments(max_items=200)
        assert len(items) == 101
        assert seen[:2] == [0, 100]

    def test_max_items_bounds_the_walk(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"items": [{"id": f"pay_{i}"} for i in range(100)], "count": 100}
            )

        with _client(handler) as client:
            items = client.list_payments(max_items=150)
        assert len(items) <= 200  # never unbounded

    def test_a_retryable_status_is_retried_then_succeeds(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, json={"error": {"description": "try later"}})
            return httpx.Response(200, json={"items": [], "count": 0})

        client = _client(
            handler,
            retry=RetryPolicy(max_attempts=3, base_delay_seconds=0.0, max_delay_seconds=0.0),
        )
        with client:
            assert client.health()["status"] == "ok"
        assert calls["n"] == 2

    def test_a_client_error_is_not_retried(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(400, json={"error": {"description": "bad request"}})

        with _client(handler) as client, pytest.raises(RazorpayError):
            client.list_orders(max_items=1)
        assert calls["n"] == 1

    def test_health_reports_an_error_rather_than_raising(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"description": "unauthorised"}})

        with _client(handler) as client:
            health = client.health()
        assert health["status"] == "error"
        # Whatever went wrong, the credentials must not be in the report.
        assert TEST_SECRET not in json.dumps(health)

    def test_an_unconfigured_client_reports_rather_than_failing(self):
        client = RazorpayClient(_settings(RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET=""))
        assert client.health()["status"] == "not_configured"


# ------------------------------------------------------------------- secrets


class TestSecretsStayPut:
    def test_settings_repr_redacts_the_secret(self):
        blob = repr(_settings())
        assert TEST_SECRET not in blob
        assert "mock-webhook-secret" not in blob

    def test_safe_dump_never_carries_credentials(self):
        from lce.config import get_settings

        blob = json.dumps(get_settings().safe_dump(), default=str)
        assert "key_secret" not in blob
        assert "rzp_test_" not in blob or "razorpay_configured" in blob

    def test_provider_records_carry_no_credentials(self):
        provider = RazorpayTestProvider(settings=_settings())
        action = Intervention(
            type=InterventionType.LIQUIDITY_INJECTION,
            merchant_id="m0", t=0.0, amount=1000.0,
        )
        blob = json.dumps(provider.execute(action).to_dict(), default=str)
        assert TEST_SECRET not in blob
        assert TEST_KEY not in blob


# ----------------------------------------------------------------- webhooks


class TestWebhookSignature:
    def _signed(self, body: bytes, secret: str) -> str:
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_a_valid_signature_is_accepted(self):
        from lce.razorpay.webhooks import require_signature, verify_signature

        body = b'{"event":"payment.captured"}'
        signature = self._signed(body, "mock-webhook-secret")
        assert verify_signature(body, signature, "mock-webhook-secret")
        require_signature(body, signature, "mock-webhook-secret")

    def test_a_tampered_body_is_rejected(self):
        from lce.razorpay.webhooks import require_signature

        body = b'{"event":"payment.captured"}'
        signature = self._signed(body, "mock-webhook-secret")
        with pytest.raises(SignatureVerificationError):
            require_signature(b'{"event":"payment.failed"}', signature, "mock-webhook-secret")

    def test_a_wrong_secret_is_rejected(self):
        from lce.razorpay.webhooks import require_signature

        body = b'{"event":"payment.captured"}'
        with pytest.raises(SignatureVerificationError):
            require_signature(body, self._signed(body, "other"), "mock-webhook-secret")


# -------------------------------------------------------------- capabilities


class TestCapabilityProbe:
    def test_route_absent_is_reported_as_absent(self):
        """A 400 from /transfers means Route is off, whatever is configured."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith(("/transfers", "/accounts")):
                return httpx.Response(400, json={"error": {"description": "not enabled"}})
            return httpx.Response(200, json={"items": [], "count": 0})

        provider = RazorpayTestProvider(
            settings=_settings(RAZORPAY_ACCOUNT_ID="acc_configured_anyway")
        )
        with _client(handler, settings=provider.config) as client:
            capabilities = {
                name: _endpoint_available(client, path)
                for name, path in provider._PROBE_ENDPOINTS
            }
        assert capabilities["payments"] is True
        assert capabilities["transfers"] is False
        assert capabilities["route_accounts"] is False

    def test_an_action_needing_an_absent_capability_is_planned_not_executed(self):
        provider = RazorpayTestProvider(settings=_settings())
        provider._capabilities = {
            "api_reachable": True, "payments": True, "orders": True,
            "settlements": True, "transfers": False, "route_accounts": False,
            "credit_ledger": False, "term_ledger": False,
        }
        action = Intervention(
            type=InterventionType.LIQUIDITY_INJECTION,
            merchant_id="m0", t=0.0, amount=1000.0,
        )
        record = provider.execute(action)
        assert record.status == "planned"
        assert "transfers" in record.detail["reason"]

    def test_unconfigured_credentials_probe_to_nothing(self):
        provider = RazorpayTestProvider(
            settings=_settings(RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET="")
        )
        assert not any(provider.capabilities().values())

    def test_live_mode_is_refused_at_construction(self):
        with pytest.raises(ConfigError, match="Test Mode"):
            RazorpayTestProvider(settings=_settings(RAZORPAY_MODE=RazorpayMode.LIVE))


# ------------------------------------------------------------- live, opt-in


@pytest.mark.razorpay_live
@pytest.mark.skipif(
    not razorpay_credentials_present(),
    reason="set RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET to run the live probe",
)
class TestLiveTestMode:
    """Reads against the real Test Mode API. Never writes."""

    def test_connectivity(self):
        from lce.config import get_settings

        with RazorpayClient(get_settings().razorpay) as client:
            health = client.health()
        assert health["status"] == "ok"
        assert health["mode"] == "test"

    def test_capabilities_are_probed_not_assumed(self):
        capabilities = RazorpayTestProvider().capabilities(refresh=True)
        assert capabilities["api_reachable"] is True
        # No assertion on Route: whether it is enabled is an account fact, and
        # asserting either way would make this test a statement about the
        # account rather than about the probe.
        assert set(capabilities) >= {"payments", "orders", "transfers", "route_accounts"}

    def test_reads_do_not_mutate_the_account(self):
        from lce.config import get_settings

        with RazorpayClient(get_settings().razorpay) as client:
            before = len(client.list_payments(max_items=5))
            after = len(client.list_payments(max_items=5))
        assert before == after
