"""Persist supervisor plans, section outcomes and provider receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0008_supervisor"
down_revision = "0007"


def upgrade():
    op.create_table(
        "agent_supervisor_runs",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("actor", UUID(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("version", sa.String(60), nullable=False),
        sa.Column(
            "created", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("view", JSONB(), nullable=False),
        sa.Column("receipts", JSONB(), nullable=False),
    )
    op.create_index("ix_agent_supervisor_runs_actor", "agent_supervisor_runs", ["actor"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM agent_supervisor_runs)")):
        raise RuntimeError("preserve supervisor executions before downgrade")
    op.drop_table("agent_supervisor_runs")
