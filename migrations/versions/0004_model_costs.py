"""Freeze pricing per call and retain explicitly estimated CNY costs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0004"
down_revision = "0003"


def upgrade():
    op.add_column("agent_model_calls", sa.Column("pricing", pg.JSONB(), nullable=True))
    op.add_column(
        "agent_model_calls", sa.Column("estimated_cost_cny", sa.Numeric(24, 12), nullable=True)
    )
    op.create_check_constraint(
        "model_cost_nonnegative", "agent_model_calls", "estimated_cost_cny >= 0"
    )
    op.create_check_constraint(
        "model_cost_has_evidence",
        "agent_model_calls",
        "estimated_cost_cny IS NULL OR (pricing IS NOT NULL AND recorded_at IS NOT NULL AND metrics IS NOT NULL)",
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM agent_model_calls WHERE pricing IS NOT NULL OR estimated_cost_cny IS NOT NULL)"
        )
    ):
        raise RuntimeError("pricing observations exist; preserve them before rollback")
    op.drop_constraint("model_cost_has_evidence", "agent_model_calls")
    op.drop_constraint("model_cost_nonnegative", "agent_model_calls")
    op.drop_column("agent_model_calls", "estimated_cost_cny")
    op.drop_column("agent_model_calls", "pricing")
