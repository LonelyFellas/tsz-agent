"""Retain the latest explicit availability probe without changing the selected model."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0006"
down_revision = "0005"


def upgrade():
    op.add_column(
        "agent_model_setting", sa.Column("probe_report", pg.JSONB(none_as_null=True), nullable=True)
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM agent_model_setting WHERE probe_report->'report'->>'state'='running')"
        )
    ):
        raise RuntimeError("active model probes must finish before rollback")
    op.drop_column("agent_model_setting", "probe_report")
