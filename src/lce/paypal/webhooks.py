"""Verifying that an inbound webhook really came from PayPal.

PayPal offers two methods; this module implements the one PayPal prefers
(https://developer.paypal.com/api/rest/webhooks/rest/):

**Self-verification.** PayPal signs the string::

    <transmission id>|<transmission time>|<webhook id>|<CRC32 of the raw body>

with RSA/SHA-256 and sends the signature in ``paypal-transmission-sig``, plus a
URL for the signing certificate in ``paypal-cert-url``. The webhook id is never
in the request: it is the id PayPal assigned when this listener was registered,
held in settings. So a forged event fails on the signature, a tampered body
fails on the CRC, and an event meant for a different listener fails on the
webhook id - all three are the same comparison.

It is also the only method that works for the developer dashboard's simulator:
mock events are signed with the literal webhook id ``WEBHOOK_ID`` and cannot be
checked by postback.

What is checked here, and what is not:

* the certificate URL must be HTTPS on a ``paypal.com`` host - otherwise anyone
  could sign with their own key and point at their own certificate;
* the certificate must be inside its validity window;
* the algorithm must be ``SHA256withRSA``.

The certificate's chain to PayPal's issuing CA is **not** validated; the host
allowlist is what stands in for it. That is a stated limitation, not an
oversight.
"""

from __future__ import annotations

import base64
import binascii
import threading
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from lce.errors import WebhookVerificationError

#: The webhook id PayPal's simulator signs mock events with.
SIMULATOR_WEBHOOK_ID = "WEBHOOK_ID"

SUPPORTED_ALGORITHM = "SHA256withRSA"

#: Headers that must all be present. Lower-cased, as HTTP header names compare.
REQUIRED_HEADERS = (
    "paypal-transmission-id",
    "paypal-transmission-time",
    "paypal-transmission-sig",
    "paypal-cert-url",
    "paypal-auth-algo",
)

CertLoader = Callable[[str], bytes]


@dataclass(frozen=True, slots=True)
class Verification:
    verified: bool
    reason: str
    transmission_id: str | None = None


def crc32_decimal(body: bytes) -> int:
    """CRC32 of the raw body as an unsigned integer - the form PayPal signs."""
    return zlib.crc32(body) & 0xFFFFFFFF


def signed_message(
    transmission_id: str, transmission_time: str, webhook_id: str, body: bytes
) -> bytes:
    return f"{transmission_id}|{transmission_time}|{webhook_id}|{crc32_decimal(body)}".encode()


def cert_url_allowed(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "paypal.com" or host.endswith(".paypal.com"))


def verify_webhook(
    headers: Mapping[str, str],
    body: bytes,
    *,
    webhook_id: str,
    load_cert: CertLoader,
    now: datetime | None = None,
) -> Verification:
    """Self-verify one delivery. Never raises; the reason says what failed.

    ``body`` must be the raw bytes as received. Parsing and re-serialising the
    JSON first changes the CRC and fails every real event.
    """
    h = {k.lower(): v for k, v in headers.items()}
    missing = [name for name in REQUIRED_HEADERS if not h.get(name)]
    if missing:
        return Verification(False, f"missing headers: {', '.join(missing)}")
    transmission_id = h["paypal-transmission-id"]

    if h["paypal-auth-algo"] != SUPPORTED_ALGORITHM:
        return Verification(False, "unsupported signature algorithm", transmission_id)
    cert_url = h["paypal-cert-url"]
    if not cert_url_allowed(cert_url):
        return Verification(False, "certificate URL is not an HTTPS paypal.com URL",
                            transmission_id)
    if not webhook_id:
        return Verification(False, "no webhook id configured", transmission_id)

    try:
        signature = base64.b64decode(h["paypal-transmission-sig"], validate=True)
    except (binascii.Error, ValueError):
        return Verification(False, "signature is not base64", transmission_id)

    try:
        certificate = x509.load_pem_x509_certificate(load_cert(cert_url))
    except Exception:
        return Verification(False, "certificate could not be loaded", transmission_id)

    moment = now or datetime.now(UTC)
    if not (certificate.not_valid_before_utc <= moment <= certificate.not_valid_after_utc):
        return Verification(False, "certificate is outside its validity window",
                            transmission_id)

    key = certificate.public_key()
    if not isinstance(key, rsa.RSAPublicKey):
        return Verification(False, "certificate key is not RSA", transmission_id)

    message = signed_message(transmission_id, h["paypal-transmission-time"], webhook_id, body)
    try:
        key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return Verification(False, "signature does not match", transmission_id)
    return Verification(True, "verified", transmission_id)


def require_verified(verification: Verification) -> None:
    if not verification.verified:
        raise WebhookVerificationError(
            "PayPal webhook signature verification failed",
            reason=verification.reason,
            transmission_id=verification.transmission_id,
        )


_cert_cache: dict[str, bytes] = {}
_cert_lock = threading.Lock()


def fetch_certificate(url: str) -> bytes:
    """Download (once) the signing certificate PayPal points at.

    Refuses any URL that is not HTTPS on a ``paypal.com`` host *before* making a
    request, so a forged header cannot make this server fetch from elsewhere.
    """
    if not cert_url_allowed(url):
        raise ValueError("certificate URL is not an HTTPS paypal.com URL")
    with _cert_lock:
        cached = _cert_cache.get(url)
    if cached is not None:
        return cached
    response = httpx.get(url, timeout=5.0, follow_redirects=False)
    response.raise_for_status()
    pem = response.content
    with _cert_lock:
        _cert_cache[url] = pem
    return pem
