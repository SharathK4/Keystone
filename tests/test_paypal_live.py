"""Live PayPal sandbox checks - the spikes. Opt-in only: ``-m paypal_live``.

These call the real sandbox with the credentials in ``.env``. They are excluded
from the default run by ``addopts``, and in the default run ``conftest.py``
blanks every ``PAYPAL_*`` credential, so they cannot run by accident.

What each establishes (record the outcome in ``reports/paypal/spikes.md``):

* **S1** - the client-credentials token flow works for this app.
* **S2** - a USD payout to a sandbox personal account is accepted, its batch can
  be read back, and the item reaches a status this system maps. Needs
  ``PAYPAL_SPIKE_RECEIVER`` (a sandbox *personal* account email).
* **S2b** - resubmitting the identical request with the same
  ``PayPal-Request-Id`` never creates a second batch. Observed on Nov 11: the
  sandbox refuses the duplicate (400 ``USER_BUSINESS_ERROR``) rather than
  returning the first batch; either answer means the plan cannot pay twice.
* **S3** - an invoice can be created, sent and read; and whether its due date can
  be changed after sending (this decides how term extension would be mapped).
  Needs ``PAYPAL_SPIKE_RECEIVER`` too.
* **probe** - the capability probe's readings, including whether a missing
  payout batch really answers 404 (the probe's assumption).

Amounts are one US dollar. Run:

    pytest -m paypal_live tests/test_paypal_live.py -rA
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from lce.config import PayPalSettings
from lce.execution.paypal import PayPalSandboxProvider
from lce.paypal.client import PayPalClient

pytestmark = pytest.mark.paypal_live

RECEIVER = os.environ.get("PAYPAL_SPIKE_RECEIVER", "")


@pytest.fixture(scope="module")
def cfg() -> PayPalSettings:
    settings = PayPalSettings()
    if not settings.configured:
        pytest.skip("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not configured")
    settings.require_sandbox()
    return settings


@pytest.fixture(scope="module")
def client(cfg):
    with PayPalClient(cfg) as c:
        yield c


def _need_receiver() -> None:
    if not RECEIVER:
        pytest.skip("set PAYPAL_SPIKE_RECEIVER to a sandbox personal account email")


def test_s1_token(client):
    result = client.probe_token()
    assert result.available is True, result.reason


def test_probe_readings(cfg):
    report = PayPalSandboxProvider(cfg).report(probe=True)
    readings = {c.name: (c.available, c.http_status, c.reason) for c in report.capabilities}
    print("capabilities:", readings)
    assert readings["api_reachable"][0] is True
    assert readings["payouts"][1] in (404, 401, 403), (
        "the payouts probe assumes a missing batch answers 404 (or 401/403 if not permitted)"
    )


def _payout_body(sender_batch_id: str) -> dict:
    return {
        "sender_batch_header": {
            "sender_batch_id": sender_batch_id,
            "recipient_type": "EMAIL",
            "email_subject": "Keystone spike S2",
        },
        "items": [{
            "recipient_type": "EMAIL",
            "amount": {"currency": "USD", "value": "1.00"},
            "receiver": RECEIVER,
            "sender_item_id": sender_batch_id[:63],
            "note": "Keystone sandbox spike S2",
        }],
    }


def test_s2_payout_create_and_read(client):
    _need_receiver()
    key = f"ks-spike-{uuid.uuid4().hex[:20]}"
    created = client.create_payout(_payout_body(key), request_id=key)
    batch_id = created["batch_header"]["payout_batch_id"]
    print("S2 batch:", batch_id, created["batch_header"]["batch_status"])
    batch = client.get_payout(batch_id)
    statuses = [i["transaction_status"] for i in batch.get("items", [])]
    print("S2 item statuses:", statuses)
    assert batch["batch_header"]["payout_batch_id"] == batch_id


def test_s2b_same_request_id_never_pays_twice(client):
    from lce.errors import PayPalError

    _need_receiver()
    key = f"ks-spike-{uuid.uuid4().hex[:20]}"
    first = client.create_payout(_payout_body(key), request_id=key)
    try:
        second = client.create_payout(_payout_body(key), request_id=key)
    except PayPalError as exc:  # the sandbox refuses the duplicate outright
        print("S2b:", first["batch_header"]["payout_batch_id"], "duplicate refused:",
              exc.context.get("name"))
        assert exc.context.get("status") == 400
        return
    print("S2b:", first["batch_header"]["payout_batch_id"],
          second["batch_header"]["payout_batch_id"])
    assert (first["batch_header"]["payout_batch_id"]
            == second["batch_header"]["payout_batch_id"])


def test_s3_invoice_create_send_get_and_due_date(client):
    _need_receiver()
    due = (datetime.now(UTC) + timedelta(days=7)).date().isoformat()
    later = (datetime.now(UTC) + timedelta(days=14)).date().isoformat()
    body = {
        "detail": {
            "currency_code": "USD",
            "invoice_number": f"KS-{uuid.uuid4().hex[:10].upper()}",
            "payment_term": {"due_date": due},
            "note": "Keystone sandbox spike S3",
        },
        "primary_recipients": [{"billing_info": {"email_address": RECEIVER}}],
        "items": [{"name": "Keystone spike", "quantity": "1",
                   "unit_amount": {"currency_code": "USD", "value": "1.00"}}],
    }
    created = client._send("POST", "/v2/invoicing/invoices", json=body)
    print("S3 create:", created.status_code)
    if created.status_code == 403:
        pytest.skip("invoicing is not permitted for this app (Keystone does not use it)")
    assert created.status_code in (200, 201), created.text
    href = created.json().get("href", "")
    invoice_id = href.rsplit("/", 1)[-1] if href else created.json()["id"]

    sent = client._send("POST", f"/v2/invoicing/invoices/{invoice_id}/send",
                        json={"send_to_invoicer": False})
    print("S3 send:", sent.status_code)
    assert sent.status_code in (200, 202, 204), sent.text

    got = client._send("GET", f"/v2/invoicing/invoices/{invoice_id}").json()
    print("S3 status after send:", got.get("status"))

    got["detail"]["payment_term"] = {"due_date": later}
    updated = client._send("PUT", f"/v2/invoicing/invoices/{invoice_id}", json=got)
    print("S3 due-date update after send:", updated.status_code, updated.text[:200])
