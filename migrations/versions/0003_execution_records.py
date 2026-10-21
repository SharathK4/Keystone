"""execution records: PayPal identities, approvals, records, audit events

Additive only. Creates four tables used by the execution routes; nothing the
analysis reads is touched.

Revision ID: 0003_execution
Revises: 0002_provenance
Create Date: 2026-10-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_execution"
down_revision: str | None = "0002_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
_NOW = sa.text("(CURRENT_TIMESTAMP)")


def upgrade() -> None:
    op.create_table(
        "paypal_identities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("recipient_type", sa.String(length=16), nullable=False),
        sa.Column("receiver", sa.String(length=127), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("merchant_id", name="uq_paypal_identity_merchant"),
    )

    op.create_table(
        "execution_approvals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("approval_id", sa.String(length=40), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("scenario_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("approver", sa.String(length=80), nullable=False),
        sa.Column("executed_total_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("demo_scale", sa.String(length=32), nullable=False),
        sa.Column("detail", _JSON, nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_id", name="uq_execution_approval_id"),
    )
    op.create_index("ix_execution_approval_plan", "execution_approvals", ["plan_hash"])

    op.create_table(
        "execution_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("record_id", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("approval_id", sa.String(length=40), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("scenario_id", sa.String(length=64), nullable=False),
        sa.Column("action_key", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("analytic_amount_inr", sa.String(length=40), nullable=False),
        sa.Column("executed_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("provider_batch_id", sa.String(length=64), nullable=True),
        sa.Column("provider_item_id", sa.String(length=64), nullable=True),
        sa.Column("provider_status", sa.String(length=32), nullable=True),
        sa.Column("debug_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("record_id", name="uq_execution_record_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_execution_idempotency"),
    )
    op.create_index("ix_execution_record_plan", "execution_records", ["plan_hash"])
    op.create_index("ix_execution_record_scenario", "execution_records", ["scenario_id"])

    op.create_table(
        "execution_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=40), nullable=False),
        sa.Column("scenario_id", sa.String(length=64), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("record_id", sa.String(length=40), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("detail", _JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_execution_event_id"),
    )
    op.create_index("ix_execution_event_scenario", "execution_events", ["scenario_id"])
    op.create_index("ix_execution_event_record", "execution_events", ["record_id"])


def downgrade() -> None:
    op.drop_index("ix_execution_event_record", table_name="execution_events")
    op.drop_index("ix_execution_event_scenario", table_name="execution_events")
    op.drop_table("execution_events")
    op.drop_index("ix_execution_record_scenario", table_name="execution_records")
    op.drop_index("ix_execution_record_plan", table_name="execution_records")
    op.drop_table("execution_records")
    op.drop_index("ix_execution_approval_plan", table_name="execution_approvals")
    op.drop_table("execution_approvals")
    op.drop_table("paypal_identities")
