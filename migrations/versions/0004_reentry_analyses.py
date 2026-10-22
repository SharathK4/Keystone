"""paypal webhook events and reentry analyses

Additive only: two new tables. PayPal webhook events get their own table rather
than the existing ``webhook_events``, whose BIGINT primary key does not
auto-increment on SQLite; ``event_id`` is unique, which is the race-safe dedupe.

Revision ID: 0004_reentry
Revises: 0003_execution
Create Date: 2026-10-22
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_reentry"
down_revision: str | None = "0003_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "paypal_webhook_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("verification", _JSON, nullable=False),
        sa.Column("transmission", _JSON, nullable=False),
        sa.Column("raw_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_paypal_webhook_event"),
    )
    op.create_index("ix_paypal_webhook_type", "paypal_webhook_events", ["event_type"])

    op.create_table(
        "reentry_analyses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_id", sa.String(length=40), nullable=False),
        sa.Column("record_id", sa.String(length=40), nullable=False),
        sa.Column("scenario_id", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("source_event_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("shock", _JSON, nullable=False),
        sa.Column("result", _JSON, nullable=False),
        sa.Column("provenance", _JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_id", name="uq_reentry_analysis_id"),
        sa.UniqueConstraint("record_id", "trigger", name="uq_reentry_record_trigger"),
    )
    op.create_index("ix_reentry_scenario", "reentry_analyses", ["scenario_id"])


def downgrade() -> None:
    op.drop_index("ix_reentry_scenario", table_name="reentry_analyses")
    op.drop_table("reentry_analyses")
    op.drop_index("ix_paypal_webhook_type", table_name="paypal_webhook_events")
    op.drop_table("paypal_webhook_events")
