"""Persist usable external dictionary references independently of generation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_lexicon_materials"
down_revision = "0009_lexicon_candidates"


def upgrade():
    op.create_table(
        "agent_lexicon_dictionary_materials",
        sa.Column("query_key", sa.String(64), primary_key=True),
        sa.Column("source", sa.String(100), primary_key=True),
        sa.Column("material", JSONB(), nullable=False),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade():
    op.drop_table("agent_lexicon_dictionary_materials")
