"""Recording one real PayPal delivery so it can be replayed through the real checks.

A replayed event is only worth showing if it is the bytes PayPal sent. The
database keeps the parsed event and a hash, not the raw body or the signature,
so a recording is captured at the door instead:

1. **Capture** (live runs only, opt-in with ``LCE_RECORD_DELIVERIES_DIR``): after
   a delivery verifies, its raw body and the five signature headers are written
   to that directory. Nothing here is secret - the signature and the certificate
   URL are public by design - and the webhook id is never written.
2. **Record** (``lce walk record <event id>``, run by the owner): the capture,
   the signing certificate downloaded from the HTTPS ``paypal.com`` URL PayPal
   named, and the trigger record the event belongs to are written to one
   committed, content-addressed fixture.
3. **Replay** (``POST /walk/fire`` in recorded mode): the fixture's bytes are
   verified against its certificate and the configured webhook id exactly as a
   live delivery is, then ingested and analysed.

Fixtures are never written by hand. :func:`build_fixture` only accepts a
capture that verifies.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lce.config import REPO_ROOT
from lce.errors import ValidationError
from lce.paypal.webhooks import REQUIRED_HEADERS, SIMULATOR_WEBHOOK_ID, CertLoader, verify_webhook

DELIVERY_KIND = "keystone-paypal-delivery-v1"
FIXTURE_KIND = "keystone-recorded-paypal-event-v1"
DEFAULT_FIXTURE = REPO_ROOT / "artifacts" / "paypal" / "recorded" / "walk.json"

SOURCE_SANDBOX = "paypal_sandbox"
SOURCE_SIMULATOR = "paypal_simulator"


def _signature_headers(headers: Mapping[str, str]) -> dict[str, str]:
    lowered = {k.lower(): v for k, v in headers.items()}
    return {name: lowered[name] for name in REQUIRED_HEADERS if name in lowered}


def capture_delivery(
    directory: Path, *, raw_body: bytes, headers: Mapping[str, str], event_id: str
) -> Path:
    """Write one verified delivery as received. Returns the file written."""
    directory.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in event_id if c.isalnum() or c in "-_")[:96] or "event"
    path = directory / f"{safe}.json"
    path.write_text(
        json.dumps(
            {
                "kind": DELIVERY_KIND,
                "captured_at": datetime.now(UTC).isoformat(),
                "event_id": event_id,
                "raw_body_b64": base64.b64encode(raw_body).decode(),
                "headers": _signature_headers(headers),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


@dataclass(frozen=True, slots=True)
class RecordedEvent:
    """One real delivery, ready to replay."""

    source: str
    recorded_at: str
    raw_body: bytes
    headers: dict[str, str]
    certificate_pem: bytes
    trigger: dict[str, Any]
    content_hash: str

    @property
    def event(self) -> dict[str, Any]:
        parsed = json.loads(self.raw_body)
        return parsed if isinstance(parsed, dict) else {}

    @property
    def webhook_id_for(self) -> str | None:
        """The webhook id the signature was made with, when it is not ours."""
        return SIMULATOR_WEBHOOK_ID if self.source == SOURCE_SIMULATOR else None

    def cert_loader(self) -> CertLoader:
        url = self.headers.get("paypal-cert-url", "")
        pem = self.certificate_pem

        def load(requested: str) -> bytes:
            if requested != url:
                raise ValueError("this recording carries one certificate only")
            return pem

        return load


def _hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def build_fixture(
    capture: Mapping[str, Any],
    *,
    certificate_pem: bytes,
    trigger: dict[str, Any],
    webhook_id: str,
) -> dict[str, Any]:
    """A fixture from a capture - refused unless the capture verifies."""
    if capture.get("kind") != DELIVERY_KIND:
        raise ValidationError("not a delivery capture", kind=capture.get("kind"))
    raw = base64.b64decode(str(capture["raw_body_b64"]))
    headers = dict(capture["headers"])
    url = headers.get("paypal-cert-url", "")

    def load(requested: str) -> bytes:
        if requested != url:
            raise ValueError("unexpected certificate URL")
        return certificate_pem

    source = SOURCE_SANDBOX
    verification = verify_webhook(headers, raw, webhook_id=webhook_id, load_cert=load)
    if not verification.verified:
        simulated = verify_webhook(headers, raw, webhook_id=SIMULATOR_WEBHOOK_ID, load_cert=load)
        if not simulated.verified:
            raise ValidationError(
                "the capture does not verify; only real PayPal deliveries are recorded",
                reason=verification.reason,
            )
        source = SOURCE_SIMULATOR
    body = {
        "kind": FIXTURE_KIND,
        "source": source,
        "recorded_at": str(capture.get("captured_at") or datetime.now(UTC).isoformat()),
        "event_id": capture.get("event_id"),
        "raw_body_b64": capture["raw_body_b64"],
        "headers": headers,
        "certificate_pem": certificate_pem.decode(),
        "trigger": trigger,
    }
    return {**body, "content_hash": _hash(body)}


def load_recorded(path: Path = DEFAULT_FIXTURE) -> RecordedEvent | None:
    """The committed recording, if there is one. A changed byte is refused."""
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    body = {k: v for k, v in data.items() if k != "content_hash"}
    if data.get("kind") != FIXTURE_KIND or _hash(body) != data.get("content_hash"):
        raise ValidationError("the recorded PayPal event does not match its content hash",
                              path=str(path))
    return RecordedEvent(
        source=str(data["source"]),
        recorded_at=str(data["recorded_at"]),
        raw_body=base64.b64decode(str(data["raw_body_b64"])),
        headers={k.lower(): str(v) for k, v in dict(data["headers"]).items()},
        certificate_pem=str(data["certificate_pem"]).encode(),
        trigger=dict(data["trigger"]),
        content_hash=str(data["content_hash"]),
    )
