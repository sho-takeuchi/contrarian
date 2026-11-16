"""Retain recommendation, trade snapshot and daily portfolio history."""
from alembic import op
from jp_screening.analysis.schema import metadata

revision = "analysis_003"
down_revision = "analysis_002"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("portfolio_sources", "portfolio_imports", "portfolio_recommendations",
                 "portfolio_trades", "portfolio_links", "portfolio_values"):
        metadata.tables[name].create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Restore a backup to roll back; trade history is never dropped.")
