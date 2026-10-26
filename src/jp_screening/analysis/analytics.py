from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, and_

from ..core import ScreeningError
from ..reporting import SafeCsvWriter, write_json
from . import schema as s
from .db import jsonable
from .domain import representative, stage_status, summarize, quartiles, FEATURE_NAMES, FEATURE_VERSION, CALCULATION_VERSION, HORIZONS, digest


def observations(conn, start=None, end=None, rules_key=None):
    runs = representative(list(conn.execute(select(s.runs)).mappings()))
    runs = [r for r in runs if (not start or r["asof"] >= start) and (not end or r["asof"] <= end)]
    keys = sorted({r["rules_key"] for r in runs})
    if not rules_key and len(keys) > 1:
        raise ScreeningError("複数の判定ルールがあります。ルールを指定して分析してください。")
    if rules_key:
        runs = [r for r in runs if r["rules_key"] == rules_key]
    by_id = {r["id"]:r for r in runs}
    rows = list(conn.execute(select(s.results).where(s.results.c.run_id.in_(by_id))).mappings())
    return runs, [dict(r, asof=by_id[r["run_id"]]["asof"], rules_key=by_id[r["run_id"]]["rules_key"]) for r in rows]


def condition_report(db, feature="daily_ma_gap", horizon=20, basis="close", start=None, end=None,
                     rules_key=None, persist=True, output_root=None, codes=None):
    if feature not in FEATURE_NAMES or horizon not in (0, *HORIZONS) or basis not in {"close", "next_open"}:
        raise ScreeningError("比較条件が不正です。")
    if start and end and start > end:
        raise ScreeningError("期間の順序が逆です。")
    with db.transaction(readonly=True) as conn:
        runs, rows = observations(conn, start, end, rules_key)
        if codes:
            rows = [r for r in rows if r["code"] in codes]
        events = {(r["run_id"], r["code"]) for r in conn.execute(select(s.episodes).where(
            s.episodes.c.active.is_(True), s.episodes.c.stage == 3)).mappings()}
        measured = {(r["run_id"], r["code"]):r for r in conn.execute(select(s.outcomes).where(
            s.outcomes.c.run_id.in_([r["id"] for r in runs]), s.outcomes.c.horizon == horizon,
            s.outcomes.c.basis == basis)).mappings()}
    items = []
    for row in rows:
        value = measured.get((row["run_id"], row["code"]))
        items.append({**row, "status":value["status"] if value else "未評価",
                      "return_pct":value["return_pct"] if value else None,
                      "outcome_provenance":value["payload"] if value else None})
    entries = [r for r in items if (r["run_id"], r["code"]) in events]
    cohorts = {}
    dates = {(r["asof"], r["product"]) for r in entries}
    for row in items:
        key = (row["asof"], row["product"])
        if key not in dates:
            continue
        if (row["run_id"], row["code"]) in events:
            group = "第3段階新規通過"
        elif row["s1"] == "通過" and row["s2"] == "不通過":
            group = "第1段階まで"
        elif row["s2"] == "通過" and row["s3"] == "不通過":
            group = "第2段階まで"
        elif stage_status(row, 3) == "判定不能":
            group = "判定不能・未評価"
        else:
            continue  # Existing continuous stage-three passes aren't new signals.
        cohorts.setdefault((*key, group), []).append(row)
    content = dict(quartiles=quartiles(entries, feature), summary=summarize(entries),
        cohorts=[dict(asof=str(day), product=product, group=group, **summarize(values))
                 for (day, product, group), values in sorted(cohorts.items())],
        observations=entries,
        note="観測された関連の比較です。因果効果や将来の値上がりを示しません。配当・売買費用は含みません。")
    parameters = dict(feature=feature, horizon=horizon, basis=basis, start=str(start or ""), end=str(end or ""),
                      rules_key=rules_key, codes=codes or [], feature_version=FEATURE_VERSION,
                      calculation_version=CALCULATION_VERSION,
                      runs=[dict(id=r["id"], digest=r["digest"]) for r in runs])
    report = dict(id=digest([parameters, content]), created_at=datetime.now(timezone.utc), parameters=parameters, content=content)
    if persist:
        from sqlalchemy.dialects.postgresql import insert
        with db.transaction() as conn:
            conn.execute(insert(s.reports).values(**report).on_conflict_do_nothing(index_elements=["id"]))
            report["created_at"] = conn.execute(select(s.reports.c.created_at).where(s.reports.c.id == report["id"])).scalar_one()
        if output_root:
            folder = Path(output_root) / report["id"]
            folder.mkdir(parents=True, exist_ok=True)
            write_json(folder / "report.json", report)
            with (folder / "conditions.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = SafeCsvWriter(f)
                keys = ["quartile", "total", "valid", "pending", "missing", "unique_codes", "mean", "median", "positive_pct"]
                writer.writerow(keys)
                for row in content["quartiles"]["groups"]:
                    writer.writerow([row[k] for k in keys])
    return jsonable(report)


def query(db, intent):
    """A closed set of read-only operations; no generated SQL is accepted."""
    if intent.kind == "conditions":
        report = condition_report(db, intent.feature, intent.horizon, intent.basis, intent.start,
                                  intent.end, intent.rules_key, persist=False, codes=intent.codes)
        # Preserve aggregated results; keep large samples out of the LLM context.
        report["content"]["observations"] = report["content"]["observations"][:20]
        return report
    with db.transaction(readonly=True) as conn:
        runs, rows = observations(conn, intent.start, intent.end, intent.rules_key)
        if intent.codes:
            rows = [r for r in rows if r["code"] in intent.codes]
        provenance = [dict(id=r["id"], asof=str(r["asof"]), settings=r["manifest"].get("settings")) for r in runs]
        if intent.kind == "candidates":
            latest = max((r["asof"] for r in runs), default=None)
            output = [r for r in rows if r["asof"] == latest and stage_status(r, intent.stage) == "通過"]
        elif intent.kind == "frequency":
            counts = {}
            for r in rows:
                if stage_status(r, intent.stage) == "通過":
                    counts.setdefault(r["code"], dict(code=r["code"], name=r["name"], count=0))["count"] += 1
            output = sorted(counts.values(), key=lambda r:(-r["count"], r["code"]))
        elif intent.kind == "differences":
            dates = sorted({r["asof"] for r in runs})
            if len(dates) < 2:
                raise ScreeningError("比較できる2日分の記録がありません。")
            before = {r["code"]:r for r in rows if r["asof"] == dates[-2]}
            after = {r["code"]:r for r in rows if r["asof"] == dates[-1]}
            output = []
            for code in sorted(before.keys() | after.keys()):
                a = stage_status(before[code], intent.stage) if code in before else "未観測"
                b = stage_status(after[code], intent.stage) if code in after else "未観測"
                if a != b:
                    output.append(dict(code=code, before=a, after=b, from_date=dates[-2], to_date=dates[-1],
                                       reason=after.get(code, {}).get("payload", {}).get("reason", "")))
        elif intent.kind == "stages":
            output = []
            for run in runs:
                data = [r for r in rows if r["run_id"] == run["id"]]
                counts = [sum(stage_status(r, i) == "通過" for r in data) for i in (1,2,3)]
                output.append(dict(asof=run["asof"], run_id=run["id"], total=len(data),
                                   stage1=counts[0], stage2=counts[1] if run["stage"] >= 2 else None,
                                   stage3=counts[2] if run["stage"] == 3 else None))
        elif intent.kind == "performance":
            events = {(r["run_id"], r["code"]) for r in conn.execute(select(s.episodes).where(
                s.episodes.c.active.is_(True), s.episodes.c.stage == intent.stage)).mappings()}
            eligible = {(r["run_id"], r["code"]) for r in rows} & events
            origins = {(r["run_id"], r["code"]):r for r in rows}
            output = [dict(r, asof=origins[r["run_id"], r["code"]]["asof"],
                           name=origins[r["run_id"], r["code"]]["name"]) for r in conn.execute(select(s.outcomes).where(
                s.outcomes.c.horizon == intent.horizon, s.outcomes.c.basis == intent.basis,
                s.outcomes.c.run_id.in_([r["id"] for r in runs]))).mappings()
                if (r["run_id"], r["code"]) in eligible]
        elif intent.kind == "history":
            output = sorted(rows, key=lambda r:(r["asof"], r["code"]))
        else:
            output = []
    return jsonable(dict(rows=output[:intent.limit], total=len(output), truncated=len(output)>intent.limit,
                         runs=provenance, definition="通過回数は代表実行の観測日数。実績評価は通過開始イベント。"))
