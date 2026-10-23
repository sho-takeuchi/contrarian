"""Pure, network-free screening rules. Values remain Decimal throughout."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import calendar
from functools import cached_property
import re
from typing import Iterable
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
D = Decimal
MONTHLY_CLOSE_POLICY = "last_trade_in_month"


class ScreeningError(Exception):
    """Expected user-facing failure without credentials in its message."""


def number(value) -> Decimal | None:
    if value is None or str(value).strip().lower() in {"", "null", "none", "nan"}:
        return None
    try:
        result = D(str(value).strip().replace(",", ""))
    except InvalidOperation as exc:
        raise ScreeningError("価格または調整係数の形式が不正です。") from exc
    if not result.is_finite():
        raise ScreeningError("価格または調整係数が有限数ではありません。")
    return result


def code5(value: str) -> str:
    value = str(value).strip().upper()
    if re.fullmatch(r"[0-9A-Z]{4}", value):
        return value + "0"
    if re.fullmatch(r"[0-9A-Z]{5}", value):
        return value
    raise ScreeningError(f"銘柄コードの形式が不正です: {value[:12]}")


def shift_month(month: date, delta: int) -> date:
    n = month.year * 12 + month.month - 1 + delta
    return date(n // 12, n % 12 + 1, 1)


def years_before(day: date, n: int) -> date:
    return date(day.year - n, day.month, min(day.day, calendar.monthrange(day.year-n, day.month)[1]))


def month_key(day: date) -> str:
    return day.strftime("%Y-%m")


@dataclass(frozen=True)
class TradingCalendar:
    days: dict[date, str]

    @cached_property
    def first_day(self) -> date:
        if not self.days:
            raise ScreeningError("取引カレンダーが空です。")
        return min(self.days)

    @cached_property
    def _month_ends(self) -> dict[date, date]:
        return {}

    @classmethod
    def from_rows(cls, rows: Iterable[dict]):
        days = {}
        for row in rows:
            day, kind = date.fromisoformat(row["Date"]), str(row["HolDiv"])
            if kind not in {"0", "1", "2", "3"}:
                raise ScreeningError("未知の休日区分です。")
            if day in days and days[day] != kind:
                raise ScreeningError("取引カレンダーに重複・不一致があります。")
            days[day] = kind
        return cls(days)

    def sessions(self, start: date, end: date) -> list[date]:
        out = []
        day = start
        while day <= end:
            if day not in self.days:
                raise ScreeningError(f"取引カレンダー不足: {day}")
            if self.days[day] in {"1", "2"}:  # OSE-only holiday trading is not a cash session.
                out.append(day)
            day += timedelta(days=1)
        return out

    def month_end(self, month: date) -> date:
        month = month.replace(day=1)
        if month in self._month_ends:
            return self._month_ends[month]
        end = date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])
        # Standard may begin midway through the oldest month. Its terminal close
        # is still usable; only the available suffix must have calendar coverage.
        start = max(month.replace(day=1), self.first_day)
        sessions = self.sessions(start, end)
        if not sessions:
            raise ScreeningError(f"営業日のない月です: {month_key(month)}")
        self._month_ends[month] = sessions[-1]
        return sessions[-1]

    def target(self, now: datetime) -> date:
        now = now.astimezone(JST)
        today = now.date()
        sessions = self.sessions(today - timedelta(days=20), today)
        if not sessions:
            raise ScreeningError("直近営業日が判定できません。")
        if sessions[-1] == today:
            close = time(11, 30) if self.days[today] == "2" else time(15, 30)
            if now.time() < close:
                raise ScreeningError("取引時間終了前です。引け後に実行してください。")
        return sessions[-1]

    def last_complete_month(self, asof: date) -> date:
        month = asof.replace(day=1)
        return month if asof >= self.month_end(month) else shift_month(month, -1)


@dataclass(frozen=True)
class Bar:
    day: date
    code: str
    close: Decimal | None
    low: Decimal | None
    high: Decimal | None
    open: Decimal | None = None


def adjusted_bars(rows: Iterable[dict], asof: date) -> list[Bar]:
    """Rebase raw OHLC to asof, excluding the factor on the row being priced."""
    data = sorted((r for r in rows if date.fromisoformat(r["Date"]) <= asof),
                  key=lambda r: r["Date"], reverse=True)
    if len({code5(r["Code"]) for r in data}) > 1:
        raise ScreeningError("単一銘柄の価格計算に複数の銘柄コードが混在しています。")
    result, seen = [], set()
    with localcontext() as ctx:
        ctx.prec = 60
        factor = D(1)
        for row in data:
            day, code = date.fromisoformat(row["Date"]), code5(row["Code"])
            if (code, day) in seen:
                raise ScreeningError("日足データに重複があります。")
            seen.add((code, day))
            def adj(field):
                value = number(row.get(field))
                if value is None:
                    return None
                if value <= 0:
                    raise ScreeningError("価格が0以下です。")
                adjusted = (value * factor).quantize(D("0.1"), rounding=ROUND_HALF_UP)
                if adjusted <= 0:
                    raise ScreeningError("API精度で再現した調整済み価格が0以下です。")
                return adjusted
            result.append(Bar(day, code, adj("C"), adj("L"), adj("H"), adj("O")))
            event = number(row.get("AdjFactor"))
            if event is None or event <= 0:
                raise ScreeningError(f"調整係数不足・不正: {day}")
            factor *= event
    return sorted(result, key=lambda b: b.day)


def api_bars(rows: Iterable[dict], asof: date) -> list[Bar]:
    """Use provider-adjusted prices; raw rebase is compared separately."""
    out, seen = [], set()
    for row in sorted(rows, key=lambda r: r["Date"]):
        day, code = date.fromisoformat(row["Date"]), code5(row["Code"])
        if day > asof:
            continue
        if (code, day) in seen:
            raise ScreeningError("API日足データに重複があります。")
        seen.add((code, day))
        values = [number(row.get(k)) for k in ("AdjC", "AdjL", "AdjH", "AdjO")]
        if any(x is not None and x <= 0 for x in values):
            raise ScreeningError("API調整済み価格が0以下です。")
        out.append(Bar(day, code, *values))
    return out


@dataclass(frozen=True)
class MonthBar:
    month: str
    close: Decimal | None
    low: Decimal | None
    end_day: date
    missing: int
    complete_low: bool
    close_day: date | None = None
    close_lag_sessions: int | None = None
    close_source: str | None = None


def monthly_bars(bars: list[Bar], cal: TradingCalendar, asof: date,
                 listing: date | None, start: date,
                 close_policy: str = MONTHLY_CLOSE_POLICY) -> dict[str, MonthBar]:
    if close_policy not in {"month_end", "last_trade_in_month"}:
        raise ScreeningError("未知の月足終値集計ルールです。")
    by_day = {b.day: b for b in bars}
    last = cal.last_complete_month(asof)
    out = {}
    month = start.replace(day=1)
    while month <= last:
        end_day = cal.month_end(month)
        true_start = max(month, listing or month)
        expected_start = max(true_start, cal.first_day)
        expected = cal.sessions(expected_start, end_day) if expected_start <= end_day else []
        missing = sum(day not in by_day for day in expected)
        terminal = by_day.get(end_day)
        selected_close = terminal if terminal and terminal.close is not None else None
        if selected_close is None and close_policy == "last_trade_in_month":
            # A record with all OHLC absent denotes an explicit no-trade day.
            # Never cross a missing record, partial OHLC anomaly or month boundary.
            for day in reversed(expected):
                bar = by_day.get(day)
                if bar is None:
                    break
                if bar.close is not None:
                    selected_close = bar
                    break
                if any(value is not None for value in (bar.open, bar.high, bar.low)):
                    break
        lows = [by_day[day].low for day in expected if day in by_day and by_day[day].low is not None]
        # Historical oldest partial month can provide its close but not its low.
        complete_low = bool(expected) and missing == 0 and expected_start == true_start
        close_day = selected_close.day if selected_close else None
        lag = sum(day > close_day for day in expected) if close_day else None
        source = ("month_end" if close_day == end_day else "last_trade_in_month") if close_day else None
        out[month_key(month)] = MonthBar(month_key(month), selected_close.close if selected_close else None,
            min(lows) if lows and complete_low else None, end_day, missing, complete_low,
            close_day, lag, source)
        month = shift_month(month, 1)
    return out


def upper_limit(lowest: Decimal) -> Decimal:
    return lowest + min(max(lowest * D("0.2"), D(100)), lowest * D("0.5"))


def evaluate(meta: dict, listing: date | None, bars: list[Bar], cal: TradingCalendar,
             asof: date, start: date,
             close_policy: str = MONTHLY_CLOSE_POLICY) -> tuple[dict, dict[str, MonthBar]]:
    result = dict(code=meta["Code"], name=meta.get("CoName", ""),
                  product="REIT" if meta.get("ProdCat") == "013" else "株式",
                  listing_date=listing.isoformat() if listing else "", asof=asof.isoformat(),
                  status="判定不能", reason="", condition_a=None, condition_b=None, condition_c=None)
    months = monthly_bars(bars, cal, asof, listing, start, close_policy=close_policy)
    reasons = []
    if listing is None:
        reasons.append("上場日不明")
    elif listing > years_before(asof, 8):
        result.update(status="対象外", reason="上場8年未満")
        return result, months
    comparison_start = shift_month(asof.replace(day=1), -120)
    if listing and listing > years_before(asof, 10):
        comparison_start = listing.replace(day=1)
    comparison_end = shift_month(asof.replace(day=1), -1)
    keys, m = [], comparison_start
    while m <= comparison_end:
        keys.append(month_key(m))
        m = shift_month(m, 1)
    available = [months.get(k) for k in keys]
    result.update(comparison_start=month_key(comparison_start), comparison_end=month_key(comparison_end),
                  required_months=len(keys), available_months=sum(b is not None and b.close is not None for b in available))
    current = next((b.close for b in bars if b.day == asof), None)
    result["current_close"] = current
    if current is None:
        reasons.append("直近営業日の終値欠損")
    if listing and all(b is not None and b.close is not None for b in available) and available:
        lowest = min(b.close for b in available)
        result.update(lowest_close=lowest, lowest_month=";".join(b.month for b in available if b.close == lowest),
                      upper_limit=upper_limit(lowest))
        if current is not None:
            result.update(condition_a=current <= upper_limit(lowest), rise_pct=(current / lowest - 1) * 100)
    else:
        reasons.append("比較期間の月足終値不足" if listing else "比較期間を確定できない")
    last = cal.last_complete_month(asof)
    recent = [months.get(month_key(shift_month(last, -i))) for i in range(4)]
    for i, b in enumerate(recent, 1):
        result[f"m{i}_month"] = b.month if b else month_key(shift_month(last, 1-i))
        result[f"m{i}_low"] = b.low if b else None
        result[f"m{i}_close"] = b.close if b else None
    if all(b and b.low is not None for b in recent):
        result["condition_b"] = min(b.low for b in recent[:3]) >= recent[3].low
    else:
        reasons.append("直近4か月の安値データ不足")
    if all(b and b.close is not None for b in recent[:3]):
        total = sum(b.close for b in recent[:3])
        result["sma3"] = total / 3
        # Cross-multiply to avoid a rounded repeating decimal influencing equality.
        result["condition_c"] = recent[0].close * 3 > total
    else:
        reasons.append("3か月平均用の終値不足")
    flags = [result[k] for k in ("condition_a", "condition_b", "condition_c")]
    if listing is not None and current is not None and all(v is not None for v in flags):
        result["status"] = "通過" if all(flags) else "不通過"
        reasons += [label for label, flag in zip(("許容上限超過", "月足安値条件不成立", "3か月平均以下"), flags) if flag is False]
    result["reason"] = ";".join(dict.fromkeys(reasons))
    return result, months
