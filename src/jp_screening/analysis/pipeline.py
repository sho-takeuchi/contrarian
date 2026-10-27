from datetime import datetime, time, timezone, timedelta
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..api import Client
from ..core import JST, ScreeningError, TradingCalendar
from ..runner import initialize, run_lock, run_live
from . import schema as s
from .domain import digest
from .ingest import import_runs, prepare_run
from .tracking import latest_closed_day, update_prices, evaluate_outcomes
from .monitoring import rebuild
from .research import automatic_research


def daily_update(config, db, if_due=False, now=None, client=None):
    now = (now or datetime.now(JST)).astimezone(JST)
    root = config.root
    initialize(root)
    journal_path = root / "data" / "analysis_job.json"
    with run_lock(root):
        journal = json.loads(journal_path.read_text()) if journal_path.exists() else {}
        journal["last_attempt"] = now.isoformat()
        journal["errors"] = {}
        journal["complete"] = False

        def save():
            tmp = journal_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(journal, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
            tmp.replace(journal_path)

        try:
            client = client or Client(config.api_key)
            cal = TradingCalendar.from_rows(list(client.rows("/markets/calendar")))
            cutoff = latest_closed_day(cal, now)
            if cutoff == now.date() and now.hour < 18:
                cutoff = max(day for day in cal.sessions(now.date()-timedelta(days=30), now.date()) if day < now.date())
        except Exception:
            journal["errors"]["calendar"] = "取引カレンダー・API認証を確認してください。"
            try:
                journal["imports"] = import_runs(db, root / "outputs")
                journal["evaluations"] = evaluate_outcomes(db, now)
                journal["monitoring"] = {"projection":rebuild(db, now)}
            except Exception:
                journal["errors"]["imports_or_evaluation"] = "保存済みデータの取り込み・評価も未完了です。"
            save()
            return journal
        day = str(now.date())
        screen_due = cal.days.get(now.date()) in {"1", "2"} and now.time() >= time(18)
        # Never invent historical runs when the computer missed an earlier date.
        if screen_due and journal.get("screening_day") != day:
            try:
                completed = []
                for path in (root / "outputs").glob("*/run.json"):
                    try:
                        run, _ = prepare_run(path.parent)
                        if run["eligible"] and run["asof"] == now.date() and run["stage"] >= 2:
                            completed.append(run)
                    except (ScreeningError, OSError, ValueError, KeyError):
                        continue
                if completed:
                    folder = Path(max(completed, key=lambda r:r["created_at"])["source"])
                else:
                    published = client.daily(day=now.date())
                    if not published or any(r["Date"] != day for r in published) or not any(r.get("C") for r in published):
                        raise ScreeningError("当日の価格が未公開です。")
                    folder, _ = run_live(root, "all", config.api_key, stage=2)
                completed_run, _ = prepare_run(folder)
                if completed_run["asof"] != now.date():
                    raise ScreeningError("当日の判定記録ではありません。")
                journal.update(screening_day=day, screening_run=folder.name)
                save()  # Durable checkpoint before DB access.
            except Exception:
                journal["errors"]["screening"] = "スクリーニング未完了。API公開状況・出力のerror.jsonを確認してください。"
        try:
            imported = import_runs(db, root / "outputs")
            journal["imports"] = imported
            if any(r["status"] == "failed" for r in imported):
                journal["errors"]["imports"] = "取り込めない履歴があります。取り込み画面で確認してください。"
        except Exception:
            journal["errors"]["imports"] = "DB取り込み失敗。CSVは保存されています。"
        try:
            projection = rebuild(db, now)
            journal["monitoring"] = {**journal.get("monitoring", {}), "projection":projection}
            with db.transaction(readonly=True) as conn:
                codes = set(conn.execute(select(s.monitor_periods.c.code).distinct()).scalars())
                targets = list(conn.execute(select(s.tracking.c.code, s.tracking.c.first_date).where(
                    s.tracking.c.paused.is_(False), s.tracking.c.code.in_(codes)).order_by(s.tracking.c.code)).mappings())
            target_key = digest([str(cutoff), [dict(r) for r in targets]])
            if not if_due or journal.get("prices_key") != target_key:
                price_report = update_prices(db, config.api_key, now=now, client=client, codes=codes, asof=cutoff)
                journal["prices"] = price_report
                if price_report["complete"]:
                    journal["prices_key"] = target_key
                else:
                    journal["errors"]["prices"] = "未取得の銘柄・営業日があります。次回再試行します。"
            with db.transaction(readonly=True) as conn:
                run_versions = [tuple(r) for r in conn.execute(select(s.runs.c.id, s.runs.c.digest).order_by(s.runs.c.id))]
                price_versions = [tuple(r) for r in conn.execute(select(s.prices.c.code, s.prices.c.day,
                    s.prices.c.revision).order_by(s.prices.c.code, s.prices.c.day))]
            data_key = digest([target_key, run_versions, price_versions])
            # A retry with incomplete prices recomputes labels as well.
            if not if_due or journal.get("evaluation_key") != data_key or "prices" in journal["errors"]:
                journal["evaluations"] = evaluate_outcomes(db, now)
                journal["monitoring"]["projection"] = rebuild(db, now)
                journal["evaluation_key"] = data_key
        except Exception:
            journal["errors"]["prices_or_evaluation"] = "株価更新・評価が未完了です。DB・API設定を確認してください。"
        save()  # Numerical observations survive a slow or unavailable research service.
        if not if_due or now.time() >= time(18):
            try:
                research = automatic_research(db, config, now, client)
                journal.setdefault("monitoring", {})["research"] = research
                journal["monitoring"].pop("research_error", None)
                journal["reports"] = [research["report_id"]]
            except Exception:
                journal.setdefault("monitoring", {})["research_error"] = "自動分析を再試行します。判定・株価は保存済みです。"
        journal["complete"] = not journal["errors"]
        if journal["complete"]:
            journal["last_success"] = now.isoformat()
        save()
        try:
            with db.transaction() as conn:
                statement = insert(s.jobs).values(id=day, updated_at=now, content=journal)
                conn.execute(statement.on_conflict_do_update(index_elements=["id"], set_={"updated_at":now, "content":journal}))
        except ScreeningError:
            pass  # The filesystem journal also survives a database outage.
        return journal
