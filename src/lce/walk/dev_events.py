"""Development events: the walk's failure, signed locally, for local runs and tests.

Until a real delivery has been recorded there is nothing to replay, and the walk
still has to be built and tested. A development event is the failure PayPal
would send, shaped like PayPal's and signed with a key generated in this
process, so it exercises the same verifier, state machine and analysis as a
real one.

It is never presented as PayPal's: every response carries the stamp
:data:`DEV_STAMP`, the event id says ``DEV``, and the routes refuse it outside
``LCE_ENV`` local or test and whenever the hosted demo mode is on. The
certificate URL has to pass the verifier's ``paypal.com`` allowlist, so it names
a path PayPal does not serve; it is answered only by :meth:`DevSigner.loader`,
in process, and never fetched.
"""

from __future__ import annotations

import base64
import functools
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from lce.data.orm import ExecutionRecordRow
from lce.paypal.money import Money
from lce.paypal.webhooks import signed_message

DEV_CERT_URL = "https://api.sandbox.paypal.com/v1/notifications/certs/KEYSTONE-DEV-NOT-PAYPAL"
DEV_WEBHOOK_ID = "KEYSTONE-DEV-LOCAL"
DEV_STAMP = "development event, signed locally, not from PayPal"


class DevSigner:
    """A throwaway RSA key and self-signed certificate, made once per process."""

    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "keystone-dev-signer")])
        start = datetime.now(UTC) - timedelta(minutes=5)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self._key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(start + timedelta(days=2))
            .sign(self._key, hashes.SHA256())
        )
        self.certificate_pem = cert.public_bytes(serialization.Encoding.PEM)

    def headers(self, body: bytes) -> dict[str, str]:
        tid = f"DEV-{uuid.uuid4().hex[:16]}"
        when = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        signature = self._key.sign(
            signed_message(tid, when, DEV_WEBHOOK_ID, body), padding.PKCS1v15(), hashes.SHA256()
        )
        return {
            "paypal-transmission-id": tid,
            "paypal-transmission-time": when,
            "paypal-transmission-sig": base64.b64encode(signature).decode(),
            "paypal-cert-url": DEV_CERT_URL,
            "paypal-auth-algo": "SHA256withRSA",
        }

    def loader(self, url: str) -> bytes:
        if url != DEV_CERT_URL:
            raise ValueError("the development signer answers for its own certificate only")
        return self.certificate_pem


@functools.lru_cache(maxsize=1)
def dev_signer() -> DevSigner:
    return DevSigner()


def dev_item_event(
    record: ExecutionRecordRow, *, outcome: str = "UNCLAIMED", receiver: str | None = None
) -> dict[str, Any]:
    """The item event PayPal sends when a payout does not land, for ``record``."""
    status = {"HELD": "ONHOLD"}.get(outcome, outcome)
    amount = Money.from_minor(record.executed_minor, record.currency).to_wire()
    return {
        "id": f"WH-DEV-{uuid.uuid4().hex[:16]}",
        "event_type": f"PAYMENT.PAYOUTS-ITEM.{outcome}",
        "create_time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": "Development event, signed locally",
        "resource": {
            "payout_item_id": f"DEV-ITEM-{record.record_id[-10:]}",
            "transaction_status": status,
            "payout_item": {
                "sender_item_id": record.idempotency_key,
                "receiver": receiver,
                "recipient_type": "EMAIL",
                "amount": amount,
            },
        },
    }
