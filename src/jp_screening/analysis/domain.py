"""Deterministic research calculations; no network, database or LLM required."""
from datetime import date
from decimal import Decimal, localcontext
from hashlib import sha256
import json
from statistics import median

from ..core import ScreeningError

D = Decimal
HORIZONS = (5, 20, 60, 120, 250)
FEATURE_VERSION = "1"
CALCULATION_VERSION = "1"
FEATURE_NAMES = (
    "long_low_rise", "ceiling_room", "monthly_low_margin", "monthly_ma_gap",
    "weekly_ma_gap", "weekly_ma_change", "daily_ma_gap", "daily_ma_change",
)


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                             separators=(",", ":")).encode()).hexdigest()


def dec(value):
    if value is None or value == "":
        return None
    try:
        value = D(str(value))
    except Exception:
        raise ScreeningError("数値の形式が不正です。") from None
    if not value.is_finite():
        raise ScreeningError("有限の数値が必要です。")
    return value


def percent(value, base):
    value, base = dec(value), dec(base)
    if value is None or base is None or base <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 60
        return (value / base - 1) * 100


def features(row):
    lows = [dec(row.get(f"m{i}_low")) for i in (1, 2, 3)]
    return dict(zip(FEATURE_NAMES, (
        percent(row.get("current_close"), row.get("lowest_close")),
        percent(row.get("upper_limit"), row.get("current_close")),
        percent(min(lows), row.get("m4_low")) if all(x is not None for x in lows) else None,
        percent(row.get("m1_close"), row.get("sma3")),
        percent(row.get("w1_close"), row.get("sma5_week")),
        percent(row.get("sma5_week"), row.get("previous_sma5_week")),
        percent(row.get("d1_close"), row.get("sma5_day")),
        percent(row.get("sma5_day"), row.get("previous_sma5_day")),
    )))


def stage_status(row, stage):
    """An upstream failure is a failure of the cumulative screen, never an unknown."""
    flags = [row.get(f"s{i}", "未評価") for i in range(1, stage + 1)]
    if any(s in {"不通過", "対象外"} for s in flags):
        return "不通過"
    return "通過" if all(s == "通過" for s in flags) else "判定不能"


def representative(runs):
    chosen = {}
    for run in runs:
        if not run["eligible"]:
            continue
        key = (run["rules_key"], run["asof"])
        rank = (run["stage"], str(run["created_at"]), run["id"])
        if key not in chosen or rank > chosen[key][0]:
            chosen[key] = (rank, run)
    return [v[1] for _, v in sorted(chosen.items())]


def build_episodes(runs, rows, sessions):
    """Rebuild a derived view; immutable source observations remain in screening_results."""
    by_run = {}
    for row in rows:
        by_run.setdefault(row["run_id"], []).append(row)
    open_events, events = {}, []
    previous = {}
    session_positions = {day: i for i, day in enumerate(sorted(sessions))}
    for run in representative(runs):
        rule, day = run["rules_key"], run["asof"]
        last_day = previous.get(rule)
        gap = last_day is not None and (
            day not in session_positions or last_day not in session_positions or
            session_positions[day] - session_positions[last_day] != 1)
        previous[rule] = day
        current = {r["code"]: r for r in by_run.get(run["id"], [])}
        for (rkey, code, stage), event in open_events.items():
            if rkey == rule and (gap or code not in current):
                event["continuity_unknown"] = True
        for code, row in current.items():
            for stage in (1, 2, 3):
                key = (rule, code, stage)
                status = stage_status(row, stage)
                if status == "不通過":
                    open_events.pop(key, None)
                elif status != "通過":
                    if key in open_events:
                        open_events[key]["continuity_unknown"] = True
                elif key not in open_events:
                    event = dict(id=digest([rule, code, stage, str(day), run["id"]]),
                                 run_id=run["id"], code=code, stage=stage, start_date=day,
                                 rules_key=rule, continuity_unknown=False)
                    events.append(event)
                    open_events[key] = event
    return events


def outcome(prices, sessions, start, horizon, basis, cutoff):
    """All prices rebase to one endpoint; no price/observation is filled forward."""
    if horizon not in (0, *HORIZONS) or basis not in {"close", "next_open"}:
        raise ScreeningError("未対応の評価期間・基準価格です。")
    sessions = sorted(sessions)
    result = dict(status="評価待ち", target_date=None, baseline=None, end_close=None,
                  return_pct=None, max_up_pct=None, max_down_pct=None,
                  excursion_status="評価待ち", calculation_version=CALCULATION_VERSION)
    if start not in sessions:
        return {**result, "status":"カレンダー不足"}
    index = sessions.index(start)
    available = [d for d in sessions[index:] if d <= cutoff and prices.get(d, {}).get("C") is not None
                 and not prices.get(d, {}).get("_missing")]
    target = available[-1] if horizon == 0 and available else (
        sessions[index + horizon] if horizon and index + horizon < len(sessions) else None)
    if target is None or target > cutoff:
        return result
    result["target_date"] = str(target)
    days = [d for d in sessions[index:] if d <= target]
    result["input_hash"] = digest([[(str(d), prices.get(d)) for d in days], basis,
                                   CALCULATION_VERSION])
    if any(d not in prices or prices[d].get("_missing") for d in days):
        return {**result, "status":"必要価格の欠損", "excursion_status":"必要価格の欠損"}
    if len(days) < 2:
        if basis == "close" and dec(prices[start].get("C")) is not None:
            return {**result, "status":"評価済み", "baseline":dec(prices[start]["C"]),
                    "end_close":dec(prices[start]["C"]), "return_pct":D(0)}
        return result
    adjusted = {}
    with localcontext() as ctx:
        ctx.prec = 60
        factor = D(1)
        for day in reversed(days):
            row = prices[day]
            event = dec(row.get("AdjFactor"))
            if event is None or event <= 0:
                return {**result, "status":"調整係数の欠損"}
            values = {}
            for field in ("O", "H", "L", "C"):
                value = dec(row.get(field))
                if value is not None and value <= 0:
                    raise ScreeningError("価格が0以下です。")
                values[field] = value * factor if value is not None else None
            adjusted[day] = values
            factor *= event
        base = adjusted[start]["C"] if basis == "close" else adjusted[days[1]]["O"]
        end = adjusted[target]["C"]
        result.update(baseline=base, end_close=end)
        highs = [adjusted[d]["H"] for d in days[1:]]
        lows = [adjusted[d]["L"] for d in days[1:]]
        if base is not None and all(x is not None for x in highs + lows):
            result.update(max_up_pct=percent(max(highs), base),
                          max_down_pct=percent(min(lows), base), excursion_status="評価済み")
        else:
            result["excursion_status"] = "必要価格の欠損"
        if base is None or end is None:
            return {**result, "status":"必要価格の欠損"}
        return {**result, "status":"評価済み", "return_pct":percent(end, base)}


def summarize(items):
    valid = [dec(x["return_pct"]) for x in items if x["status"] == "評価済み" and x.get("return_pct") is not None]
    return dict(total=len(items), valid=len(valid),
                pending=sum(x["status"] == "評価待ち" for x in items),
                missing=sum(x["status"] not in {"評価済み", "評価待ち"} for x in items),
                unique_codes=len({x["code"] for x in items}),
                mean=sum(valid) / len(valid) if valid else None,
                median=median(valid) if valid else None,
                positive_pct=D(sum(v > 0 for v in valid)) / len(valid) * 100 if valid else None,
                distribution=sorted(valid))


def quartiles(items, feature):
    if feature not in FEATURE_NAMES:
        raise ScreeningError("未対応の特徴量です。")
    values = sorted(dec(x["features"][feature]) for x in items if x["features"].get(feature) is not None)
    if not values:
        return dict(boundaries=[], groups=[], unknown_features=len(items))
    # Nearest-rank quartiles. Equal values never split across groups.
    boundaries = [values[max(0, (len(values)*q + 3)//4 - 1)] for q in (1, 2, 3)]
    buckets = [[] for _ in range(4)]
    for item in items:
        value = dec(item["features"].get(feature))
        if value is not None:
            buckets[sum(value > b for b in boundaries)].append(item)
    return dict(boundaries=boundaries, groups=[dict(quartile=i+1, **summarize(b)) for i, b in enumerate(buckets)],
                unknown_features=len(items)-len(values))
