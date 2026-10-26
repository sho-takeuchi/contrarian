from sqlalchemy import (MetaData, Table, Column, String, Text, Integer, Boolean, Date,
                        DateTime, Numeric, ForeignKey, ForeignKeyConstraint, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from pgvector.sqlalchemy import Vector

metadata = MetaData()
runs = Table("analysis_runs", metadata,
    Column("id", Text, primary_key=True), Column("asof", Date, nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False), Column("stage", Integer, nullable=False),
    Column("scope", String(20), nullable=False), Column("rules_key", Text, nullable=False),
    Column("eligible", Boolean, nullable=False), Column("digest", Text, nullable=False),
    Column("source", Text, nullable=False), Column("manifest", JSONB, nullable=False))
run_reviews = Table("analysis_run_reviews", metadata,
    Column("run_id", Text, ForeignKey(runs.c.id), primary_key=True),
    Column("reviewed_at", DateTime(timezone=True), nullable=False),
    Column("rules_version", Text, nullable=False), Column("note", Text, nullable=False))
results = Table("analysis_results", metadata,
    Column("run_id", Text, ForeignKey(runs.c.id), primary_key=True), Column("code", String(5), primary_key=True),
    Column("name", Text, nullable=False), Column("product", Text, nullable=False),
    Column("s1", Text, nullable=False), Column("s2", Text, nullable=False), Column("s3", Text, nullable=False),
    Column("close", Numeric), Column("features", JSONB, nullable=False), Column("payload", JSONB, nullable=False))
feature_values = Table("analysis_features", metadata,
    Column("run_id", Text, primary_key=True), Column("code", String(5), primary_key=True),
    Column("name", Text, primary_key=True), Column("value", Numeric), Column("version", Text, nullable=False),
    ForeignKeyConstraint(["run_id", "code"], [results.c.run_id, results.c.code]))
episodes = Table("analysis_episodes", metadata,
    Column("id", Text, primary_key=True), Column("run_id", Text, nullable=False),
    Column("code", String(5), nullable=False), Column("stage", Integer, nullable=False),
    Column("start_date", Date, nullable=False), Column("rules_key", Text, nullable=False),
    Column("continuity_unknown", Boolean, nullable=False), Column("active", Boolean, nullable=False),
    ForeignKeyConstraint(["run_id", "code"], [results.c.run_id, results.c.code]))
tracking = Table("analysis_tracking", metadata,
    Column("code", String(5), primary_key=True), Column("first_date", Date, nullable=False),
    Column("paused", Boolean, nullable=False, default=False), Column("updated_at", DateTime(timezone=True)),
    Column("missing_days", Integer, nullable=False, default=0), Column("state", Text, nullable=False, default="未更新"))
calendar = Table("analysis_calendar", metadata,
    Column("day", Date, primary_key=True), Column("kind", String(1), nullable=False))
prices = Table("analysis_prices", metadata,
    Column("code", String(5), primary_key=True), Column("day", Date, primary_key=True),
    Column("open", Numeric), Column("high", Numeric), Column("low", Numeric), Column("close", Numeric),
    Column("factor", Numeric), Column("payload", JSONB, nullable=False), Column("digest", Text, nullable=False),
    Column("revision", Integer, nullable=False), Column("fetched_at", DateTime(timezone=True), nullable=False))
price_revisions = Table("analysis_price_revisions", metadata,
    Column("code", String(5), primary_key=True), Column("day", Date, primary_key=True),
    Column("revision", Integer, primary_key=True), Column("payload", JSONB, nullable=False),
    Column("digest", Text, nullable=False), Column("fetched_at", DateTime(timezone=True), nullable=False))
outcomes = Table("analysis_outcomes", metadata,
    Column("run_id", Text, primary_key=True), Column("code", String(5), primary_key=True),
    Column("horizon", Integer, primary_key=True), Column("basis", String(10), primary_key=True),
    Column("status", Text, nullable=False), Column("return_pct", Numeric), Column("max_up_pct", Numeric),
    Column("max_down_pct", Numeric), Column("payload", JSONB, nullable=False),
    Column("evaluated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["run_id", "code"], [results.c.run_id, results.c.code]))
reports = Table("analysis_reports", metadata,
    Column("id", Text, primary_key=True), Column("created_at", DateTime(timezone=True), nullable=False),
    Column("parameters", JSONB, nullable=False), Column("content", JSONB, nullable=False))
documents = Table("analysis_documents", metadata,
    Column("id", Text, primary_key=True), Column("source", Text, nullable=False),
    Column("content_hash", Text, nullable=False), Column("profile", Text, nullable=False),
    Column("title", Text, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False),
    Column("active", Boolean, nullable=False), Column("code", String(5)), Column("document_date", Date),
    Column("excluded", Boolean, nullable=False, default=False), Column("rules_key", Text),
    Column("original", Text, nullable=False))
chunks = Table("analysis_chunks", metadata,
    Column("document_id", Text, ForeignKey(documents.c.id), primary_key=True),
    Column("ordinal", Integer, primary_key=True), Column("heading", Text, nullable=False),
    Column("body", Text, nullable=False), Column("embedding", Vector, nullable=False))
jobs = Table("analysis_jobs", metadata,
    Column("id", Text, primary_key=True), Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("content", JSONB, nullable=False))
