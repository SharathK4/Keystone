"""``lce paypal`` - map sandbox identities and check the PayPal connection.

``map`` and ``identities`` touch only the local database. ``status --probe`` is
the one command here that calls PayPal (token + read-only probes); it runs only
when a person types it.

Nothing here prints a credential. Receivers are sandbox accounts and are printed
so the person mapping them can see what they mapped.
"""

from __future__ import annotations

import argparse
import json
from typing import Any


def cmd_map(args: argparse.Namespace) -> int:
    from lce.data.database import session_scope
    from lce.execution.service import ExecutionService
    from lce.snapshot.store import require_store

    kind, receiver = (
        ("EMAIL", args.email) if args.email
        else ("PHONE", args.phone) if args.phone
        else ("PAYPAL_ID", args.paypal_id)
    )
    with session_scope() as session:
        row = ExecutionService(session, store=require_store()).map_identity(
            args.merchant_id, recipient_type=kind, receiver=receiver, label=args.label or ""
        )
        print(json.dumps({"merchant_id": row.merchant_id, "recipient_type": row.recipient_type,
                          "receiver": row.receiver, "label": row.label}))
    return 0


def cmd_identities(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from lce.data.database import session_scope
    from lce.data.orm import PayPalIdentityRow

    with session_scope() as session:
        rows = session.execute(
            select(PayPalIdentityRow).order_by(PayPalIdentityRow.merchant_id)
        ).scalars()
        out = [{"merchant_id": r.merchant_id, "recipient_type": r.recipient_type,
                "receiver": r.receiver, "label": r.label} for r in rows]
    print(json.dumps(out, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from lce.config import get_paypal_settings
    from lce.execution.paypal import PayPalSandboxProvider, refused_report

    settings = get_paypal_settings()
    report = (
        refused_report(settings)
        if settings.refusal() is not None
        else PayPalSandboxProvider(settings).report(probe=args.probe)
    )
    out: dict[str, Any] = {
        **settings.safe_dump(),
        "probed": report.probed,
        "capabilities": [c.to_dict() for c in report.capabilities],
        "executable_intervention_types": report.executable_intervention_types,
    }
    print(json.dumps(out, indent=2, default=str))
    return 0 if report.refused is None else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Read payouts back from PayPal. Never resubmits; calls PayPal (GET only)."""
    from sqlalchemy import select

    from lce.data.database import session_scope
    from lce.data.orm import ExecutionRecordRow
    from lce.execution.reentry import reenter
    from lce.execution.service import ExecutionService
    from lce.snapshot.store import require_store

    store = require_store()
    with session_scope() as session:
        if args.record:
            ids = [args.record]
        else:
            ids = [r.record_id for r in session.execute(
                select(ExecutionRecordRow).where(ExecutionRecordRow.status.in_(
                    ["unknown", "submitted", "pending", "held"]))).scalars()]
        service = ExecutionService(session, store=store)
        out = []
        for record_id in ids:
            record, reentry = service.refresh(record_id)
            analysed = None
            if reentry is not None:
                analysed = reenter(session, store, record, trigger=reentry.trigger,
                                   receiver=reentry.receiver).status
            out.append({"record_id": record.record_id, "status": record.status,
                        "reason": record.reason, "reentry": analysed})
    print(json.dumps(out, indent=2))
    return 0


def register(sub: Any) -> None:
    paypal = sub.add_parser("paypal", help="PayPal sandbox: identities and connection")
    psub = paypal.add_subparsers(dest="paypal_command", required=True)

    m = psub.add_parser("map", help="Map a merchant to a PayPal sandbox account")
    m.add_argument("merchant_id")
    who = m.add_mutually_exclusive_group(required=True)
    who.add_argument("--email")
    who.add_argument("--phone")
    who.add_argument("--paypal-id", dest="paypal_id")
    m.add_argument("--label", default="")
    m.set_defaults(func=cmd_map)

    psub.add_parser("identities", help="List mapped sandbox identities").set_defaults(
        func=cmd_identities
    )

    rc = psub.add_parser("reconcile",
                         help="Read open payouts back from PayPal (GET only; never resubmits)")
    rc.add_argument("--record", default=None, help="One record id; default: every open record")
    rc.set_defaults(func=cmd_reconcile)

    st = psub.add_parser("status", help="Configuration, and capabilities if probed")
    st.add_argument("--probe", action="store_true",
                    help="Call PayPal (token + read-only probes). Off by default.")
    st.set_defaults(func=cmd_status)
