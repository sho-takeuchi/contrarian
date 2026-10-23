"""Stage two: completed cash-market weeks and two strict moving-average conditions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .core import Bar, ScreeningError, TradingCalendar

WEEKLY_SETTINGS = {"sma_weeks":5, "require_breakout":False, "week":"MON-FRI",
                   "close_policy":"last_trade_in_week"}


@dataclass(frozen=True)
class WeekBar:
    start_day: date
    end_day: date
    close: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close_day: date | None
    missing: int
    no_trade_days: int


def weekly_bars(bars: list[Bar], cal: TradingCalendar, asof: date, n: int = 6) -> list[WeekBar]:
    """Newest first. A no-quote stock week remains a missing week, never skipped."""
    if n < 1:
        raise ScreeningError("週足の必要本数は1以上です。")
    by_day = {b.day:b for b in bars if b.day <= asof}
    monday = asof - timedelta(days=asof.weekday())
    weeks = []
    while len(weeks) < n:
        friday = monday + timedelta(days=4)
        if monday < cal.first_day:
            raise ScreeningError("確定週足に必要な取引カレンダーが不足しています。")
        sessions = cal.sessions(monday, friday)
        # Skip an exchange-wide closed week, not a stock-specific no-trade week.
        if sessions and sessions[-1] <= asof:
            missing = sum(day not in by_day for day in sessions)
            no_trade = sum(day in by_day and all(getattr(by_day[day], name) is None
                          for name in ("open","high","low","close")) for day in sessions)
            quote = None
            if missing == 0:
                for day in reversed(sessions):
                    bar = by_day[day]
                    if bar.close is not None:
                        quote = bar
                        break
                    if any(value is not None for value in (bar.open, bar.high, bar.low)):
                        break
            highs = [by_day[d].high for d in sessions if d in by_day and by_day[d].high is not None]
            lows = [by_day[d].low for d in sessions if d in by_day and by_day[d].low is not None]
            incomplete_high = any(d in by_day and by_day[d].high is None and
                any(getattr(by_day[d], f) is not None for f in ("open","low","close")) for d in sessions)
            incomplete_low = any(d in by_day and by_day[d].low is None and
                any(getattr(by_day[d], f) is not None for f in ("open","high","close")) for d in sessions)
            weeks.append(WeekBar(monday, sessions[-1], quote.close if quote else None,
                max(highs) if highs and not missing and not incomplete_high else None,
                min(lows) if lows and not missing and not incomplete_low else None,
                quote.day if quote else None, missing, no_trade))
        monday -= timedelta(days=7)
    return weeks


def evaluate_weekly(stage1: dict, bars: list[Bar], cal: TradingCalendar,
                    asof: date, *, require_breakout: bool = False) -> tuple[dict, list[WeekBar]]:
    result = {**stage1, "stage1_status":stage1["status"], "stage1_reason":stage1["reason"],
              "stage2_status":"未対象", "stage2_reason":"第1段階を通過していないため",
              "weekly_condition_1":None, "weekly_condition_2":None, "weekly_condition_3":None}
    # Legacy results must retain their exact field set for saved fingerprints.
    if not require_breakout:
        result["weekly_breakout_required"] = False
    if stage1["status"] != "通過":
        return result, []
    result.update(stage2_status="判定不能", stage2_reason="")
    try:
        weeks = weekly_bars(bars, cal, asof)
    except ScreeningError as exc:
        result.update(status="判定不能", reason=str(exc), stage2_reason=str(exc))
        return result, []
    reasons = []
    for i, week in enumerate(weeks, 1):
        result[f"w{i}_start"] = str(week.start_day)
        result[f"w{i}_end"] = str(week.end_day)
        result[f"w{i}_close"] = week.close
        result[f"w{i}_high"] = week.high
        result[f"w{i}_close_day"] = str(week.close_day) if week.close_day else ""
    five = [w.close for w in weeks[:5]]
    previous_five = [w.close for w in weeks[1:6]]
    recent_highs = [w.high for w in weeks[1:4]]
    sum5 = sum(five) if all(v is not None for v in five) else None
    previous_sum5 = sum(previous_five) if all(v is not None for v in previous_five) else None
    result["sma5_week"] = sum5 / 5 if sum5 is not None else None
    result["previous_sma5_week"] = previous_sum5 / 5 if previous_sum5 is not None else None
    result["previous_three_week_high"] = max(recent_highs) if require_breakout and all(v is not None for v in recent_highs) else None
    if sum5 is not None:
        result["weekly_condition_1"] = weeks[0].close * 5 > sum5
    else:
        reasons.append("5週移動平均用の終値不足")
    if sum5 is not None and previous_sum5 is not None:
        result["weekly_condition_2"] = sum5 > previous_sum5
    else:
        reasons.append("前週との5週移動平均比較用の終値不足")
    if require_breakout:
        if weeks[0].close is not None and result["previous_three_week_high"] is not None:
            result["weekly_condition_3"] = weeks[0].close > result["previous_three_week_high"]
        else:
            reasons.append("直近週終値または直前3週高値不足")
    flags = [result[f"weekly_condition_{i}"] for i in ((1,2,3) if require_breakout else (1,2))]
    if all(v is not None for v in flags):
        result["stage2_status"] = "通過" if all(flags) else "不通過"
    reasons.extend(text for flag,text in zip(flags,
        ("週足終値が5週平均以下", "5週平均が前週以下", "週足終値が直前3週高値以下")) if flag is False)
    result["stage2_reason"] = ";".join(dict.fromkeys(reasons))
    result["status"], result["reason"] = result["stage2_status"], result["stage2_reason"]
    return result, weeks


def reference_check_weekly(result: dict, weeks: list[WeekBar], *, require_breakout: bool = False) -> list[str]:
    """Equivalent comparisons without sharing the production SMA comparisons."""
    if result.get("stage1_status") != "通過" or not weeks:
        return []
    errors = []
    if all(w.close is not None for w in weeks[:5]):
        expected = 4 * weeks[0].close > sum(w.close for w in weeks[1:5])
        if result.get("weekly_condition_1") != expected:
            errors.append("weekly_condition_1")
    if all(w.close is not None for w in weeks):
        if result.get("weekly_condition_2") != (weeks[0].close > weeks[5].close):
            errors.append("weekly_condition_2")
    if require_breakout and weeks[0].close is not None and all(w.high is not None for w in weeks[1:4]):
        if result.get("weekly_condition_3") != all(weeks[0].close > w.high for w in weeks[1:4]):
            errors.append("weekly_condition_3")
    return errors
