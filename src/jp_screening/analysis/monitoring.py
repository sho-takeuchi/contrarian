"""Rebuildable, deterministic stage-two observations and interval returns."""
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext

from sqlalchemy import select, update, text
from sqlalchemy.dialects.postgresql import insert

from . import schema as s
from .db import jsonable
from .domain import dec, digest, percent, FEATURE_NAMES
from .tracking import latest_closed_day
from ..core import JST, TradingCalendar

VERSION = "stage2-monitor-1"
FEATURE_LABELS = {
    "long_low_rise": "長期最安終値からの上昇率", "ceiling_room": "許容上限までの余裕率",
    "monthly_low_margin": "直近3か月の安値の上昇率", "monthly_ma_gap": "月足の3か月平均との乖離率",
    "weekly_ma_gap": "週足の5週平均との乖離率", "weekly_ma_change": "5週平均の変化率",
}


def representatives(runs):
    """Monitoring uses the latest real full-universe run, irrespective of depth >= 2."""
    selected = {}
    for run in runs:
        if not run["eligible"] or run["stage"] < 2:
            continue
        key = (run["rules_key"], run["asof"])
        if key not in selected or (run["created_at"], run["id"]) > (selected[key]["created_at"], selected[key]["id"]):
            selected[key] = run
    return sorted(selected.values(), key=lambda r:(r["asof"], r["rules_key"], r["id"]))


def status(row):
    if row is None:
        return "未観測"
    # Structural exclusion or missing data is not an observed rule failure.
    if row["s1"] == "不通過" or (row["s1"] == "通過" and row["s2"] == "不通過"):
        return "不通過"
    if row["s1"] == row["s2"] == "通過":
        return "通過"
    return "対象外" if row["s1"] == "対象外" else "判定不能"


def failure_details(row):
    payload = row.get("payload", {})
    monthly = row["s1"] == "不通過"
    fields = (("condition_a", "許容上限超過"), ("condition_b", "月足安値条件不成立"),
              ("condition_c", "3か月平均以下")) if monthly else (
              ("weekly_condition_1", "週足終値が5週平均以下"),
              ("weekly_condition_2", "5週平均が前週以下"))
    false = [label for key, label in fields if payload.get(key) is False or str(payload.get(key)).lower() == "false"]
    reason = payload.get("stage1_reason" if monthly else "stage2_reason") or payload.get("reason") or "不通過の保存記録を参照"
    return dict(stage=1 if monthly else 2, reason=reason, failed_conditions=false,
                values={key:payload.get(key) for key in (
                    "current_close", "lowest_close", "upper_limit", "m1_low", "m2_low", "m3_low", "m4_low",
                    "m1_close", "sma3", "w1_close", "sma5_week", "previous_sma5_week")})


def build_periods(runs, rows, sessions):
    selected = representatives(runs)
    by_run = {}
    for row in rows:
        by_run.setdefault(row["run_id"], {})[row["code"]] = row
    positions = {day:i for i, day in enumerate(sorted(sessions))}
    opened, periods, days, previous = {}, [], [], {}
    for run in selected:
        rule, day = run["rules_key"], run["asof"]
        last = previous.get(rule)
        gap = last is not None and (last not in positions or day not in positions or positions[day] - positions[last] != 1)
        previous[rule] = day
        current = by_run.get(run["id"], {})
        for key, period in list(opened.items()):
            if key[0] == rule and (gap or key[1] not in current):
                period["continuity_unknown"] = True
        for code in sorted(set(current) | {k[1] for k in opened if k[0] == rule}):
            row = current.get(code)
            current_status = status(row)
            key = (rule, code)
            period = opened.get(key)
            if current_status == "通過" and period is None:
                period = dict(id=digest([VERSION, rule, code, str(day)]), code=code, name=row["name"],
                    rules_key=rule, start_date=day, start_run=run["id"], start_snapshot=dict(row),
                    end_date=None, end_run=None, last_date=day, last_status=current_status,
                    continuity_unknown=False, failure=None)
                periods.append(period)
                opened[key] = period
            if period is None:
                continue
            period.update(last_date=day, last_status=current_status)
            if current_status == "不通過":
                period.update(end_date=day, end_run=run["id"], end_snapshot=dict(row), failure=failure_details(row))
                opened.pop(key)
            elif current_status != "通過":
                period["continuity_unknown"] = True
            days.append(dict(id=digest([period["id"], str(day), run["id"]]), period_id=period["id"],
                day=day, run_id=run["id"], active=True,
                content=jsonable(dict(status=current_status, snapshot=dict(row) if row else None))))
    return periods, days


def _interval(prices, sessions, start, end):
    """Exact endpoints; adjust every quote to end's basis. Never substitute a day."""
    result = dict(status="必要価格の欠損", baseline=None, end_close=None, return_pct=None,
                  max_up_pct=None, max_down_pct=None, trading_days=None, start=str(start), end=str(end))
    ordered = sorted(day for day in sessions if start <= day <= end)
    if not ordered or ordered[0] != start or ordered[-1] != end:
        return {**result, "status":"カレンダー不足"}
    result["trading_days"] = len(ordered) - 1
    if any(day not in prices or prices[day].get("_missing") for day in ordered):
        return result
    factor, adjusted = Decimal(1), {}
    for day in reversed(ordered):
        quote = prices[day]
        multiplier = dec(quote.get("AdjFactor"))
        if multiplier is None or multiplier <= 0:
            return {**result, "status":"調整係数の欠損"}
        adjusted[day] = {key:(dec(quote.get(key)) * factor if dec(quote.get(key)) is not None else None)
                         for key in ("O", "H", "L", "C")}
        if any(value is not None and value <= 0 for value in adjusted[day].values()):
            return {**result, "status":"価格が不正"}
        factor *= multiplier
    baseline, ending = adjusted[start]["C"], adjusted[end]["C"]
    result.update(baseline=baseline, end_close=ending)
    if baseline is None or ending is None:
        return result
    result.update(status="評価済み", return_pct=percent(ending, baseline))
    if start != end:
        highs = [adjusted[d]["H"] for d in ordered[1:]]
        lows = [adjusted[d]["L"] for d in ordered[1:]]
        if all(v is not None for v in highs + lows):
            result.update(max_up_pct=percent(max(highs), baseline), max_down_pct=percent(min(lows), baseline))
    return result


def interval(prices, sessions, start, end):
    with localcontext() as context:
        context.prec = 60
        return _interval(prices, sessions, start, end)


def upsert(conn, table, value, keys):
    statement = insert(table).values(**value)
    conn.execute(statement.on_conflict_do_update(index_elements=keys,
        set_={key:value for key,value in value.items() if key not in keys}))


def rebuild(db, now=None):
    now = now or datetime.now(JST)
    with db.transaction() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(18426003)"))
        runs = list(conn.execute(select(s.runs)).mappings())
        rows = list(conn.execute(select(s.results)).mappings())
        calendar = {r["day"]:r["kind"] for r in conn.execute(select(s.calendar)).mappings()}
        sessions = {day for day, kind in calendar.items() if kind in {"1", "2"}}
        periods, days = build_periods(runs, rows, sessions)
        try:
            cutoff = latest_closed_day(TradingCalendar(calendar), now)
            if cutoff == now.date() and now.hour < 18:
                cutoff = max(day for day in sessions if day < now.date())
        except Exception:
            cutoff = max((r["asof"] for r in representatives(runs)), default=now.date())
        quotes = {}
        for quote in conn.execute(select(s.prices)).mappings():
            quotes.setdefault(quote["code"], {})[quote["day"]] = quote["payload"]
        old = {r["id"]:r for r in conn.execute(select(s.monitor_periods)).mappings()}
        conn.execute(update(s.monitor_periods).values(active=False))
        conn.execute(update(s.monitor_days).values(active=False))
        for period in periods:
            code = period["code"]
            data = quotes.get(code, {})
            latest = interval(data, sessions, period["start_date"], cutoff)
            ended = interval(data, sessions, period["start_date"], period["end_date"]) if period["end_date"] else None
            pct = dec(ended["return_pct"]) if ended else None
            direction = "unknown" if pct is None else "up" if pct > 0 else "down" if pct < 0 else "flat"
            state = "ended" if period["end_date"] else "passing" if period["last_status"] == "通過" else "uncertain"
            if not period["end_date"] and period["last_date"] < cutoff:
                state = "uncertain"
                period["continuity_unknown"] = True
            for observation in (d for d in days if d["period_id"] == period["id"]):
                observation["content"]["measurement"] = jsonable(interval(data, sessions, period["start_date"], observation["day"]))
            content = jsonable({**period, "latest":latest, "exit":ended, "version":VERSION})
            hash_value = digest(content)
            previous = old.get(period["id"])
            if previous and previous["digest"] != hash_value:
                conn.execute(insert(s.monitor_revisions).values(period_id=period["id"], digest=previous["digest"],
                    content=previous["content"], recorded_at=now).on_conflict_do_nothing())
            upsert(conn, s.monitor_periods, dict(id=period["id"], code=code, rules_key=period["rules_key"],
                start_date=period["start_date"], end_date=period["end_date"], active=True, state=state,
                direction=direction, content=content, digest=hash_value,
                updated_at=previous["updated_at"] if previous and previous["digest"] == hash_value else now), ["id"])
            # Resume all stage-two securities; the read-only dashboard has no pause control.
            tracking = conn.execute(select(s.tracking).where(s.tracking.c.code == code)).mappings().first()
            first = min(tracking["first_date"], period["start_date"]) if tracking else period["start_date"]
            statement = insert(s.tracking).values(code=code, first_date=first, paused=False, missing_days=0, state="未更新")
            conn.execute(statement.on_conflict_do_update(index_elements=["code"], set_={"first_date":first, "paused":False}))
        for observation in days:
            upsert(conn, s.monitor_days, observation, ["id"])
    return dict(periods=len(periods), observations=len(days))


def bucket(period):
    content = period["content"]
    if period["state"] == "passing" and content["latest"]["status"] == "評価済み" and not content["continuity_unknown"]:
        return "通過中"
    if period["state"] == "ended" and not content["continuity_unknown"]:
        if period["direction"] == "up":
            return "上昇して通過終了"
        if period["direction"] == "down":
            return "下落して通過終了"
    return "変化なし・要確認"


def summary(periods):
    eligible = [p for p in periods if p["active"] and p["state"] == "ended"
                and p["direction"] in {"up", "down"} and not p["content"]["continuity_unknown"]]
    groups = {}
    for rule in sorted({p["rules_key"] for p in eligible}):
        sample = [p for p in eligible if p["rules_key"] == rule]
        rule_groups = {}
        for direction in ("up", "down"):
            values = [p for p in sample if p["direction"] == direction]
            feature_means = {}
            for feature in FEATURE_LABELS:
                numbers = [dec(p["content"]["start_snapshot"]["features"].get(feature)) for p in values]
                numbers = [n for n in numbers if n is not None]
                feature_means[feature] = dict(valid=len(numbers), mean=sum(numbers)/len(numbers) if numbers else None)
            returns = [dec(p["content"]["exit"]["return_pct"]) for p in values]
            rule_groups[direction] = dict(events=len(values), securities=len({p["code"] for p in values}),
                mean_return=sum(returns)/len(returns) if returns else None, features=feature_means,
                exit_reasons=dict(Counter(p["content"]["failure"]["reason"] for p in values)),
                by_date=dict(Counter(p["content"]["end_date"] for p in values)),
                mean_duration=(sum(p["content"]["exit"]["trading_days"] for p in values)/len(values)) if values else None)
        sufficient = all(rule_groups[key]["securities"] >= 10 for key in ("up", "down"))
        comparisons = []
        if sufficient:
            for feature, label in FEATURE_LABELS.items():
                up, down = (dec(rule_groups[key]["features"][feature]["mean"]) for key in ("up", "down"))
                if up is not None and down is not None:
                    comparisons.append(f"{label}の平均は、上昇終了群 {up:.2f}％、下落終了群 {down:.2f}％でした。")
        groups[rule] = dict(groups=rule_groups, state="比較可能" if sufficient else "蓄積不足", comparisons=comparisons)
    return jsonable(dict(version=VERSION, rules=groups,
        conclusion=" / ".join(line for value in groups.values() for line in value["comparisons"]) or
                   "蓄積不足：上昇・下落それぞれ10銘柄以上の通過終了記録を待っています。",
        note="観測された関連の比較です。通過中・観測不足は上昇終了群や下落終了群に含めません。",
        counts=dict(Counter(bucket(p) for p in periods if p["active"]))))
