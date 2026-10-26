from contextlib import contextmanager
import json
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from ..core import ScreeningError


def jsonable(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class Database:
    def __init__(self, url):
        if not url or not url.startswith("postgresql+psycopg://"):
            raise ScreeningError("ANALYSIS_DATABASE_URLにpostgresql+psycopgの接続先を設定してください。")
        self.engine = create_engine(url, pool_pre_ping=True, hide_parameters=True,
                                    poolclass=NullPool,
                                    connect_args={"connect_timeout":5},
                                    json_serializer=lambda v:json.dumps(v, ensure_ascii=False, default=str))

    @contextmanager
    def transaction(self, readonly=False):
        try:
            with self.engine.begin() as conn:
                if readonly:
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text("SET LOCAL statement_timeout = '60s'"))
                yield conn
        except SQLAlchemyError:
            raise ScreeningError("分析DBへの接続・処理に失敗しました。doctorとDB設定を確認してください。") from None

    def upgrade(self):
        from alembic import command
        from alembic.config import Config
        cfg = Config()
        cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self.transaction() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")

    def ping(self):
        with self.transaction(readonly=True) as conn:
            return conn.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'")).scalar_one()
