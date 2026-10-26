"""Initial local research schema."""
from alembic import op
from jp_screening.analysis.schema import metadata

revision = "analysis_001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    metadata.create_all(op.get_bind())


def downgrade():
    raise RuntimeError("Data-preserving migrations only; restore a backup to roll back.")
