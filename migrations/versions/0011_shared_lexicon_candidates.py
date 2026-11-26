"""Index candidate lookup across editors of the same entry."""

import sqlalchemy as sa
from alembic import op

revision = "0011_shared_lexicon_candidates"
down_revision = "0010_lexicon_materials"


def upgrade():
    op.create_index(
        "ix_lexicon_candidates_entry_scope",
        "agent_lexicon_candidates",
        [
            "entry_id",
            "section",
            "fingerprint",
            sa.text("run_created DESC"),
            sa.text("source_run_id DESC"),
        ],
    )


def downgrade():
    op.drop_index("ix_lexicon_candidates_entry_scope", table_name="agent_lexicon_candidates")
