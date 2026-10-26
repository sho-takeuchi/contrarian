import csv
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from sqlalchemy import select, update, text
from sqlalchemy.dialects.postgresql import insert

from ..core import ScreeningError, code5
from ..reporting import BASE_FIELDS, HEADERS, WEEKLY_FIELDS, WEEKLY_HEADERS, DAILY_FIELDS, DAILY_HEADERS
from . import schema as s
from .db import jsonable
from .domain import features, digest, dec, stage_status, build_episodes, FEATURE_VERSION

HEADERS_MAP = dict(zip(HEADERS + WEEKLY_HEADERS + DAILY_HEADERS,
                       BASE_FIELDS + WEEKLY_FIELDS + DAILY_FIELDS))
STATUSES = {"通過", "不通過", "判定不能", "対象外", "未対象", "未評価"}


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not {"銘柄コード", "判定", "判定日"}.issubset(reader.fieldnames or []):
            raise ScreeningError("結果CSVの必須列がありません。")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ScreeningError("結果CSVに重複列があります。")
        return [{HEADERS_MAP.get(k, k):v for k, v in row.items()} for row in reader]


def prepare_run(folder):
    folder = Path(folder).resolve()
    if (folder / "error.json").exists() or not (folder / "run.json").is_file():
        raise ScreeningError("正常完了したrun.jsonのあるフォルダーが必要です。")
    raw_manifest = (folder / "run.json").read_bytes()
    manifest = json.loads(raw_manifest)
    if manifest["run_id"] != folder.name:
        raise ScreeningError("実行IDとフォルダー名が一致しません。")
    version = manifest.get("format_version", 1)
    if version not in (1, 2):
        raise ScreeningError("未対応の実行形式です。")
    hashed = {}
    for name, expected in manifest.get("artifact_hashes", {}).items():
        if Path(name).name != name or not name.endswith(".csv"):
            raise ScreeningError("出力ファイル名の形式が不正です。")
        actual = sha256((folder / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ScreeningError("CSVのハッシュが一致しません。")
        hashed[name] = actual
    stage = manifest.get("settings", {}).get("stage", 1)
    if stage not in (1, 2, 3):
        raise ScreeningError("未対応の判定段階です。")
    names = ["all_results.csv", "candidates.csv"]
    if stage >= 2:
        names.append("stage1_candidates.csv")
    if stage == 3:
        names.append("stage2_candidates.csv")
    for name in names:
        hashed[name] = sha256((folder / name).read_bytes()).hexdigest()
        if version == 2 and name not in manifest.get("artifact_hashes", {}):
            raise ScreeningError("必須CSVのハッシュ記録がありません。")
    asof = date.fromisoformat(manifest["asof"])
    payloads = csv_rows(folder / "all_results.csv")
    if len(payloads) != manifest["counts"]["total"]:
        raise ScreeningError("結果の件数が実行記録と一致しません。")
    seen, rows = set(), []
    for payload in payloads:
        code = code5(payload["code"])
        if code in seen or payload["asof"] != str(asof):
            raise ScreeningError("銘柄重複または判定日不一致です。")
        seen.add(code)
        flags = [payload.get("stage1_status", "") if stage >= 2 else payload["status"],
                 payload.get("stage2_status", "") if stage >= 2 else "未評価",
                 payload.get("stage3_status", "") if stage == 3 else "未評価"]
        if any(x not in STATUSES for x in flags) or payload["status"] not in STATUSES:
            raise ScreeningError("判定列の値が不正です。")
        row = dict(run_id=manifest["run_id"], code=code, name=payload.get("name", ""),
                   product=payload.get("product", ""), s1=flags[0], s2=flags[1], s3=flags[2],
                   close=dec(payload.get("current_close")), features=jsonable(features(payload)), payload=payload)
        rows.append(row)
    for status in ("通過", "不通過", "判定不能", "対象外"):
        if sum(r["payload"]["status"] == status for r in rows) != manifest["counts"].get(status, 0):
            raise ScreeningError("判定別件数が実行記録と一致しません。")
    for name in names[1:]:
        which = 1 if name == "stage1_candidates.csv" else 2 if name == "stage2_candidates.csv" else stage
        candidates = csv_rows(folder / name)
        expected = {r["code"] for r in rows if stage_status(r, which) == "通過"}
        codes = [code5(r["code"]) for r in candidates]
        if len(codes) != len(set(codes)) or set(codes) != expected:
            raise ScreeningError("候補CSVと段階別判定が一致しません。")
        if any(r["asof"] != str(asof) for r in candidates):
            raise ScreeningError("候補CSVの判定日が一致しません。")
    scope = manifest.get("source_scope", "unknown")
    settings = dict(manifest.get("settings", {}))
    settings.pop("stage", None)
    # One code version identifies the same full rule set across execution depths.
    rules_key = digest([manifest.get("rules_version", "legacy"),
                        settings.get("monthly_close_policy", "month_end")])
    created = datetime.fromisoformat(manifest["created_at"])
    if created.tzinfo is None:
        raise ScreeningError("実行日時にタイムゾーンが必要です。")
    run = dict(id=manifest["run_id"], asof=asof, created_at=created, stage=stage, scope=scope,
               eligible=(scope == "all" and manifest.get("mode") == "all" and not manifest.get("synthetic")
                         and bool(manifest.get("rules_version"))),
               rules_key=rules_key, digest=digest([raw_manifest.hex(), hashed]),
               source=str(folder), manifest=manifest)
    return run, rows


def refresh_episodes(conn):
    conn.execute(text("SELECT pg_advisory_xact_lock(18426001)"))
    runs = list(conn.execute(select(s.runs)).mappings())
    rows = list(conn.execute(select(s.results.c.run_id, s.results.c.code,
        s.results.c.s1, s.results.c.s2, s.results.c.s3)).mappings())
    sessions = set(conn.execute(select(s.calendar.c.day).where(s.calendar.c.kind.in_(["1", "2"]))).scalars())
    events = build_episodes(runs, rows, sessions)
    conn.execute(update(s.episodes).values(active=False))
    for event in events:
        statement = insert(s.episodes).values(**event, active=True)
        conn.execute(statement.on_conflict_do_update(index_elements=["id"], set_={
            "active":True, "continuity_unknown":event["continuity_unknown"]}))


def import_run(db, folder):
    run, rows = prepare_run(folder)
    with db.transaction() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(18426001)"))
        existing = conn.execute(select(s.runs.c.digest).where(s.runs.c.id == run["id"])).scalar_one_or_none()
        if existing:
            if existing != run["digest"]:
                raise ScreeningError("同じ実行IDの内容が異なります。履歴は上書きしません。")
            return "skipped"
        conn.execute(s.runs.insert().values(**run))
        if rows:
            conn.execute(s.results.insert(), rows)
            values = [dict(run_id=run["id"], code=r["code"], name=k, value=dec(v), version=FEATURE_VERSION)
                      for r in rows for k, v in r["features"].items()]
            conn.execute(s.feature_values.insert(), values)
        for item in run["manifest"]["calendar"]:
            statement = insert(s.calendar).values(day=date.fromisoformat(item["Date"]), kind=str(item["HolDiv"]))
            conn.execute(statement.on_conflict_do_nothing(index_elements=["day"]))
        if run["eligible"]:
            from sqlalchemy import func
            for row in rows:
                if row["s1"] == "通過":
                    statement = insert(s.tracking).values(code=row["code"], first_date=run["asof"], paused=False,
                                                          missing_days=0, state="未更新")
                    conn.execute(statement.on_conflict_do_update(index_elements=["code"],
                        set_={"first_date":func.least(s.tracking.c.first_date, run["asof"])}))
        refresh_episodes(conn)
    return "imported"


def import_runs(db, root):
    root = Path(root).resolve()
    folders = [root] if (root / "run.json").exists() else sorted(p.parent for p in root.glob("*/run.json"))
    report = []
    for folder in folders:
        try:
            report.append(dict(run=folder.name, status=import_run(db, folder)))
        except (ScreeningError, OSError, ValueError, KeyError):
            report.append(dict(run=folder.name, status="failed", reason="実行形式・件数・ハッシュ・DB接続を確認してください。"))
    return report


def review_legacy_run(db, run_id, rules_version, note):
    """Explicitly attest an old full-universe run; preserve its original manifest."""
    if not rules_version.strip() or not note.strip():
        raise ScreeningError("当時のルール識別子と全銘柄実行を確認した根拠が必要です。")
    with db.transaction() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(18426001)"))
        run = conn.execute(select(s.runs).where(s.runs.c.id == run_id)).mappings().first()
        if not run or run["scope"] != "unknown" or run["manifest"].get("mode") != "all" or run["manifest"].get("synthetic"):
            raise ScreeningError("対象範囲が不明な旧形式の全件実行だけを確認できます。デモ・指定銘柄・再計算は対象外です。")
        rule = digest([rules_version, run["manifest"].get("settings", {}).get("monthly_close_policy", "month_end")])
        conn.execute(s.run_reviews.insert().values(run_id=run_id, rules_version=rules_version,
            reviewed_at=datetime.now(timezone.utc), note=note))
        conn.execute(update(s.runs).where(s.runs.c.id == run_id).values(scope="all", eligible=True, rules_key=rule))
        from sqlalchemy import func
        for code in conn.execute(select(s.results.c.code).where(s.results.c.run_id == run_id, s.results.c.s1 == "通過")).scalars():
            statement = insert(s.tracking).values(code=code, first_date=run["asof"], paused=False)
            conn.execute(statement.on_conflict_do_update(index_elements=["code"],
                set_={"first_date":func.least(s.tracking.c.first_date, run["asof"])}))
        refresh_episodes(conn)
    return dict(run_id=run_id, status="reviewed", rules_key=rule)
