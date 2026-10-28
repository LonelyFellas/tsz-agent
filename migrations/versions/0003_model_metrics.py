"""Optional per-call cache and latency observations; existing receipts stay unknown."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0003"
down_revision = "0002"


def upgrade():
    op.add_column("agent_model_calls", sa.Column("metrics", pg.JSONB(), nullable=True))


def downgrade():
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM agent_model_calls WHERE metrics IS NOT NULL)")
    ):
        raise RuntimeError("model metrics are not empty; preserve observations before rollback")
    op.drop_column("agent_model_calls", "metrics")
