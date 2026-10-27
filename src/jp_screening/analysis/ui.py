"""Read-only dashboard. Opening a page never starts collection or analysis."""
import argparse
from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import select

from jp_screening.core import JST, ScreeningError
from jp_screening.analysis import schema as s
from jp_screening.analysis.config import Config
from jp_screening.analysis.db import Database, jsonable
from jp_screening.analysis.domain import dec
from jp_screening.analysis.monitoring import bucket

VIEWS = ("通過中", "上昇して通過終了", "下落して通過終了", "変化なし・要確認")


def table(rows, labels):
    if not rows:
        st.info("該当する記録はまだありません。")
        return
    frame = pd.DataFrame(jsonable(rows))
    numeric = {"price", "latest_pct", "exit_pct", "return_pct", "mean"}
    formatting = {}
    for column in numeric & set(frame.columns):
        frame[column] = frame[column].map(lambda value:None if pd.isna(value) else float(dec(value)) if dec(value) is not None else None)
        formatting[labels.get(column,column)] = st.column_config.NumberColumn(format="%.2f")
    st.dataframe(frame.rename(columns=labels), hide_index=True, width="stretch", column_config=formatting)


def pct(value):
    number = dec(value)
    return f"{number:+.2f}％" if number is not None else "確認待ち"


def date_text(value):
    return value.astimezone(JST).strftime("%Y-%m-%d %H:%M") if value else "未完了"


def detail(db, period, analysis):
    content = period["content"]
    st.write(f"通過開始：{period['start_date']}　／　最終判定：{content['last_date']}（{content['last_status']}）")
    if period["end_date"]:
        label = "終了を初めて確認した日" if content["continuity_unknown"] else "通過終了日"
        st.write(f"{label}：{period['end_date']}　／　終了時の騰落率：{pct(content['exit']['return_pct'])}")
        st.write("通過終了の理由：", content["failure"]["reason"])
        if content["failure"]["failed_conditions"]:
            st.write("満たさなくなった条件：", "、".join(content["failure"]["failed_conditions"]))
    st.write("最新時点の開始日比：", pct(content["latest"]["return_pct"]))
    if content["latest"]["status"] != "評価済み":
        st.info("株価の評価状態：" + content["latest"]["status"])
    if content["continuity_unknown"]:
        st.info("未実行・判定不能などの期間があり、通過の連続性を確認できない部分があります。")
    with db.transaction(readonly=True) as conn:
        prices = list(conn.execute(select(s.prices).where(s.prices.c.code == period["code"],
            s.prices.c.day >= period["start_date"]).order_by(s.prices.c.day.desc())).mappings())
        history = list(conn.execute(select(s.monitor_days).where(s.monitor_days.c.period_id == period["id"],
            s.monitor_days.c.active.is_(True)).order_by(s.monitor_days.c.day)).mappings())
        fetches = list(conn.execute(select(s.monitor_fetches).where(s.monitor_fetches.c.code == period["code"],
            s.monitor_fetches.c.provider.in_(["jquants", "edinet", "gdelt"]))).mappings())
    factor, chart = dec(1), []
    for quote in prices:
        chart.append({"日付":quote["day"], "調整後終値（円）":float(quote["close"] * factor)
                      if quote["close"] is not None and factor is not None else None})
        factor = factor * quote["factor"] if factor is not None and quote["factor"] is not None else None
    if chart:
        st.caption("最新の取得日を基準に分割・併合を調整した終値")
        st.line_chart(pd.DataFrame(chart).set_index("日付").sort_index())
    st.write("自動分析")
    if analysis:
        st.write(analysis["content"]["conclusion"])
        for factor in analysis["content"].get("factors", []):
            st.write(factor["category"] + "：" + factor["title"])
            st.text(factor["quote"])
            st.link_button("根拠の原資料を開く", factor["url"])
        for note in analysis["content"].get("limitations", []):
            st.caption(note)
        st.caption("分析保存日時：" + date_text(analysis["created_at"]))
    else:
        st.info("自動分析の保存を待っています。")
    with st.expander("判定履歴・資料の取得状況"):
        table([dict(day=r["day"], status=r["content"]["status"],
            return_pct=r["content"].get("measurement", {}).get("return_pct"), run_id=r["run_id"]) for r in history],
            {"day":"判定日", "status":"第2段階までの判定", "return_pct":"開始日比騰落率（％）", "run_id":"実行ID"})
        names = {"jquants":"決算・業績予想", "edinet":"企業開示（EDINET）", "gdelt":"ニュース（GDELT）"}
        states = {"ready":"取得済み", "no_data":"該当情報なし", "waiting_key":"初回設定待ち", "retry":"再試行待ち", "partial":"一部取得"}
        table([dict(provider=names[r["provider"]], state=states.get(r["state"], r["state"]),
            note=r["content"].get("note", ""), updated=date_text(r["updated_at"])) for r in fetches],
            {"provider":"情報源", "state":"取得状況", "note":"詳細", "updated":"最終確認日時"})
        st.caption("判定ルール：" + period["rules_key"][:12] + "　／　基準価格は通過開始日の終値。配当・売買費用は含みません。")


def render(config):
    db = Database(config.reader_url)
    with db.transaction(readonly=True) as conn:
        periods = [dict(r) for r in conn.execute(select(s.monitor_periods).where(s.monitor_periods.c.active.is_(True)).order_by(
            s.monitor_periods.c.start_date.desc(), s.monitor_periods.c.code)).mappings()]
        ids = [p["analysis_id"] for p in periods if p["analysis_id"]]
        saved = list(conn.execute(select(s.monitor_analyses).where(s.monitor_analyses.c.id.in_(ids))).mappings())
        latest_report = conn.execute(select(s.monitor_analyses).where(s.monitor_analyses.c.kind == "summary").order_by(
            s.monitor_analyses.c.created_at.desc()).limit(1)).mappings().first()
        jobs = conn.execute(select(s.jobs).order_by(s.jobs.c.updated_at.desc()).limit(1)).mappings().first()
    latest = {}
    for row in saved:
        if row["kind"] == "individual":
            matching = next((p for p in periods if p["id"] == row["period_id"]), None)
            if matching and row["content"].get("period_digest") == matching["digest"]:
                latest.setdefault(row["period_id"], row)
    current_report = jobs["content"].get("monitoring", {}).get("research", {}).get("report_id") if jobs else None
    with db.transaction(readonly=True) as conn:
        report = conn.execute(select(s.monitor_analyses).where(s.monitor_analyses.c.id == current_report)).mappings().first() if current_report else None
    report = report or latest_report
    journal = config.root / "data" / "analysis_job.json"
    status = json.loads(journal.read_text()) if journal.exists() else (jobs["content"] if jobs else {})
    st.caption("自動更新：営業日18時以降　／　最終成功：" + str(status.get("last_success") or "初回の更新を待っています。"))
    errors = status.get("errors", {})
    if errors:
        st.warning("更新を再試行しています。" + " ".join(str(v) for v in errors.values()))
    if not config.edinet_api_key:
        st.info("企業開示（EDINET）は初回のAPIキー設定待ちです。株価・判定の記録と、利用可能な資料での自動分析は継続します。")
    waiting_indexes = status.get("monitoring", {}).get("research", {}).get("pending_indexes", 0)
    if waiting_indexes:
        st.info(f"資料の検索索引を準備中です（{waiting_indexes}件）。定期処理で自動再試行します。")
    if status.get("monitoring", {}).get("research_error"):
        st.warning(status["monitoring"]["research_error"])
    if any(r["state"] == "waiting_model" for r in latest.values()):
        st.info("ローカルモデルの分析待ちがあります。定期処理で自動再試行します。")
    if report:
        st.write("自動分析のまとめ")
        st.write(report["content"]["conclusion"])
        st.caption(report["content"]["note"])
        with st.expander("全体比較の件数と根拠"):
            records = []
            for rule, value in report["content"].get("rules", {}).items():
                for direction, group in value["groups"].items():
                    records.append(dict(rule=rule[:12], group="上昇して通過終了" if direction == "up" else "下落して通過終了",
                        events=group["events"], securities=group["securities"], mean=group["mean_return"], state=value["state"]))
            table(records, {"rule":"判定ルール", "group":"比較グループ", "events":"通過期間の件数", "securities":"銘柄数", "mean":"平均騰落率（％）", "state":"分析状態"})
            material_rows = [dict(group="上昇終了" if direction == "up" else "下落終了", category=category, events=count)
                for direction, categories in report["content"].get("materials", {}).items() for category,count in categories.items()]
            if material_rows:
                table(material_rows, {"group":"比較グループ", "category":"関連資料の分類", "events":"該当する通過期間の件数"})
    columns = st.columns(4)
    for column, view in zip(columns, VIEWS):
        column.metric(view, sum(bucket(p) == view for p in periods))
    for tab, view in zip(st.tabs(VIEWS), VIEWS):
        with tab:
            entries = [p for p in periods if bucket(p) == view]
            records = []
            for p in entries:
                content = p["content"]
                records.append(dict(code=p["code"], name=content["name"], start=p["start_date"], end=p["end_date"],
                    price=content["latest"]["end_close"], latest_pct=content["latest"]["return_pct"],
                    exit_pct=content["exit"]["return_pct"] if content["exit"] else None,
                    reason=content["failure"]["reason"] if content["failure"] else content["last_status"],
                    conclusion=latest[p["id"]]["content"]["conclusion"] if p["id"] in latest else "自動分析待ち"))
            visible = ("code", "name", "start", "price", "latest_pct") if view == "通過中" else (
                ("code", "name", "start", "end", "exit_pct", "reason", "conclusion") if view != "変化なし・要確認" else
                ("code", "name", "start", "reason", "conclusion"))
            records = [{key:record[key] for key in visible} for record in records]
            table(records, {"code":"銘柄コード", "name":"銘柄名", "start":"通過開始日", "end":"通過終了の確認日",
                "price":"最新調整後終値（円）", "latest_pct":"最新の開始日比（％）", "exit_pct":"終了時騰落率（％）",
                "reason":"判定・終了理由", "conclusion":"自動分析の結論"})
            if entries:
                by_id = {p["id"]:p for p in entries}
                selected = st.selectbox("銘柄の記録を見る", list(by_id), key="view-"+view,
                    format_func=lambda x: f"{by_id[x]['code']} {by_id[x]['content']['name']} ／ {by_id[x]['start_date']}")
                detail(db, by_id[selected], latest.get(selected))
    db.engine.dispose()


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    args, _ = parser.parse_known_args()
    st.set_page_config(page_title="第2段階 通過銘柄の記録", layout="wide")
    st.title("第2段階 通過銘柄の記録")
    st.caption("登録・株価更新・分析は自動で進みます。銘柄を選ぶと、履歴と分析の根拠を確認できます。")
    config = Config.load(args.root)
    @st.fragment(run_every="60s")
    def dashboard():
        try:
            render(config)
        except ScreeningError as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error(f"記録の読み込みを再試行しています（{type(exc).__name__}）。")
    dashboard()


if __name__ == "__main__":
    main()
