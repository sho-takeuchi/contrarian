"""Local Streamlit interface; numerical tables remain usable without Ollama."""
import argparse
from datetime import date
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import select, update, and_

from jp_screening.core import ScreeningError
from jp_screening.analysis import schema as s
from jp_screening.analysis.config import Config
from jp_screening.analysis.db import Database, jsonable
from jp_screening.analysis.domain import FEATURE_NAMES, HORIZONS, dec
from jp_screening.analysis.analytics import query, condition_report
from jp_screening.analysis.rag import Intent, Ollama, answer, index_document, set_document_active


def show_table(rows):
    if not rows:
        st.info("該当する記録はありません。")
        return
    frame = pd.DataFrame(jsonable(rows))
    for col in frame.columns:
        frame[col] = frame[col].map(lambda x:json.dumps(x, ensure_ascii=False) if isinstance(x, (dict,list)) else x)
    st.dataframe(frame, width="stretch", hide_index=True)


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    args, _ = parser.parse_known_args()
    st.set_page_config(page_title="contrarian 分析", layout="wide")
    st.title("contrarian 分析")
    st.caption("判定履歴と通過後の値動きを確認し、条件との関連を調べます。")
    config = Config.load(args.root)
    writer, reader = Database(config.database_url), Database(config.reader_url)
    page = st.sidebar.radio("画面", ["候補・履歴", "追跡一覧", "条件比較", "資料管理", "チャット", "更新状況"])
    with reader.transaction(readonly=True) as conn:
        rules = list(conn.execute(select(s.runs.c.rules_key).where(s.runs.c.eligible.is_(True)).distinct()).scalars())
    rule = st.sidebar.selectbox("判定ルール", rules, format_func=lambda x:x[:12]) if rules else None
    if page in {"候補・履歴", "追跡一覧", "条件比較"}:
        horizon = st.sidebar.selectbox("評価期間（営業日）", (*HORIZONS, 0), index=1,
                                      format_func=lambda x:"最新の取得済み価格" if x == 0 else str(x))
        basis = st.sidebar.selectbox("基準価格", ["close", "next_open"],
            format_func=lambda x:"判定日終値" if x == "close" else "翌営業日始値")
    if page == "候補・履歴":
        mode = st.selectbox("表示する内容", ["candidates","history","frequency","differences","stages"],
            format_func=lambda x:{"candidates":"最新の候補", "history":"判定の履歴", "frequency":"通過回数",
                                  "differences":"前回からの変化", "stages":"段階別の件数"}[x])
        code = st.text_input("銘柄コード（空欄で全銘柄）")
        stage = st.selectbox("段階", [1,2,3], index=2)
        restrict = st.checkbox("期間を指定する")
        start = st.date_input("開始日", date.today().replace(day=1)) if restrict else None
        end = st.date_input("終了日", date.today()) if restrict else None
        result = query(reader, Intent(kind=mode, codes=[code] if code else [], stage=stage,
                                      start=start, end=end, rules_key=rule, limit=500))
        st.write(f"対象 {result['total']} 件")
        show_table(result["rows"])
        if mode == "stages" and result["rows"]:
            st.line_chart(pd.DataFrame(result["rows"]).set_index("asof")[["stage1","stage2","stage3"]])
        if result["truncated"]:
            st.info("先頭500件を表示しています。期間や銘柄を絞り込めます。")
        with st.expander("根拠となる実行"):
            show_table(result["runs"])
    elif page == "追跡一覧":
        result = query(reader, Intent(kind="performance", horizon=horizon, basis=basis, rules_key=rule, limit=500))
        show_table(result["rows"])
        with reader.transaction(readonly=True) as conn:
            targets = list(conn.execute(select(s.tracking).order_by(s.tracking.c.code)).mappings())
        show_table([dict(x) for x in targets])
        if targets:
            code = st.selectbox("追跡銘柄", [r["code"] for r in targets])
            current = next(r for r in targets if r["code"] == code)
            if st.button("追跡を再開" if current["paused"] else "追跡を停止"):
                from jp_screening.analysis.tracking import set_paused
                set_paused(writer, code, not current["paused"])
                st.rerun()
            with reader.transaction(readonly=True) as conn:
                quotes = list(conn.execute(select(s.prices).where(s.prices.c.code == code).order_by(s.prices.c.day.desc())).mappings())
            factor, chart = dec(1), []
            for quote in quotes:
                chart.append(dict(day=quote["day"], close=float(quote["close"]*factor) if quote["close"] is not None and factor is not None else None))
                factor = factor*quote["factor"] if factor is not None and quote["factor"] is not None else None
            if chart:
                st.caption("最新の取得日を基準に調整した終値")
                st.line_chart(pd.DataFrame(chart).set_index("day").sort_index())
    elif page == "条件比較":
        feature = st.selectbox("比較する条件", FEATURE_NAMES)
        start = st.date_input("開始日", date.today().replace(month=1, day=1))
        end = st.date_input("終了日", date.today())
        if st.button("比較結果を作成・保存", type="primary"):
            result = condition_report(writer, feature, horizon, basis, start, end, rule,
                                      output_root=config.root / "outputs" / "analysis_reports")
            st.session_state["condition_report"] = result
        result = st.session_state.get("condition_report")
        if result:
            st.caption(result["content"]["note"])
            show_table(result["content"]["quartiles"]["groups"])
            show_table(result["content"]["cohorts"])
            st.download_button("計算条件と結果を保存（JSON）", json.dumps(result, ensure_ascii=False, indent=2),
                               file_name="condition-report.json", mime="application/json")
    elif page == "資料管理":
        uploaded = st.file_uploader("Markdown・テキスト・Word", type=["md","txt","docx"])
        code = st.text_input("関連する銘柄コード（任意）")
        document_rule = st.selectbox("関連する判定ルール（任意）", [None, *rules],
                                    format_func=lambda x:x[:12] if x else "共通資料")
        dated = st.checkbox("資料日付を指定する")
        document_date = st.date_input("資料日付") if dated else None
        if uploaded and st.button("資料を登録"):
            safe_name = Path(uploaded.name).name
            folder = config.root / "data" / "knowledge"
            folder.mkdir(parents=True, exist_ok=True)
            # Stable source path allows newer revisions to supersede the same document.
            path = folder / safe_name
            path.write_bytes(uploaded.getvalue())
            result = index_document(writer, Ollama(config), path, code or None, document_date, document_rule)
            st.success(f"登録結果: {result['status']}")
        if st.button("このプロジェクトのREADMEを登録"):
            st.write(index_document(writer, Ollama(config), config.root / "README.md"))
        with reader.transaction(readonly=True) as conn:
            documents = list(conn.execute(select(s.documents.c.id, s.documents.c.title, s.documents.c.active,
                s.documents.c.profile, s.documents.c.created_at).order_by(s.documents.c.created_at.desc())).mappings())
        show_table([dict(x) for x in documents])
        if documents:
            selected = st.selectbox("検索対象を変更する資料", [x["id"] for x in documents],
                format_func=lambda x:next(d["title"] + " / " + x[:8] for d in documents if d["id"] == x))
            active = next(d["active"] for d in documents if d["id"] == selected)
            if st.button("検索対象から除外" if active else "検索対象に戻す"):
                set_document_active(writer, selected, not active)
                st.rerun()
            with reader.transaction(readonly=True) as conn:
                doc = conn.execute(select(s.documents).where(s.documents.c.id == selected)).mappings().one()
            st.download_button("保存した原文を確認（テキスト）", doc["original"], file_name=doc["title"]+".txt")
    elif page == "チャット":
        st.caption("例：最新の第3段階通過銘柄は？／銘柄コード1000の20営業日後の騰落率は？")
        question = st.chat_input("データや登録資料について質問してください")
        if question:
            with st.chat_message("user"):
                st.write(question)
            with st.spinner("記録と資料を確認しています"):
                result = answer(reader, Ollama(config), question, rules_key=rule)
            with st.chat_message("assistant"):
                st.write(result.get("clarification") or result.get("answer"))
                for warning in result.get("warnings", []):
                    st.info(warning)
                data = result.get("data", {})
                if "rows" in data:
                    show_table(data["rows"])
                elif data:
                    st.json(data)
                with st.expander("参照した記録・資料"):
                    st.json(dict(intent=result.get("intent"), runs=data.get("runs"), sources=result.get("sources")))
                    for source in result.get("sources", []):
                        with reader.transaction(readonly=True) as conn:
                            original = conn.execute(select(s.documents.c.original).where(s.documents.c.id == source["id"])).scalar_one()
                        st.download_button(f"[{source['citation']}] {source['title']}の原文", original,
                            file_name=source["title"]+".txt", key="source-"+source["id"]+source["citation"])
    elif page == "更新状況":
        source = st.text_input("結果フォルダー", str(config.root / "outputs"))
        if st.button("過去の結果を取り込む"):
            from jp_screening.analysis.ingest import import_runs
            show_table(import_runs(writer, source))
        left, right = st.columns(2)
        if left.button("株価・評価を更新"):
            from jp_screening.analysis.tracking import update_prices, evaluate_outcomes
            with st.spinner("追跡株価を更新しています"):
                st.json(update_prices(writer, config.api_key))
                st.write("更新した評価指標:", evaluate_outcomes(writer))
        if right.button("毎日の処理を再試行"):
            from jp_screening.analysis.pipeline import daily_update
            with st.spinner("スクリーニングと追跡処理を実行しています"):
                st.json(daily_update(config, writer, if_due=True))
        journal = config.root / "data" / "analysis_job.json"
        if journal.exists():
            st.json(json.loads(journal.read_text()))
        else:
            st.info("定期処理の記録はまだありません。")
        with reader.transaction(readonly=True) as conn:
            imports = list(conn.execute(select(s.runs.c.id, s.runs.c.asof, s.runs.c.scope,
                s.runs.c.eligible, s.runs.c.source).order_by(s.runs.c.asof.desc()).limit(500)).mappings())
        st.caption("取り込んだ実行。eligible=falseの記録は通常集計から除外しています。")
        show_table([dict(x) for x in imports])


try:
    main()
except ScreeningError as exc:
    st.error(str(exc))
except Exception as exc:
    st.error(f"処理を完了できませんでした ({type(exc).__name__})。接続設定・入力を確認してください。")
