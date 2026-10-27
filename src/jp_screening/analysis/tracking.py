from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select, update, and_
from sqlalchemy.dialects.postgresql import insert

from ..api import Client
from ..core import ScreeningError, TradingCalendar, JST, code5
from . import schema as s
from .domain import digest, dec, outcome, HORIZONS, representative


def latest_closed_day(cal, now):
    now = now.astimezone(JST)
    today = now.date()
    sessions = cal.sessions(today - timedelta(days=20), today)
    if sessions and sessions[-1] == today:
        closing = time(11, 30) if cal.days[today] == "2" else time(15, 30)
        if now.time() < closing:
            sessions.pop()
    if not sessions:
        raise ScreeningError("直近の確定営業日がありません。")
    return sessions[-1]


def save_calendar(db, rows):
    cal = TradingCalendar.from_rows(rows)
    with db.transaction() as conn:
        for day, kind in cal.days.items():
            statement = insert(s.calendar).values(day=day, kind=kind)
            conn.execute(statement.on_conflict_do_update(index_elements=["day"], set_={"kind":kind}))
    return cal


def save_prices(db, code, rows, now, expected=None):
    rows = list(rows)
    prepared, seen = [], set()
    for row in rows:
        day = date.fromisoformat(row["Date"])
        if code5(row["Code"]) != code or day in seen:
            raise ScreeningError("追跡株価の銘柄不一致または重複があります。")
        seen.add(day)
        payload = {k:row.get(k) for k in ("Date", "Code", "O", "H", "L", "C", "AdjFactor", "Vo", "Va")}
        if row.get("_missing"):
            payload["_missing"] = True
        values = {name:dec(row.get(field)) for name, field in
                  (("open","O"),("high","H"),("low","L"),("close","C"),("factor","AdjFactor"))}
        if any(v is not None and v <= 0 for v in values.values()):
            raise ScreeningError("追跡株価・調整係数が不正です。")
        prepared.append(dict(code=code, day=day, payload=payload, digest=digest(payload), fetched_at=now, **values))
    with db.transaction() as conn:
        # Serialize writers for this security; versions remain stable on retries.
        conn.execute(select(s.tracking.c.code).where(s.tracking.c.code == code).with_for_update())
        existing = {r["day"]:r for r in conn.execute(select(s.prices).where(s.prices.c.code == code)).mappings()}
        # An omitted old record may have aged out of the API's retention window.
        # Absence alone is not a correction of a quote we already observed.
        if expected is not None:
            for day in sorted(set(expected) - seen - existing.keys()):
                payload = dict(Date=str(day), Code=code, _missing=True)
                prepared.append(dict(code=code, day=day, payload=payload, digest=digest(payload),
                    fetched_at=now, open=None, high=None, low=None, close=None, factor=None))
        changed = 0
        for row in prepared:
            old = existing.get(row["day"])
            if old and old["digest"] == row["digest"]:
                continue
            if old:
                conn.execute(s.price_revisions.insert().values(**{k:old[k] for k in
                    ("code", "day", "revision", "payload", "digest", "fetched_at")}))
            row["revision"] = old["revision"] + 1 if old else 1
            statement = insert(s.prices).values(**row)
            conn.execute(statement.on_conflict_do_update(index_elements=["code", "day"],
                set_={k:v for k, v in row.items() if k not in {"code", "day"}}))
            changed += 1
    return changed


def update_prices(db, key, now=None, client=None, codes=None, asof=None):
    now = now or datetime.now(JST)
    client = client or Client(key)
    cal = save_calendar(db, list(client.rows("/markets/calendar")))
    cutoff = asof or latest_closed_day(cal, now)
    published = client.daily(day=cutoff)
    if not published or any(r["Date"] != str(cutoff) for r in published) or not any(r.get("C") for r in published):
        raise ScreeningError("直近営業日の株価は更新待ちです。古い日付に置き換えません。")
    with db.transaction(readonly=True) as conn:
        statement = select(s.tracking).where(s.tracking.c.paused.is_(False))
        if codes is not None:
            statement = statement.where(s.tracking.c.code.in_(codes))
        targets = list(conn.execute(statement).mappings())
    report = []
    for target in targets:
        code = target["code"]
        try:
            # Refetch the tracked interval to detect corrections, including old factors.
            rows = client.daily(code=code, start=target["first_date"], end=cutoff)
            if any(not (target["first_date"] <= date.fromisoformat(r["Date"]) <= cutoff) for r in rows):
                raise ScreeningError("追跡期間外の価格が返されました。")
            expected = set(cal.sessions(target["first_date"], cutoff))
            changed = save_prices(db, code, rows, now, expected=expected)
            present = {date.fromisoformat(r["Date"]) for r in rows}
            with db.transaction(readonly=True) as conn:
                stored = {r["day"] for r in conn.execute(select(s.prices.c.day, s.prices.c.payload).where(
                    s.prices.c.code == code)).mappings() if not r["payload"].get("_missing")}
            missing = len(expected - stored)
            unconfirmed = len((expected & stored) - present)
            state = ("要確認：取得漏れ" if missing else "要確認：今回未返却の保存価格あり" if unconfirmed
                     else "要確認：価格未提供" if any(r.get("C") is None for r in rows) else "更新済み")
            with db.transaction() as conn:
                conn.execute(update(s.tracking).where(s.tracking.c.code == code).values(
                    updated_at=now, missing_days=missing, state=state))
            report.append(dict(code=code, status=state, changed=changed, missing_days=missing, unconfirmed_days=unconfirmed))
        except (ScreeningError, OSError, ValueError, KeyError):
            with db.transaction() as conn:
                conn.execute(update(s.tracking).where(s.tracking.c.code == code).values(state="更新失敗"))
            report.append(dict(code=code, status="failed"))
    return dict(cutoff=str(cutoff), securities=report,
                complete=all(x["status"] != "failed" and not x.get("missing_days") for x in report))


def set_paused(db, code, paused):
    with db.transaction() as conn:
        result = conn.execute(update(s.tracking).where(s.tracking.c.code == code5(code)).values(paused=paused))
        if result.rowcount != 1:
            raise ScreeningError("追跡対象にない銘柄です。")


def evaluate_outcomes(db, now=None):
    now = now or datetime.now(JST)
    with db.transaction(readonly=True) as conn:
        calendar_rows = list(conn.execute(select(s.calendar)).mappings())
        if not calendar_rows:
            return 0
        cal = TradingCalendar({r["day"]:r["kind"] for r in calendar_rows})
        cutoff = latest_closed_day(cal, now)
        sessions = {r["day"] for r in calendar_rows if r["kind"] in {"1", "2"}}
        runs = representative(list(conn.execute(select(s.runs)).mappings()))
        run_map = {r["id"]:r for r in runs}
        rows = list(conn.execute(select(s.results).where(s.results.c.run_id.in_(run_map), s.results.c.s1 == "通過")).mappings())
        tracked = {r["code"]:r for r in conn.execute(select(s.tracking)).mappings()}
    count = 0
    for code in sorted({r["code"] for r in rows}):
        with db.transaction(readonly=True) as conn:
            quotes = {r["day"]:r["payload"] for r in conn.execute(select(s.prices).where(s.prices.c.code == code)).mappings()}
        values = []
        for row in (r for r in rows if r["code"] == code):
            for horizon in (0, *HORIZONS):
                for basis in ("close", "next_open"):
                    result = outcome(quotes, sessions, run_map[row["run_id"]]["asof"], horizon, basis, cutoff)
                    result["tracking_paused"] = tracked.get(code, {}).get("paused", False)
                    values.append(dict(run_id=row["run_id"], code=code, horizon=horizon, basis=basis,
                        status=result["status"], return_pct=result["return_pct"], max_up_pct=result["max_up_pct"],
                        max_down_pct=result["max_down_pct"], payload=result, evaluated_at=now))
        with db.transaction() as conn:
            for value in values:
                statement = insert(s.outcomes).values(**value)
                conn.execute(statement.on_conflict_do_update(index_elements=["run_id", "code", "horizon", "basis"],
                    set_={k:v for k,v in value.items() if k not in {"run_id", "code", "horizon", "basis"}}))
        count += len(values)
    return count
