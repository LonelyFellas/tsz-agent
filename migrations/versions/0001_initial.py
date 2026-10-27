"""Agent execution and append-only review event ledger."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001"
down_revision = None


def upgrade():
    op.create_table(
        "agent_runs",
        sa.Column("id", pg.UUID(), primary_key=True),
        sa.Column("external_request_id", pg.UUID(), nullable=False, unique=True),
        sa.Column("idempotency_key", pg.UUID(), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("capability", sa.String(60), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(40), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", pg.UUID()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checkpoint", pg.JSONB(), nullable=False),
        sa.Column("result", pg.JSONB()),
        sa.Column("usage", pg.JSONB(), nullable=False),
        sa.Column("error_code", sa.String(60)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('queued','running','succeeded','failed','cancelled')"),
        sa.CheckConstraint("attempts >= 0"),
    )
    op.create_index("agent_runs_due", "agent_runs", ["state", "next_attempt_at"])
    op.create_table(
        "agent_review_events",
        sa.Column("event_id", pg.UUID(), primary_key=True),
        sa.Column("review_id", pg.UUID(), nullable=False),
        sa.Column("review_revision", sa.Integer(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("review_id", "review_revision"),
        sa.CheckConstraint("review_revision > 0"),
    )
    op.create_table(
        "agent_review_current",
        sa.Column("review_id", pg.UUID(), primary_key=True),
        sa.Column("review_revision", sa.Integer(), nullable=False),
        sa.Column("source_run_id", pg.UUID(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("agent_review_current")
    op.drop_table("agent_review_events")
    op.drop_table("agent_runs")
