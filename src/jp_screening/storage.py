"""Transactional raw cache. Completed file versions make interrupted runs resumable."""
from contextlib import closing
from datetime import date
from hashlib import sha256
import csv
import gzip
import json
from pathlib import Path
import sqlite3

from .core import ScreeningError, code5, number


def json_text(obj):
    return json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True)


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path, timeout=30)
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS prices (
            code TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL, source TEXT NOT NULL,
            PRIMARY KEY(code,day));
          CREATE INDEX IF NOT EXISTS prices_source ON prices(source);
          CREATE TABLE IF NOT EXISTS files (key TEXT PRIMARY KEY, modified TEXT, size INTEGER, digest TEXT);
          CREATE TABLE IF NOT EXISTS api_prices (
            code TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(code,day));
          CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, manifest TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def file_current(self, item):
        row = self.db.execute("SELECT modified,size FROM files WHERE key=?", (item["Key"],)).fetchone()
        return bool(row and row == (str(item["LastModified"]), int(item["Size"])))

    def replace_source(self, key, rows, manifest=None):
        # File content and its completion marker commit together or roll back together.
        with self.db:
            self.db.execute("DELETE FROM prices WHERE source=?", (key,))
            seen = set()
            batch = []
            for row in rows:
                code, day = code5(row["Code"]), date.fromisoformat(row["Date"]).isoformat()
                if (code, day) in seen:
                    raise ScreeningError("取得ファイル内に重複した日足があります。")
                seen.add((code, day))
                row = {**row, "Code": code, "Date": day}
                batch.append((code, day, json_text(row), key))
                if len(batch) >= 2000:
                    self.db.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?)", batch)
                    batch.clear()
            self.db.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?)", batch)
            if manifest:
                self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)", manifest)

    def import_gzip(self, item, path):
        digest = sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024*1024), b""):
                digest.update(chunk)
        if path.stat().st_size != int(item["Size"]):
            raise ScreeningError("一括ファイルのサイズが一覧と一致しません。")
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if not {"Date", "Code", "O", "H", "L", "C", "AdjFactor"}.issubset(reader.fieldnames or []):
                raise ScreeningError("一括ファイルの必須列が不足しています。")
            self.replace_source(item["Key"], reader,
                (item["Key"], str(item["LastModified"]), int(item["Size"]), digest.hexdigest()))

    def prices(self, code, start, end, api=False):
        table = "api_prices" if api else "prices"
        cursor = self.db.execute(f"SELECT payload FROM {table} WHERE code=? AND day>=? AND day<=? ORDER BY day",
                                 (code, str(start), str(end)))
        return [json.loads(row[0]) for row in cursor]

    def save_api(self, code, rows):
        with self.db:
            self.db.execute("DELETE FROM api_prices WHERE code=?", (code,))
            self.db.executemany("INSERT INTO api_prices VALUES (?,?,?)",
                [(code5(r["Code"]), r["Date"], json_text(r)) for r in rows])

    def split_codes(self):
        # This is run once after import, not once per candidate.
        codes = set()
        for code, payload in self.db.execute("SELECT code,payload FROM prices"):
            factor = number(json.loads(payload).get("AdjFactor"))
            if factor is not None and factor != 1:
                codes.add(code)
        return codes

    def file_manifest(self):
        return [dict(zip(("key", "modified", "size", "digest"), row))
                for row in self.db.execute("SELECT * FROM files ORDER BY key")]

    def save_run(self, run_id, manifest):
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (?,?)", (run_id, json_text(manifest)))

    def load_run(self, run_id):
        row = self.db.execute("SELECT manifest FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ScreeningError("指定した実行記録がありません。")
        return json.loads(row[0])

