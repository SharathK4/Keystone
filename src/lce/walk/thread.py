"""The walk's thread: which merchant's payment fails, and by how much.

The failing payment is modelled as what the system already handles: a PayPal
payout to the merchant that does not arrive. Its analytic amount is a declared
multiple of the merchant's liquidity slack - the same sizing rule the bounded
analysis and the offline pulls use - so a failed trigger re-enters as exactly
the shock "m0038 loses 2x its slack" and its analysis is the one the pulls
artifact replayed offline.

The trigger is a plan like any other here: it has a fingerprint, an approval row
saying who fired it, and one execution record keyed by an idempotency key, so a
second fire finds the same record instead of paying again. It is never an
intervention; ``action_type`` says so.
"""

from __future__ import annotations

import functools
import hashlib
import json
import uuid
from dataclasses import dataclass

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from lce.config import REPO_ROOT
from lce.data.orm import ExecutionApprovalRow, ExecutionRecordRow
from lce.execution.plan import idempotency_key
from lce.paypal.money import DemoScale, Money
from lce.snapshot.analysis import _liquidity_slack
from lce.snapshot.store import SnapshotStore

#: Execution records and approvals for the trigger carry this scenario id, so
#: the plan ledgers never mix them with an intervention plan.
WALK_SCENARIO_ID = "walk-trigger"
TRIGGER_ACTION_KEY = "trigger"
TRIGGER_ACTION_TYPE = "trigger_payout"
TRIGGER_HASH_VERSION = "keystone-walk-trigger-v1"
PROVIDER_NAME = "paypal_sandbox"


class WalkSettings(BaseSettings):
    """Which merchant the walk follows.

    m0038 at 2x its cushion: its failure reaches one more merchant, one step
    out; the plan is two injections PayPal can pay (m0005, m0009); and both
    cost-of-waiting curves fall as the money lands later. m0013 reaches two
    more, but its plan includes a supplier term extension PayPal cannot pay,
    and re-planning an hour after its shock finds a better plan the first
    search missed (the candidate-list weakness recorded in Gate H).
    """

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    merchant: str = Field(default="m0038", alias="LCE_WALK_MERCHANT")
    multiple: float = Field(default=2.0, gt=0, le=5, alias="LCE_WALK_MULTIPLE")
    #: Live runs only: write each verified PayPal delivery here, as received, so
    #: ``lce walk record`` can turn one into the committed fixture. Off when empty.
    record_deliveries_dir: str = Field(default="", alias="LCE_RECORD_DELIVERIES_DIR")


@functools.lru_cache(maxsize=1)
def get_walk_settings() -> WalkSettings:
    return WalkSettings()


def reset_walk_settings_cache() -> None:
    get_walk_settings.cache_clear()


@dataclass(frozen=True, slots=True)
class Trigger:
    """The payment that will fail, priced and fingerprinted."""

    snapshot_id: str
    merchant_id: str
    slack_inr: float
    multiple: float
    missing_inr: float
    executed: Money
    demo_scale: str
    plan_hash: str
    idempotency_key: str


def trigger(store: SnapshotStore, settings: WalkSettings, scale: DemoScale) -> Trigger:
    store.merchant(settings.merchant)  # raises NotFoundError for an unknown merchant
    slack = max(_liquidity_slack(store, settings.merchant), 1.0)
    missing = slack * settings.multiple
    executed = scale.apply(missing)
    canonical = {
        "version": TRIGGER_HASH_VERSION,
        "snapshot_id": store.snapshot_id,
        "merchant_id": settings.merchant,
        "analytic_amount_inr": repr(missing),
        "executed": executed.to_wire(),
        "demo": scale.describe(),
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return Trigger(
        snapshot_id=store.snapshot_id,
        merchant_id=settings.merchant,
        slack_inr=slack,
        multiple=settings.multiple,
        missing_inr=missing,
        executed=executed,
        demo_scale=f"{scale.rate:f}",
        plan_hash=digest,
        idempotency_key=idempotency_key(digest, TRIGGER_ACTION_KEY),
    )


def find_trigger_record(session: Session, key: str) -> ExecutionRecordRow | None:
    return session.execute(
        select(ExecutionRecordRow).where(ExecutionRecordRow.idempotency_key == key)
    ).scalar_one_or_none()


def ensure_trigger_record(
    session: Session, trig: Trigger, *, approver: str, reason: str
) -> ExecutionRecordRow:
    """The trigger's execution record, created once with the approval behind it."""
    existing = find_trigger_record(session, trig.idempotency_key)
    if existing is not None:
        return existing
    approval = ExecutionApprovalRow(
        approval_id=f"apr_{uuid.uuid4().hex[:20]}",
        plan_hash=trig.plan_hash,
        snapshot_id=trig.snapshot_id,
        scenario_id=WALK_SCENARIO_ID,
        provider=PROVIDER_NAME,
        approver=approver[:80],
        executed_total_minor=trig.executed.minor_units,
        currency=trig.executed.currency,
        demo_scale=trig.demo_scale,
        detail={"actions": [TRIGGER_ACTION_KEY], "kind": "walk_trigger"},
    )
    record = ExecutionRecordRow(
        record_id=f"rec_{uuid.uuid4().hex[:20]}",
        idempotency_key=trig.idempotency_key,
        approval_id=approval.approval_id,
        plan_hash=trig.plan_hash,
        scenario_id=WALK_SCENARIO_ID,
        action_key=TRIGGER_ACTION_KEY,
        merchant_id=trig.merchant_id,
        action_type=TRIGGER_ACTION_TYPE,
        analytic_amount_inr=repr(trig.missing_inr),
        executed_minor=trig.executed.minor_units,
        currency=trig.executed.currency,
        provider=PROVIDER_NAME,
        status="submitted",
        reason=reason,
    )
    session.add_all([approval, record])
    session.commit()
    return record
