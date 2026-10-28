"""Persist the model selected for new runs; credentials remain in OpenCode."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0005"
down_revision = "0004"


def upgrade():
    op.create_table(
        "agent_model_setting",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("selection", pg.JSONB(), nullable=False),
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM agent_runs WHERE usage->>'model' LIKE 'opencode/%' "
            "AND state IN ('queued','running'))"
        )
    ):
        raise RuntimeError("active OpenCode runs must finish or be cancelled before rollback")
    op.drop_table("agent_model_setting")
