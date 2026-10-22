"""Test doubles for the PayPal sandbox. No network.

``FakePayPal`` is an ``httpx.MockTransport`` handler that behaves like the parts
of the real API Keystone depends on - including the parts that make double
payment possible if the client gets them wrong:

* ``PayPal-Request-Id`` idempotency: the same id is answered from the stored
  response, it does not create a second payout;
* ``sender_batch_id`` uniqueness: reusing one under a *different* request id is
  rejected, as PayPal does for 30 days;
* a ``"drop_response"`` failure mode in which the payout **is created** but the
  answer never reaches the client - the case where a naive retry pays twice.

``ExtractStore`` serves the scenarios extracted from the canonical snapshot
(``fixtures/paypal/canonical_extract.json``) through the same methods the
execution service calls on a real ``SnapshotStore``.
"""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any

import httpx

from lce.config import PayPalSettings
from lce.errors import NotFoundError
from lce.paypal.client import PROBE_BATCH_ID, PayPalClient
from lce.snapshot.models import MerchantView, ScenarioSnapshot

FIXTURES = Path(__file__).parent / "fixtures" / "paypal"

#: Deliberately recognisable so a leak is easy to find in any output.
FAKE_CLIENT_ID = "KS-TEST-CLIENT-ID-c0ffee"
FAKE_SECRET = "KS-TEST-SECRET-must-never-appear-7e3a"
FAKE_TOKEN = "KS-TEST-ACCESS-TOKEN-must-never-appear-91b2"
FAKE_WEBHOOK_ID = "WH-KS-TEST-0001"


def settings(**overrides: Any) -> PayPalSettings:
    values: dict[str, Any] = {
        "PAYPAL_CLIENT_ID": FAKE_CLIENT_ID,
        "PAYPAL_CLIENT_SECRET": FAKE_SECRET,
        "PAYPAL_WEBHOOK_ID": FAKE_WEBHOOK_ID,
        **overrides,
    }
    return PayPalSettings(_env_file=None, **values)


class FakePayPal:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.payout_posts: list[httpx.Request] = []
        self.batches: dict[str, dict[str, Any]] = {}
        self.by_request_id: dict[str, tuple[int, dict[str, Any]]] = {}
        self.sender_batch_ids: set[str] = set()
        self.item_status = "SUCCESS"
        self.batch_status_on_create = "PENDING"
        self.fail_next_post: str | tuple[int, dict[str, Any]] | None = None
        self.probe_status = 404
        self.token_status = 200

    # -------------------------------------------------------------- transport

    def client(self, s: PayPalSettings | None = None) -> PayPalClient:
        s = s or settings()
        http = httpx.Client(transport=httpx.MockTransport(self.handler), base_url=s.api_base)
        return PayPalClient(s, client=http)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == "/v1/oauth2/token":
            return self._token(request)
        if request.headers.get("authorization") != f"Bearer {FAKE_TOKEN}":
            return _error(401, "AUTHENTICATION_FAILURE", "bad token")
        if path == "/v1/payments/payouts" and request.method == "POST":
            return self._create_payout(request)
        if path.startswith("/v1/payments/payouts/") and request.method == "GET":
            return self._get_payout(path.rsplit("/", 1)[-1])
        if path == "/v2/invoicing/invoices":
            return httpx.Response(200, json={"items": [], "links": []})
        if path == "/v1/notifications/webhooks":
            return httpx.Response(200, json={"webhooks": []})
        return _error(404, "RESOURCE_NOT_FOUND", f"no route {path}")

    def _token(self, request: httpx.Request) -> httpx.Response:
        expected = base64.b64encode(f"{FAKE_CLIENT_ID}:{FAKE_SECRET}".encode()).decode()
        if self.token_status != 200 or request.headers.get("authorization") != f"Basic {expected}":
            return httpx.Response(401, json={"error": "invalid_client",
                                             "error_description": "Client Authentication failed"})
        return httpx.Response(200, json={"access_token": FAKE_TOKEN, "token_type": "Bearer",
                                         "expires_in": 32400})

    def _create_payout(self, request: httpx.Request) -> httpx.Response:
        self.payout_posts.append(request)
        request_id = request.headers.get("paypal-request-id", "")
        if request_id and request_id in self.by_request_id:
            status, body = self.by_request_id[request_id]
            return httpx.Response(status, json=body)

        failure, self.fail_next_post = self.fail_next_post, None
        if failure == "transport":
            raise httpx.ConnectError("connection reset", request=request)
        if isinstance(failure, tuple):
            status, body = failure
            return httpx.Response(status, json=body, headers={"paypal-debug-id": body.get(
                "debug_id", "")})

        body = json.loads(request.content)
        header = body["sender_batch_header"]
        if header["sender_batch_id"] in self.sender_batch_ids:
            return _error(400, "USER_BUSINESS_ERROR", "sender_batch_id was used before")
        self.sender_batch_ids.add(header["sender_batch_id"])
        batch_id = "FAKEBATCH" + uuid.uuid4().hex[:12].upper()
        self.batches[batch_id] = {"header": header, "items": body["items"]}
        answer = {
            "batch_header": {
                "payout_batch_id": batch_id,
                "batch_status": self.batch_status_on_create,
                "sender_batch_header": header,
            },
            "links": [],
        }
        if request_id:
            self.by_request_id[request_id] = (201, answer)
        if failure == "drop_response":
            # Created on PayPal's side; the client never hears about it.
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(201, json=answer)

    def _get_payout(self, batch_id: str) -> httpx.Response:
        if batch_id == PROBE_BATCH_ID:
            return _error(self.probe_status, "RESOURCE_NOT_FOUND"
                          if self.probe_status == 404 else "NOT_AUTHORIZED", "probe")
        batch = self.batches.get(batch_id)
        if batch is None:
            return _error(404, "RESOURCE_NOT_FOUND", "no such batch")
        items = [
            {
                "payout_item_id": f"ITEM{batch_id[-8:]}{i}",
                "payout_batch_id": batch_id,
                "transaction_status": self.item_status,
                "payout_item": item,
            }
            for i, item in enumerate(batch["items"])
        ]
        return httpx.Response(200, json={
            "batch_header": {"payout_batch_id": batch_id, "batch_status": "SUCCESS",
                             "sender_batch_header": batch["header"]},
            "items": items,
        })

    def failing_drop_response(self) -> None:
        self.fail_next_post = "drop_response"


def _error(status: int, name: str, message: str) -> httpx.Response:
    debug_id = "dbg" + uuid.uuid4().hex[:10]
    return httpx.Response(status, json={"name": name, "message": message, "debug_id": debug_id},
                          headers={"paypal-debug-id": debug_id})


class ExtractStore:
    """The slice of ``SnapshotStore`` the execution service uses, from real data."""

    def __init__(self, path: Path = FIXTURES / "canonical_extract.json") -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        self.snapshot_id: str = data["snapshot_id"]
        self._scenarios = {
            s["scenario_id"]: ScenarioSnapshot.model_validate(s) for s in data["scenarios"]
        }
        self._merchants = [MerchantView.model_validate(m) for m in data["merchants"]]

    def scenario(self, scenario_id: str) -> ScenarioSnapshot:
        try:
            return self._scenarios[scenario_id]
        except KeyError:
            raise NotFoundError("no such scenario", scenario_id=scenario_id) from None

    def scenarios(self) -> list[ScenarioSnapshot]:
        return list(self._scenarios.values())

    def merchants(self) -> list[MerchantView]:
        return self._merchants

    def by_family(self, family: str) -> ScenarioSnapshot:
        return next(s for s in self._scenarios.values() if s.family == family)


# ------------------------------------------------------------ webhook signing

def keypair(*, valid_days: int = 30, start_days_ago: int = 1):
    """An RSA key and a self-signed certificate, PEM-encoded."""
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "keystone-test-signer")])
    start = datetime.now(UTC) - timedelta(days=start_days_ago)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(start).not_valid_after(start + timedelta(days=valid_days))
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM)


CERT_URL = "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-KS-TEST"


def signed_headers(key, body: bytes, webhook_id: str = FAKE_WEBHOOK_ID,
                   transmission_id: str | None = None) -> dict[str, str]:
    """Headers for one delivery, signed the way PayPal signs."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    from lce.paypal.webhooks import signed_message

    tid = transmission_id or f"TX-{uuid.uuid4().hex[:12]}"
    ttime = "2026-10-22T10:00:00Z"
    sig = key.sign(signed_message(tid, ttime, webhook_id, body), padding.PKCS1v15(),
                   hashes.SHA256())
    return {
        "PAYPAL-TRANSMISSION-ID": tid,
        "PAYPAL-TRANSMISSION-TIME": ttime,
        "PAYPAL-TRANSMISSION-SIG": base64.b64encode(sig).decode(),
        "PAYPAL-CERT-URL": CERT_URL,
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "Content-Type": "application/json",
    }


def item_event(outcome: str, *, sender_item_id: str, batch_id: str | None = None,
               receiver: str | None = None, event_id: str | None = None) -> dict:
    """A PAYMENT.PAYOUTS-ITEM.<outcome> event as PayPal shapes it."""
    status = {"SUCCEEDED": "SUCCESS", "HELD": "ONHOLD"}.get(outcome, outcome)
    return {
        "id": event_id or f"WH-{uuid.uuid4().hex[:16]}",
        "event_type": f"PAYMENT.PAYOUTS-ITEM.{outcome}",
        "resource": {
            "payout_item_id": f"ITEM-{sender_item_id[-8:]}",
            "payout_batch_id": batch_id,
            "transaction_status": status,
            "payout_item": {"sender_item_id": sender_item_id, "receiver": receiver,
                            "recipient_type": "EMAIL",
                            "amount": {"currency": "USD", "value": "1.00"}},
        },
    }
