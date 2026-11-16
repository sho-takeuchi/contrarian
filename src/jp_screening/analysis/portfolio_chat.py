"""Read-only answers about recorded recommendations and actual trades."""
from datetime import date, datetime, time
import re

from ..core import JST, ScreeningError, code5
from .portfolio import overview


def answer_portfolio(db, question, previous=None):
    from .monitor_chat import escape, normalized, number
    text = normalized(question)
    previous = previous or {}
    keywords = r"保有|買付|購入|買った|買いました|売却|売買|推薦|おすすめ|お勧め|損益|含み|取得単価"
    followup = bool(re.search(r"その|それ|この|根拠|理由|詳し|もっと|では", text))
    if not re.search(keywords, text) and not (previous.get("kind") == "portfolio" and followup):
        return None
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    try:
        days = [date.fromisoformat(day) for day in dates]
    except ValueError:
        raise ScreeningError("日付はYYYY-MM-DDの実在する日付で指定してください。") from None
    if len(days) > 2 or len(days) == 2 and days[0] > days[1]:
        raise ScreeningError("期間は開始日・終了日の順で、2つまで指定してください。")
    now = datetime.now(JST)
    if days:
        now = min(now, datetime.combine(days[-1], time(23, 59), JST))
    data = overview(db, now)
    names = {row["code"]:row["name"] for row in data["trades"]}
    without_dates = re.sub(r"\d{4}-\d{2}-\d{2}", "", text)
    tokens = re.findall(r"(?<![0-9a-z])[0-9][0-9a-z]{3}[0-9]?(?![0-9a-z])", without_dates)
    codes = {code5(token.upper()) for token in tokens}
    for code, name in names.items():
        short = re.sub(r"株式会社|ホールディングス|holdings|hd", "", normalized(name))
        if normalized(name) in text or len(short) >= 2 and short in text:
            codes.add(code)
    if not codes and previous.get("kind") == "portfolio" and followup and not re.search("全体|全銘柄|一覧|すべて", text):
        codes = set(previous.get("codes", []))
    chosen = [row for row in data["positions"] if not codes or row["code"] in codes]
    subject = re.match(r"(.+?)(?:の|は|について)", text)
    if not codes and subject and not re.search(r"銘柄|保有|買付|購入|売却|売買|推薦|おすすめ|お勧め|損益|全体|その|それ|この|私", subject[1]):
        chosen = []
    result = dict(context=dict(kind="portfolio", codes=sorted(codes)), record_ids=[], source_ids=[])
    if not chosen:
        return {**result, "answer":"該当する売買履歴はまだ登録されていません。"}
    lines = ["保存済みの推薦・売買履歴と、確定終値による評価です。",
             "取得原価は移動平均で計算し、売買履歴にある手数料・税金等・諸費用を含めます。配当・譲渡益課税は含みません。"]
    for position in chosen:
        code = position["code"]
        lines += [f"**{escape(code)} {escape(position['name'])}／{escape(position['account'])}**",
            "保有数量：" + number(position["quantity"], "株"),
            "平均取得単価：" + number(position["average_cost"]),
            f"評価株価：{number(position['price'])}（{position['price_day'] or '終値待ち'}）",
            "評価損益：" + number(position["unrealized"]) + " ／ " + number(position["return_pct"], "％"),
            "確定損益：" + number(position["realized"]),
            "評価状態：" + escape(position["status"]),
            f"最新判定：{position['screening_date'] or '未観測'}／月足 {escape(position['stage1'])}／週足 {escape(position['stage2'])}"]
        for recommendation in position["recommendations"]:
            lines += ["保存した推薦日：" + str(recommendation["recommended_on"] or "未確認"),
                "推薦理由：" + escape(recommendation["reason"]),
                "推薦時の注意点：" + escape(recommendation["risks"]),
                "出典：" + escape(recommendation["source"])]
            result["record_ids"].append(recommendation["id"])
        if not position["recommendations"]:
            lines.append("推薦理由は未登録です。")
        records = [row for row in data["trades"] if row["code"] == code and row["account"] == position["account"]
                   and row["trade_date"] <= now.date() and (len(days) != 2 or row["trade_date"] >= days[0])]
        for row in records[-20:]:
            lines.append(f"- {row['trade_date']} {escape(row['side'])} {number(row['quantity'], '株')} × {number(row['unit_price'])}")
        if len(records) > 20:
            lines.append(f"直近20件を表示しています。該当する売買は{len(records)}件です。")
    return {**result, "answer":"\n\n".join(lines)}
