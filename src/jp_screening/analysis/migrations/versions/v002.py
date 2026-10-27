"""Add automatic stage-two monitoring without removing research history."""
from alembic import op
from jp_screening.analysis.schema import metadata

revision = "analysis_002"
down_revision = "analysis_001"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("monitor_periods", "monitor_revisions", "monitor_days", "monitor_sources",
                 "monitor_fetches", "monitor_analyses"):
        metadata.tables[name].create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Restore a backup to roll back; monitoring history is never dropped.")
