"""Auditable UTF-8 BOM CSV exports and deterministic result fingerprints."""
import csv
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import unicodedata

from .storage import json_text

BASE_FIELDS = ["code", "name", "product", "listing_date", "asof", "status", "reason",
    "condition_a", "condition_b", "condition_c", "comparison_start", "comparison_end",
    "required_months", "available_months", "lowest_close", "lowest_month", "current_close",
    "upper_limit", "rise_pct", "m1_month", "m1_low", "m1_close", "m2_month", "m2_low", "m2_close",
    "m3_month", "m3_low", "m3_close", "m4_month", "m4_low", "m4_close", "sma3"]
HEADERS = ["銘柄コード", "銘柄名", "商品区分", "東証上場日", "判定日", "判定", "理由",
    "条件A_許容上限", "条件B_安値維持", "条件C_3か月平均", "比較開始月", "比較終了月",
    "必要月数", "取得月数", "最安終値", "最安終値の月", "直近終値", "許容上限価格", "上昇率_pct",
    "M1月", "M1安値", "M1終値", "M2月", "M2安値", "M2終値", "M3月", "M3安値", "M3終値",
    "M4月", "M4安値", "M4終値", "3か月平均"]

WEEKLY_FIELDS = ["stage1_status", "stage1_reason", "stage2_status", "stage2_reason",
    "weekly_condition_1", "weekly_condition_2", "weekly_condition_3", "w1_start", "w1_end",
    "w1_close_day", "w1_close", "sma5_week", "previous_sma5_week", "previous_three_week_high",
    "w2_high", "w3_high", "w4_high"]
WEEKLY_HEADERS = ["第1段階判定", "第1段階理由", "第2段階判定", "第2段階理由",
    "週足条件1_終値が5週平均超", "週足条件2_5週平均上向き", "週足条件3_直前3週高値超",
    "最新確定週_開始", "最新確定週_最終営業日", "週足終値採用日", "確定週足終値",
    "5週移動平均", "前週5週移動平均", "直前3週最高値", "1週前高値", "2週前高値", "3週前高値"]

DAILY_FIELDS = ["stage3_status", "stage3_reason", "daily_condition_1", "daily_condition_2",
    "d1_date", "d1_close", "sma5_day", "previous_sma5_day"]
DAILY_HEADERS = ["第3段階判定", "第3段階理由", "日足条件1_終値が5日平均未満", "日足条件2_5日平均下向き",
    "日足判定日", "確定日足終値", "5日移動平均", "前営業日5日移動平均"]


def fingerprint(results):
    return sha256(json_text(sorted(results, key=lambda r: r["code"])).encode("utf-8")).hexdigest()


def label(value):
    if value is True:
        return "通過"
    if value is False:
        return "不通過"
    return "" if value is None else str(value)


def safe_cell(value):
    """Neutralize formula-like text without turning numeric values into text."""
    if not isinstance(value, str):
        return value
    probe = value
    while probe and (probe[0].isspace() or unicodedata.category(probe[0]) in {"Cc", "Cf"}):
        probe = probe[1:]
    # Normalize for detection only, preserving the original company name.
    probe = unicodedata.normalize("NFKC", probe)
    if value.startswith(("\t", "\r", "\n")) or probe.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


class SafeCsvWriter:
    """One CSV output boundary shared by results, audits and validation codes."""

    def __init__(self, stream):
        self._writer = csv.writer(stream)

    def writerow(self, row):
        return self._writer.writerow([safe_cell(value) for value in row])

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)


def write_results(folder: Path, results, monthly, weekly=None, daily=None):
    results = sorted(results, key=lambda r: r["code"])
    fields = BASE_FIELDS + (WEEKLY_FIELDS if weekly is not None else [])
    headers = HEADERS + (WEEKLY_HEADERS if weekly is not None else [])
    if weekly is not None and any(r.get("weekly_breakout_required") is False for r in results):
        removed = {"weekly_condition_3", "previous_three_week_high", "w2_high", "w3_high", "w4_high"}
        selected_columns = [(field, header) for field, header in zip(fields, headers) if field not in removed]
        fields, headers = map(list, zip(*selected_columns))
    if daily is not None:
        fields, headers = fields + DAILY_FIELDS, headers + DAILY_HEADERS
    exports = [("all_results.csv", results),
               ("candidates.csv", [r for r in results if r["status"] == "通過"])]
    if weekly is not None:
        stage1_passed = [r for r in results if r.get("stage1_status") == "通過"]
        exports.extend([("stage1_candidates.csv",stage1_passed),("weekly_results.csv",stage1_passed)])
    if daily is not None:
        stage2_passed = [r for r in results if r.get("stage2_status") == "通過"]
        exports.extend([("stage2_candidates.csv",stage2_passed),("daily_results.csv",stage2_passed)])
    for filename, selected in exports:
        with (folder / filename).open("w", encoding="utf-8-sig", newline="") as f:
            writer = SafeCsvWriter(f)
            writer.writerow(headers)
            writer.writerows([[label(r.get(k)) if isinstance(r.get(k), bool) else r.get(k)
                               for k in fields] for r in selected])
    with (folder / "monthly_audit.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = SafeCsvWriter(f)
        writer.writerow(["銘柄コード", "月", "月足終値", "月足安値", "期間末営業日", "取得漏れ日数", "安値集計可能",
                         "終値採用日", "月末との差_営業日", "終値の採用方法"])
        for code, months in sorted(monthly.items()):
            for b in months.values():
                method = {"month_end":"月末営業日", "last_trade_in_month":"月内最終取引日"}.get(b.close_source, "欠損")
                writer.writerow([code, b.month, b.close, b.low, b.end_day, b.missing, b.complete_low,
                                 b.close_day or "", b.close_lag_sessions if b.close_lag_sessions is not None else "", method])
    if weekly is not None:
        with (folder / "weekly_audit.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = SafeCsvWriter(f)
            writer.writerow(["銘柄コード", "週番号", "週開始日", "週最終営業日", "終値採用日",
                             "週足終値", "週足高値", "週足安値", "取得漏れ日数", "無約定日数"])
            for code, weeks in sorted(weekly.items()):
                for index, week in enumerate(weeks,1):
                    writer.writerow([code,f"W{index}",week.start_day,week.end_day,week.close_day or "",
                        week.close,week.high,week.low,week.missing,week.no_trade_days])
    if daily is not None:
        with (folder / "daily_audit.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = SafeCsvWriter(f)
            writer.writerow(["銘柄コード", "営業日番号", "日付", "調整済み終値", "レコード有無"])
            for code, days in sorted(daily.items()):
                for index, day in enumerate(days, 1):
                    writer.writerow([code, f"D{index}", day.day, day.close, day.record_present])


def counts(results):
    summary = dict(total=len(results), **{status: sum(r["status"] == status for r in results)
                for status in ("通過", "不通過", "判定不能", "対象外")},
                condition_a_pass=sum(r.get("condition_a") is True for r in results),
                condition_b_pass=sum(r.get("condition_b") is True for r in results),
                condition_c_pass=sum(r.get("condition_c") is True for r in results))
    if any("stage2_status" in r for r in results):
        summary.update(stage1_pass=sum(r.get("stage1_status")=="通過" for r in results),
            stage2_total=sum(r.get("stage1_status")=="通過" for r in results),
            stage2_pass=sum(r.get("stage2_status")=="通過" for r in results),
            stage2_fail=sum(r.get("stage2_status")=="不通過" for r in results),
            stage2_unknown=sum(r.get("stage2_status")=="判定不能" for r in results))
        active_conditions = (1,2) if any(r.get("weekly_breakout_required") is False for r in results) else (1,2,3)
        for i in active_conditions:
            summary[f"weekly_condition_{i}_pass"] = sum(r.get(f"weekly_condition_{i}") is True for r in results)
    if any("stage3_status" in r for r in results):
        summary.update(stage3_total=sum(r.get("stage2_status")=="通過" for r in results),
            stage3_pass=sum(r.get("stage3_status")=="通過" for r in results),
            stage3_fail=sum(r.get("stage3_status")=="不通過" for r in results),
            stage3_unknown=sum(r.get("stage3_status")=="判定不能" for r in results))
        for i in (1,2):
            summary[f"daily_condition_{i}_pass"] = sum(r.get(f"daily_condition_{i}") is True for r in results)
    return summary


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
