"""``lce walk`` - the walk's event recording and its offline cost of waiting.

``status`` reads only. ``record`` turns one captured live delivery into the
committed fixture; it downloads the signing certificate from the HTTPS
``paypal.com`` URL the delivery names and refuses a capture that does not
verify. ``build-speed`` replays the walk's shock on the delay grid (offline,
about twenty seconds) and writes the content-addressed artifact.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def cmd_status(args: argparse.Namespace) -> int:
    from lce.walk.recording import DEFAULT_FIXTURE, load_recorded
    from lce.walk.thread import get_walk_settings

    s = get_walk_settings()
    recorded = load_recorded()
    print(json.dumps({
        "merchant": s.merchant,
        "multiple": s.multiple,
        "fixture": str(DEFAULT_FIXTURE),
        "recorded": None if recorded is None else {
            "source": recorded.source, "recorded_at": recorded.recorded_at,
            "event_type": recorded.event.get("event_type"),
        },
        "capturing_deliveries_to": s.record_deliveries_dir or None,
    }, indent=2))
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from lce.config import get_paypal_settings
    from lce.data.database import session_scope
    from lce.data.orm import ExecutionApprovalRow, ExecutionRecordRow
    from lce.paypal.webhooks import fetch_certificate
    from lce.walk.fire import trigger_export
    from lce.walk.recording import DEFAULT_FIXTURE, build_fixture
    from lce.walk.thread import WALK_SCENARIO_ID, get_walk_settings

    directory = Path(args.dir or get_walk_settings().record_deliveries_dir or "")
    capture_path = directory / f"{args.event_id}.json"
    if not capture_path.is_file():
        print(f"no capture at {capture_path}; deliveries are captured only while "
              "LCE_RECORD_DELIVERIES_DIR is set")
        return 1
    capture: dict[str, Any] = json.loads(capture_path.read_text(encoding="utf-8"))
    import base64

    event = json.loads(base64.b64decode(capture["raw_body_b64"]))
    item = ((event.get("resource") or {}).get("payout_item") or {})
    key = str(item.get("sender_item_id") or "")
    with session_scope() as session:
        record = session.execute(
            select(ExecutionRecordRow).where(ExecutionRecordRow.idempotency_key == key)
        ).scalar_one_or_none()
        if record is None or record.scenario_id != WALK_SCENARIO_ID:
            print("the captured event does not belong to a walk trigger record")
            return 1
        approval = session.execute(
            select(ExecutionApprovalRow).where(
                ExecutionApprovalRow.approval_id == record.approval_id)
        ).scalar_one()
        trig = trigger_export(record, approval)
    pem = fetch_certificate(str(capture["headers"]["paypal-cert-url"]))
    fixture = build_fixture(capture, certificate_pem=pem, trigger=trig,
                            webhook_id=get_paypal_settings().require_webhook_id())
    out = Path(args.out) if args.out else DEFAULT_FIXTURE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    print(json.dumps({"wrote": str(out), "source": fixture["source"],
                      "content_hash": fixture["content_hash"]}, indent=2))
    return 0


def cmd_build_speed(args: argparse.Namespace) -> int:
    from lce.config import REPO_ROOT
    from lce.snapshot.store import require_store
    from lce.walk.speed import build_walk_speed, write_walk_speed
    from lce.walk.thread import get_walk_settings

    s = get_walk_settings()
    store = require_store()
    payload = build_walk_speed(store, merchant_ids=[s.merchant], magnitude_multiple=s.multiple)
    path = write_walk_speed(REPO_ROOT / "artifacts" / "structure", payload)
    print(json.dumps({"wrote": str(path)}, indent=2))
    return 0


def register(sub: Any) -> None:
    walk = sub.add_parser("walk", help="The front page's walk: event recording, cost of waiting")
    wsub = walk.add_subparsers(dest="walk_command", required=True)
    wsub.add_parser("status", help="Which merchant, and whether an event is recorded").set_defaults(
        func=cmd_status)
    r = wsub.add_parser("record", help="Turn one captured live delivery into the fixture")
    r.add_argument("event_id")
    r.add_argument("--dir", default=None, help="Capture directory (default: the setting)")
    r.add_argument("--out", default=None, help="Fixture path (default: the committed one)")
    r.set_defaults(func=cmd_record)
    wsub.add_parser("build-speed", help="Replay the walk's shock on the delay grid").set_defaults(
        func=cmd_build_speed)
