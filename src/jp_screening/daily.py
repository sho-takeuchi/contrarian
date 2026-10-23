"""Stage three: short-term daily pullbacks after monthly and weekly passes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .core import Bar, ScreeningError, TradingCalendar

DAILY_SETTINGS = {"sma_days":5, "close_policy":"same_business_day", "missing_policy":"no_fill"}


@dataclass(frozen=True)
class DayClose:
    day: date
    close: Decimal | None
    record_present: bool


def daily_closes(bars: list[Bar], cal: TradingCalendar, asof: date) -> list[DayClose]:
    """Latest six cash sessions, newest first; missing quotes retain their slots."""
    if not cal.sessions(asof, asof):
        raise ScreeningError("日足の判定日は現物市場の営業日を指定してください。")
    by_day = {bar.day:bar for bar in bars if bar.day <= asof}
    days = []
    day = asof
    while len(days) < 6:
        if day < cal.first_day:
            raise ScreeningError("日足の5日平均比較に必要な取引カレンダーが不足しています。")
        if cal.sessions(day, day):
            bar = by_day.get(day)
            days.append(DayClose(day, bar.close if bar else None, bar is not None))
        day -= timedelta(days=1)
    return days


def evaluate_daily(stage2: dict, bars: list[Bar], cal: TradingCalendar,
                   asof: date) -> tuple[dict, list[DayClose]]:
    result = {**stage2, "stage3_status":"未対象", "stage3_reason":"第2段階を通過していないため",
              "daily_condition_1":None, "daily_condition_2":None,
              "sma5_day":None, "previous_sma5_day":None}
    if stage2.get("stage2_status") != "通過":
        return result, []
    result.update(stage3_status="判定不能", stage3_reason="")
    try:
        days = daily_closes(bars, cal, asof)
    except ScreeningError as exc:
        result.update(status="判定不能", reason=str(exc), stage3_reason=str(exc))
        return result, []
    for index, day in enumerate(days, 1):
        result[f"d{index}_date"] = str(day.day)
        result[f"d{index}_close"] = day.close
    closes = [day.close for day in days]
    sum5 = sum(closes[:5]) if all(v is not None for v in closes[:5]) else None
    previous_sum5 = sum(closes[1:]) if all(v is not None for v in closes[1:]) else None
    result["sma5_day"] = sum5 / 5 if sum5 is not None else None
    result["previous_sma5_day"] = previous_sum5 / 5 if previous_sum5 is not None else None
    reasons = []
    if sum5 is not None:
        result["daily_condition_1"] = closes[0] * 5 < sum5
    else:
        reasons.append("直近5営業日の終値不足")
    if sum5 is not None and previous_sum5 is not None:
        result["daily_condition_2"] = sum5 < previous_sum5
    else:
        reasons.append("前営業日との5日平均比較用の終値不足")
    for flag, text in zip((result["daily_condition_1"], result["daily_condition_2"]),
                         ("日足終値が5日平均以上", "5日平均が前営業日以上")):
        if flag is False:
            reasons.append(text)
    if all(result[f"daily_condition_{i}"] is not None for i in (1,2)):
        result["stage3_status"] = "通過" if all(result[f"daily_condition_{i}"] for i in (1,2)) else "不通過"
    result["stage3_reason"] = ";".join(dict.fromkeys(reasons))
    result["status"], result["reason"] = result["stage3_status"], result["stage3_reason"]
    return result, days


def reference_check_daily(result: dict, days: list[DayClose]) -> list[str]:
    """Equivalent arithmetic, independent of the production average comparison."""
    if result.get("stage2_status") != "通過" or not days:
        return []
    closes = [day.close for day in days]
    errors = []
    if all(v is not None for v in closes[:5]):
        if result.get("daily_condition_1") != (4 * closes[0] < sum(closes[1:5])):
            errors.append("daily_condition_1")
    if all(v is not None for v in closes):
        if result.get("daily_condition_2") != (closes[0] < closes[5]):
            errors.append("daily_condition_2")
    return errors
