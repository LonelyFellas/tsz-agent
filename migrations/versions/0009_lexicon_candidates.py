"""Store reusable results of independent lexicon section runs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0009_lexicon_candidates"
down_revision = "0008_supervisor"


def upgrade():
    op.create_table(
        "agent_lexicon_candidates",
        sa.Column("actor_id", UUID(), primary_key=True),
        sa.Column("entry_id", UUID(), primary_key=True),
        sa.Column("section", sa.String(16), primary_key=True),
        sa.Column("fingerprint", sa.String(64), primary_key=True),
        sa.Column(
            "source_run_id",
            UUID(),
            sa.ForeignKey("agent_supervisor_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("versions", JSONB(), nullable=False),
        sa.Column("proposals", JSONB(), nullable=False),
        sa.Column("sources", JSONB(), nullable=False),
    )


def downgrade():
    # Only the reuse index and its projections are lost. Source executions remain.
    op.drop_table("agent_lexicon_candidates")
