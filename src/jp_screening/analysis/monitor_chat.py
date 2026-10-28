"""Answer questions from saved stage-two observations; never start a job or write data."""
from collections import Counter
from datetime import date
import re
import unicodedata
from urllib.parse import urlsplit

from sqlalchemy import select

from ..core import ScreeningError, code5
from . import schema as s
from .domain import dec
from .monitoring import bucket
from .research import evidence_for, validate_extraction

LIMIT = 10
HELP = ("銘柄コード・銘柄名、または上昇／下落の区分を入れて質問してください。\n\n"
        "例：『上昇して通過終了した銘柄は、なぜ終了した？』"
        "『下落して通過終了した理由と根拠を教えて』"
        "『2499の通過終了理由は？』『上昇と下落の共通傾向は？』")
DEFINITION = ("第1段階の月足と第2段階の週足を両方通過した期間を記録しています。"
    "月足・週足のいずれかが明確に不通過になれば終了です。\n\n"
    "分割・併合の調整基準をそろえた通過開始日の終値と、通過終了日の終値を比較し、"
    "正なら『上昇して通過終了』、負なら『下落して通過終了』、0％なら『変化なし』です。"
    "終了後の値動きでは分類を変えません。価格訂正は旧版を残して再評価します。"
    "配当・売買費用は含みません。\n\n"
    "未実行・判定不能・必要価格の欠損や観測の途切れは『要確認』とし、"
    "不通過や0％、上昇・下落の実績に置き換えません。")


def normalized(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


def escape(value):
    return re.sub(r"([\\`*_{}\[\]()<>#!|])", r"\\\1", str(value)).replace("\n", " ")


def number(value, suffix="円"):
    value = dec(value)
    if value is None:
        return "確認待ち"
    return (f"{value:+.2f}" if suffix == "％" else f"{value:,.2f}") + suffix


def select_records(question, periods, previous=None):
    """Resolve only names/codes present in the question, plus explicit follow-ups."""
    text = normalized(question)
    previous = previous or {}
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    for day in dates:
        try:
            date.fromisoformat(day)
        except ValueError:
            raise ScreeningError("日付はYYYY-MM-DDの実在する日付で指定してください。") from None
    if len(dates) > 2 or (len(dates) == 2 and dates[0] > dates[1]):
        raise ScreeningError("期間は開始日・終了日の順で、2つまで指定してください。")
    without_dates = re.sub(r"\d{4}-\d{2}-\d{2}", "", unicodedata.normalize("NFKC", question).casefold())
    tokens = re.findall(r"(?<![0-9a-z])[0-9][0-9a-z]{3}[0-9]?(?![0-9a-z])", without_dates)
    codes = {code5(token.upper()) for token in tokens}
    codes.update(p["code"] for p in periods if p["content"]["name"] and normalized(p["content"]["name"]) in text)
    up = bool(re.search("上昇|値上がり|上が|上げ|プラス", text))
    down = bool(re.search("下落|値下がり|下が|下げ|マイナス", text))
    direction = "up" if up and not down else "down" if down and not up else None
    if "通過中" in text or "継続中" in text:
        direction = "passing"
    if "要確認" in text or "変化なし" in text:
        direction = "uncertain"
    all_scope = bool(re.search("全体|全銘柄|すべて|一覧|両方|上昇と下落|上昇・下落", text)) or (up and down)
    followup = bool(re.search("その|それ|この|同じ|根拠|資料|詳し|もっと|条件|理由|なぜ|では|次の|残り", text))
    if not codes and previous and followup and not all_scope:
        codes = set(previous.get("codes", []))
        if direction is None:
            direction = previous.get("direction")
    start = dates[0] if dates else previous.get("start") if followup and not all_scope else None
    end = dates[-1] if dates else previous.get("end") if followup and not all_scope else None
    # Date filters refer to the observed exit date (start date for an ongoing period).
    scope = dict(codes=sorted(codes), direction=direction, start=start, end=end)
    selected = [p for p in periods if (not codes or p["code"] in codes)]
    labels = {"up":"上昇して通過終了", "down":"下落して通過終了", "passing":"通過中", "uncertain":"変化なし・要確認"}
    if direction:
        selected = [p for p in selected if bucket(p) == labels[direction]]
    elif not codes:
        selected = [p for p in selected if p["end_date"]]
    if start:
        selected = [p for p in selected if start <= str(p["end_date"] or p["start_date"]) <= end]
    selected.sort(key=lambda p:(str(p["end_date"] or p["start_date"]), p["code"], p["id"]), reverse=True)
    offset = previous.get("offset", 0) + LIMIT if re.search("次の|残り", text) and all(scope.get(k) == previous.get(k) for k in scope) else 0
    scope["offset"] = offset
    recognized = bool(codes or direction or all_scope or dates or previous and followup or
                      re.search("通過|終了|傾向|比較|違い|要因|理由|なぜ", text))
    if not codes and not direction and not all_scope:
        subject = re.match(r"(.+?)(?:の|は|について)", text)
        if subject and not re.search("銘柄|通過|終了|全体|傾向|共通|比較|違い|その|それ|この|なぜ", subject[1]):
            recognized = False  # An unknown company must not silently become an all-company answer.
    if not codes and not previous and re.search("この銘柄|その銘柄|この会社|その会社", text):
        recognized = False
    return selected, scope, recognized


def record_answer(period, analysis, sources):
    content = period["content"]
    lines = [f"**{escape(period['code'])} {escape(content['name'])}**（{bucket(period)}）",
             f"通過開始日：{period['start_date']}　／　最終判定日：{escape(content['last_date'])}"]
    if not period["end_date"]:
        lines += ["通過終了はまだ確認されていません。", f"最新の開始日比：{number(content['latest']['return_pct'], '％')}"]
    else:
        measurement = content["exit"]
        label = "終了を初めて確認した日" if content["continuity_unknown"] else "通過終了日"
        lines += [f"{label}：{period['end_date']}",
                  f"開始時の調整後終値：{number(measurement['baseline'])}　→　終了時：{number(measurement['end_close'])}",
                  f"終了時騰落率：{number(measurement['return_pct'], '％')}（{escape(measurement['status'])}）",
                  "", "**条件を外れた理由**", escape(content['failure']['reason'])]
        failed = content["failure"].get("failed_conditions", [])
        if failed:
            lines.append("満たさなくなった条件：" + "、".join(map(escape, failed)))
        labels = {"current_close":"判定時終値", "lowest_close":"長期最安終値", "upper_limit":"許容上限",
                  "m1_low":"直近確定月安値", "m2_low":"2か月前安値", "m3_low":"3か月前安値", "m4_low":"4か月前安値",
                  "m1_close":"直近確定月終値", "sma3":"3か月平均", "w1_close":"確定週終値",
                  "sma5_week":"5週平均", "previous_sma5_week":"前週の5週平均"}
        values = content["failure"].get("values", {})
        relevant = list(labels)[:9] if content["failure"]["stage"] == 1 else list(labels)[9:]
        lines.extend(f"- {labels[key]}：{number(values[key])}" for key in relevant if values.get(key) is not None)
    if content["continuity_unknown"]:
        lines += ["観測に途切れがあるため連続性は要確認です。上昇・下落の全体比較には含めません。"]
    lines += ["", "**値動きに関連する資料・自動分析**"]
    source_ids = []
    if not analysis or analysis["content"].get("period_digest") != period["digest"]:
        lines.append("現在の判定・株価に対応する自動分析は保存待ちです。過去の分析で理由を補いません。")
    else:
        lines.append(escape(analysis["content"]["conclusion"]))
        eligible = evidence_for(period, sources) if period["end_date"] else []
        for factor in analysis["content"].get("factors", []):
            try:
                verified = validate_extraction({"factors":[{key:factor[key] for key in ("source_id", "quote", "category")}]}, eligible)[0]
            except (ScreeningError, ValueError, KeyError):
                lines.append("以前の根拠資料は現在の取得状態と照合できないため、引用を保留しています。")
                continue
            source_ids.append(verified["source_id"])
            lines += [f"- 関連する材料：{escape(verified['category'])}／{escape(verified['title'])}",
                      "> " + escape(verified["quote"])]
            url = verified["url"]
            parsed = urlsplit(url)
            if parsed.scheme == "https" and parsed.hostname and not parsed.username and not re.search(r"[\s<>()]", url):
                lines.append(f"[根拠資料を開く](<{url}>)")
            lines.append("発表日時：" + escape(verified["published_at"]))
        for coverage in analysis["content"].get("coverage", []):
            labels = {"ready":"取得済み", "no_data":"該当情報なし", "retry":"再試行待ち", "waiting_key":"キー設定待ち", "partial":"一部取得"}
            lines.append(f"資料取得：{escape(coverage['provider'])}／{escape(labels.get(coverage['state'], coverage['state']))}")
        lines.append(f"分析保存日時：{escape(analysis['created_at'])}　／　分析ID：{escape(analysis['id'])}")
    if period["end_date"]:
        lines.append("条件不成立の理由と、株価変動の原因は別です。資料との関連を示しており、因果関係は確定していません。")
    lines.append(f"実行ID：{escape(content['start_run'])} → {escape(content.get('end_run') or '通過終了の観測なし')}")
    return "\n\n".join(lines), source_ids


def answer_question(db, question, previous=None):
    """Return an auditable answer and follow-up scope using READ ONLY transactions."""
    question = question.strip()
    if not question or len(question) > 1000:
        raise ScreeningError("質問は1〜1000文字で入力してください。")
    with db.transaction(readonly=True) as conn:
        periods = [dict(r) for r in conn.execute(select(s.monitor_periods).where(
            s.monitor_periods.c.active.is_(True))).mappings()]
        report = conn.execute(select(s.monitor_analyses).where(s.monitor_analyses.c.kind == "summary").order_by(
            s.monitor_analyses.c.created_at.desc()).limit(1)).mappings().first()
        chosen, scope, recognized = select_records(question, periods, previous)
        displayed = chosen[scope["offset"]:scope["offset"] + LIMIT]
        ids = [p["analysis_id"] for p in displayed if p["analysis_id"]]
        analyses = {r["id"]:dict(r) for r in conn.execute(select(s.monitor_analyses).where(
            s.monitor_analyses.c.id.in_(ids), s.monitor_analyses.c.kind == "individual")).mappings()}
        sources = list(conn.execute(select(s.monitor_sources).where(
            s.monitor_sources.c.code.in_({p["code"] for p in displayed}))).mappings())
    result = dict(context=scope, record_ids=[], source_ids=[])
    text = normalized(question)
    if re.search("意味|分類の基準|数え方|計算方法|計算式", text) and not scope["codes"]:
        return {**result, "answer":DEFINITION}
    if not recognized:
        return {**result, "answer":HELP}
    counts = Counter(bucket(p) for p in periods)
    status = "／".join(f"{label} {counts[label]}件" for label in ("通過中", "上昇して通過終了", "下落して通過終了", "変化なし・要確認"))
    if not chosen:
        return {**result, "answer":"質問に該当する通過期間の記録はまだありません。\n\n" + status +
                "\n\n通過中の記録を終了例として扱ったり、根拠を推測したりせず、今後の自動記録・分析を待ちます。"}
    lines = [f"対象は{len(chosen)}件・{len({p['code'] for p in chosen})}銘柄です。", status]
    if scope["start"]:
        lines.append(f"対象日：{scope['start']}〜{scope['end']}（終了確認日。通過中は開始日）")
    if report and re.search("傾向|比較|共通|違い", text):
        lines += ["**保存済みの全体比較**（判定ルールごと）", escape(report["content"]["conclusion"]),
                  escape(report["content"]["note"]), f"全体比較の保存日時：{escape(report['created_at'])}"]
    if not displayed:
        lines.append("続きの記録はありません。")
    for period in displayed:
        analysis = analyses.get(period["analysis_id"])
        if analysis and analysis["period_id"] != period["id"]:
            analysis = None
        message, evidence_ids = record_answer(period, analysis, sources)
        lines += ["---", message]
        result["record_ids"].append(period["id"])
        result["source_ids"].extend(evidence_ids)
    if len(chosen) > scope["offset"] + LIMIT:
        lines.append("新しい順に10件ずつ表示しています。続きは『残りを教えて』と質問してください。")
    return {**result, "answer":"\n\n".join(lines)}
