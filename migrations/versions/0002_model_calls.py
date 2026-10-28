"""Durable call reservations and idempotent usage receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0002"
down_revision = "0001"


def upgrade():
    op.create_table(
        "agent_model_calls",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column("run_id", pg.UUID(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("lease_token", pg.UUID(), nullable=False),
        sa.Column("reserved_output_tokens", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("reserved_output_tokens > 0"),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0"),
        sa.CheckConstraint(
            "(recorded_at IS NULL AND input_tokens IS NULL AND output_tokens IS NULL) OR (recorded_at IS NOT NULL AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL)"
        ),
    )
    op.create_index("ix_agent_model_calls_run_id", "agent_model_calls", ["run_id"])


def downgrade():
    # Losing unresolved reservations could allow already-billed work to consume budget again.
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM agent_model_calls)")):
        raise RuntimeError(
            "model call ledger is not empty; rollback requires preserving accounting"
        )
    op.drop_table("agent_model_calls")
