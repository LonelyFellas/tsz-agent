"""Record lexicon assistant calls in the same ledger without pretending they are runs."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade():
    op.add_column(
        "agent_model_calls",
        sa.Column("purpose", sa.String(40), nullable=False, server_default="question_generation"),
    )
    op.add_column("agent_model_calls", sa.Column("model", sa.String(100), nullable=True))
    op.alter_column("agent_model_calls", "run_id", nullable=True)
    op.alter_column("agent_model_calls", "lease_token", nullable=True)
    op.create_check_constraint(
        "agent_model_calls_run_binding",
        "agent_model_calls",
        "(purpose = 'question_generation') = (run_id IS NOT NULL AND lease_token IS NOT NULL)",
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM agent_model_calls WHERE run_id IS NULL)")
    ):
        raise RuntimeError("assistant call receipts exist; preserve them before rollback")
    op.drop_constraint("agent_model_calls_run_binding", "agent_model_calls")
    op.alter_column("agent_model_calls", "lease_token", nullable=False)
    op.alter_column("agent_model_calls", "run_id", nullable=False)
    op.drop_column("agent_model_calls", "model")
    op.drop_column("agent_model_calls", "purpose")
