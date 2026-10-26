from alembic import context
from jp_screening.analysis.schema import metadata

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=metadata,
                  version_table="analysis_schema_version")
with context.begin_transaction():
    context.run_migrations()
