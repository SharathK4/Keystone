"""P3 - webhook signature verification (the receiver itself lands in Gate P2).

A real RSA key and X.509 certificate are generated per session and used to sign
deliveries exactly as PayPal does: RSA/SHA-256 over
``transmission_id|transmission_time|webhook_id|crc32(raw body)``. Every way a
delivery can be wrong must be refused with a reason, and the one right way must
pass - including the simulator's ``WEBHOOK_ID`` convention.
"""

from __future__ import annotations

import base64
import zlib
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from lce.errors import WebhookVerificationError
from lce.paypal.webhooks import (
    REQUIRED_HEADERS,
    SIMULATOR_WEBHOOK_ID,
    cert_url_allowed,
    crc32_decimal,
    require_verified,
    signed_message,
    verify_webhook,
)

WEBHOOK_ID = "WH-KS-TEST-0001"
CERT_URL = "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-KS-TEST"
BODY = (
    b'{"id":"WH-1","event_type":"PAYMENT.PAYOUTS-ITEM.UNCLAIMED",'
    b'"resource":{"payout_item_id":"ITEM1","transaction_status":"UNCLAIMED"}}'
)


def _keypair(*, valid_from: datetime, valid_for: timedelta):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "keystone-test-signer")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(valid_from)
        .not_valid_after(valid_from + valid_for)
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture(scope="module")
def signer():
    now = datetime.now(UTC)
    return _keypair(valid_from=now - timedelta(days=1), valid_for=timedelta(days=30))


def _headers(key, *, body=BODY, webhook_id=WEBHOOK_ID, **overrides):
    tid, ttime = "TX-ks-0001", "2026-10-21T10:00:00Z"
    signature = key.sign(signed_message(tid, ttime, webhook_id, body), padding.PKCS1v15(),
                         hashes.SHA256())
    headers = {
        "PAYPAL-TRANSMISSION-ID": tid,
        "PAYPAL-TRANSMISSION-TIME": ttime,
        "PAYPAL-TRANSMISSION-SIG": base64.b64encode(signature).decode(),
        "PAYPAL-CERT-URL": CERT_URL,
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    }
    headers.update(overrides)
    return headers


def _verify(headers, pem, *, body=BODY, webhook_id=WEBHOOK_ID, now=None):
    return verify_webhook(headers, body, webhook_id=webhook_id, load_cert=lambda _url: pem,
                          now=now)


class TestTheSignedString:
    def test_crc_is_unsigned_decimal(self):
        assert crc32_decimal(BODY) == zlib.crc32(BODY) & 0xFFFFFFFF
        assert crc32_decimal(BODY) >= 0

    def test_message_layout(self):
        assert signed_message("a", "b", "c", BODY) == f"a|b|c|{crc32_decimal(BODY)}".encode()


class TestVerification:
    def test_a_genuine_delivery_passes(self, signer):
        key, pem = signer
        result = _verify(_headers(key), pem)
        assert result.verified, result.reason
        require_verified(result)

    def test_header_names_are_case_insensitive(self, signer):
        key, pem = signer
        lowered = {k.lower(): v for k, v in _headers(key).items()}
        assert _verify(lowered, pem).verified

    def test_a_tampered_body_fails(self, signer):
        key, pem = signer
        tampered = BODY.replace(b"UNCLAIMED", b"SUCCEEDED")
        result = _verify(_headers(key), pem, body=tampered)
        assert not result.verified and result.reason == "signature does not match"

    def test_reserialised_json_fails(self, signer):
        """The raw bytes are signed; parsing and re-dumping changes them."""
        import json

        key, pem = signer
        redumped = json.dumps(json.loads(BODY)).encode()
        assert redumped != BODY
        assert not _verify(_headers(key), pem, body=redumped).verified

    def test_a_delivery_for_another_listener_fails(self, signer):
        key, pem = signer
        headers = _headers(key, webhook_id="WH-SOMEONE-ELSE")
        assert not _verify(headers, pem).verified

    @pytest.mark.parametrize("missing", REQUIRED_HEADERS)
    def test_every_header_is_required(self, signer, missing):
        key, pem = signer
        headers = {k: v for k, v in _headers(key).items() if k.lower() != missing}
        result = _verify(headers, pem)
        assert not result.verified and missing in result.reason

    def test_only_sha256_with_rsa(self, signer):
        key, pem = signer
        result = _verify(_headers(key, **{"PAYPAL-AUTH-ALGO": "SHA1withRSA"}), pem)
        assert not result.verified and "algorithm" in result.reason

    @pytest.mark.parametrize(
        "url",
        [
            "http://api.sandbox.paypal.com/v1/notifications/certs/X",
            "https://api.sandbox.paypal.com.evil.example/certs/X",
            "https://evil.example/paypal.com/certs/X",
            "https://notpaypal.com/certs/X",
        ],
    )
    def test_the_certificate_must_come_from_paypal_over_https(self, signer, url):
        key, pem = signer
        assert not cert_url_allowed(url)
        result = _verify(_headers(key, **{"PAYPAL-CERT-URL": url}), pem)
        assert not result.verified and "certificate URL" in result.reason

    def test_an_unloadable_certificate_fails_closed(self, signer):
        key, _pem = signer

        def boom(_url):
            raise OSError("network down")

        result = verify_webhook(_headers(key), BODY, webhook_id=WEBHOOK_ID, load_cert=boom)
        assert not result.verified and "could not be loaded" in result.reason

    def test_an_expired_certificate_fails(self):
        old = datetime.now(UTC) - timedelta(days=400)
        key, pem = _keypair(valid_from=old, valid_for=timedelta(days=30))
        result = _verify(_headers(key), pem)
        assert not result.verified and "validity" in result.reason

    def test_a_signature_from_another_key_fails(self, signer):
        _key, pem = signer
        other, _ = _keypair(valid_from=datetime.now(UTC) - timedelta(days=1),
                            valid_for=timedelta(days=30))
        assert not _verify(_headers(other), pem).verified

    def test_garbage_signature_fails(self, signer):
        key, pem = signer
        result = _verify(_headers(key, **{"PAYPAL-TRANSMISSION-SIG": "not base64!!"}), pem)
        assert not result.verified and "base64" in result.reason

    def test_no_configured_webhook_id_fails(self, signer):
        key, pem = signer
        assert not _verify(_headers(key), pem, webhook_id="").verified

    def test_require_verified_raises_with_the_reason(self, signer):
        key, pem = signer
        result = _verify(_headers(key, webhook_id="WH-OTHER"), pem)
        with pytest.raises(WebhookVerificationError) as raised:
            require_verified(result)
        assert raised.value.context["reason"] == "signature does not match"


class TestSimulatorEvents:
    def test_simulator_events_verify_with_the_literal_id(self, signer):
        key, pem = signer
        headers = _headers(key, webhook_id=SIMULATOR_WEBHOOK_ID)
        assert _verify(headers, pem, webhook_id=SIMULATOR_WEBHOOK_ID).verified
        assert not _verify(headers, pem, webhook_id=WEBHOOK_ID).verified
