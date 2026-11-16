"""Private trade snapshots, moving-average cost and saved portfolio valuations."""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re

from sqlalchemy import select, text, update, func
from sqlalchemy.dialects.postgresql import insert

from ..core import JST, ScreeningError, TradingCalendar, code5
from . import schema as s
from .domain import digest, representative

HEADERS = ("約定日", "受渡日", "銘柄コード", "銘柄名", "口座区分", "取引区分",
           "売買区分", "数量［株］", "単価［円］", "手数料［円］", "税金等［円］", "諸費用［円］")
LOCK = 18426004
VERSION = "portfolio-1"


def decimal(value):
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        raise ScreeningError("売買履歴の数量・単価・費用に不正な数値があります。") from None
    if not result.is_finite() or result < 0:
        raise ScreeningError("売買履歴の数値は0以上の有限数で指定してください。")
    return result


def trade_day(value, optional=False):
    if optional and value in (None, "", "-"):
        return None
    try:
        if isinstance(value, (float, int)):
            if int(value) != value:
                raise ValueError()
            return date(1899, 12, 30) + timedelta(days=int(value))
        year, month, day = map(int, str(value).replace("/", "-").split("-"))
        return date(year, month, day)
    except (ValueError, TypeError, OverflowError):
        raise ScreeningError("売買履歴の日付を確認してください。") from None


def prepare_snapshot(payload):
    source = payload.get("source", {})
    sheet_id, title = source.get("spreadsheet_id", ""), source.get("sheet_name", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", sheet_id) or not isinstance(title, str) or not title.strip():
        raise ScreeningError("Google SheetsのIDとシート名が必要です。")
    if payload.get("complete") is not True:
        raise ScreeningError("全行の取得が完了したスナップショットだけを取り込めます。")
    try:
        observed = datetime.fromisoformat(payload["observed_at"])
        if observed.tzinfo is None:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ScreeningError("取得日時にはタイムゾーンを指定してください。") from None
    values = payload.get("values", [])
    if not values or not isinstance(values, list) or not all(isinstance(row, list) for row in values):
        raise ScreeningError("ヘッダー付きの売買履歴が必要です。")
    headers = values[0]
    if len(headers) != len(set(headers)) or not set(HEADERS).issubset(headers):
        raise ScreeningError("売買履歴の必須列が不足または重複しています。")
    trades = []
    for ordinal, cells in enumerate(values[1:], 2):
        if not any(value not in (None, "") for value in cells):
            continue
        raw = dict(zip(headers, cells))
        try:
            if raw["取引区分"] != "現物" or raw["売買区分"] not in {"買付", "売付"}:
                raise ScreeningError(f"{ordinal}行目は対応する現物の買付・売付ではありません。")
            day = trade_day(raw["約定日"])
            settlement = trade_day(raw.get("受渡日"), optional=True)
            quantity, price = decimal(raw["数量［株］"]), decimal(raw["単価［円］"])
            costs = sum((decimal(raw[name]) for name in ("手数料［円］", "税金等［円］", "諸費用［円］")), Decimal(0))
            code = code5(str(raw["銘柄コード"]))
            account = str(raw["口座区分"]).strip()
            name = str(raw["銘柄名"]).strip()
        except KeyError:
            raise ScreeningError(f"{ordinal}行目の売買履歴に空欄があります。") from None
        if not quantity or not price or quantity != quantity.to_integral_value() or not account or not name:
            raise ScreeningError(f"{ordinal}行目の数量・単価・口座・銘柄名を確認してください。")
        if settlement and settlement < day:
            raise ScreeningError(f"{ordinal}行目の受渡日が約定日より前です。")
        if day > observed.astimezone(JST).date():
            raise ScreeningError(f"{ordinal}行目の約定日が取得日より後です。")
        trades.append(dict(ordinal=ordinal, trade_date=day, settlement_date=settlement,
            code=code, name=name, account=account, side=raw["売買区分"], quantity=quantity,
            unit_price=price, costs=costs, raw=raw))
    tab_id = source.get("sheet_id")
    if not isinstance(tab_id, int) or isinstance(tab_id, bool) or tab_id < 0:
        raise ScreeningError("取得元の固定シートIDが必要です。")
    source_id = digest([sheet_id, tab_id])
    occurrences = defaultdict(int)
    for trade in trades:
        signature = digest({key:value for key,value in trade.items() if key not in {"ordinal", "raw"}})
        trade["trade_key"] = digest([source_id, signature, occurrences[signature]])
        occurrences[signature] += 1
    # Preserve repeated identical executions, including their same-day source order.
    snapshot_id = digest([source_id, trades])
    return source_id, snapshot_id, observed, trades


def active_trades(conn):
    return [dict(row) for row in conn.execute(select(s.portfolio_trades).join(
        s.portfolio_sources, s.portfolio_trades.c.snapshot_id == s.portfolio_sources.c.snapshot_id)
        .order_by(s.portfolio_trades.c.trade_date, s.portfolio_trades.c.ordinal)).mappings()]


def import_snapshot(db, payload, now=None):
    source_id, snapshot_id, observed, trades = prepare_snapshot(payload)
    now = now or datetime.now(timezone.utc)
    if observed > now + timedelta(minutes=5):
        raise ScreeningError("取得日時が未来になっています。")
    with db.transaction() as conn:
        conn.execute(text(f"SELECT pg_advisory_xact_lock({LOCK})"))
        old = conn.execute(select(s.portfolio_sources).where(s.portfolio_sources.c.id == source_id)).mappings().first()
        if old and observed < old["checked_at"]:
            raise ScreeningError("より新しい売買履歴が既に保存されています。")
        if old and observed == old["checked_at"] and old["snapshot_id"] != snapshot_id:
            raise ScreeningError("同じ取得日時の売買履歴が競合しています。")
        source = payload["source"]
        url = f"https://docs.google.com/spreadsheets/d/{source['spreadsheet_id']}/edit"
        stmt = insert(s.portfolio_sources).values(id=source_id, url=url, sheet_name=source["sheet_name"], checked_at=observed)
        conn.execute(stmt.on_conflict_do_update(index_elements=["id"], set_={"checked_at":observed, "sheet_name":source["sheet_name"]}))
        exists = conn.execute(select(s.portfolio_imports.c.id).where(s.portfolio_imports.c.id == snapshot_id)).scalar_one_or_none()
        if not exists:
            conn.execute(s.portfolio_imports.insert().values(id=snapshot_id, source_id=source_id,
                observed_at=observed, content=payload))
            if trades:
                conn.execute(s.portfolio_trades.insert(), [dict(snapshot_id=snapshot_id, **row) for row in trades])
        conn.execute(update(s.portfolio_sources).where(s.portfolio_sources.c.id == source_id).values(snapshot_id=snapshot_id))
        for row in trades:
            if row["side"] == "買付":
                recommendation = conn.execute(select(s.recommendations.c.id).where(s.recommendations.c.code == row["code"],
                    s.recommendations.c.recommended_on <= row["trade_date"]).order_by(
                    s.recommendations.c.recommended_on.desc(), s.recommendations.c.recorded_at.desc())).scalar()
                if recommendation:
                    conn.execute(insert(s.portfolio_links).values(trade_key=row["trade_key"], recommendation_id=recommendation)
                        .on_conflict_do_nothing())
            stmt = insert(s.tracking).values(code=row["code"], first_date=row["trade_date"], paused=False, missing_days=0, state="未更新")
            conn.execute(stmt.on_conflict_do_update(index_elements=["code"], set_={
                "first_date":func.least(s.tracking.c.first_date, row["trade_date"]), "paused":False}))
    return dict(status="skipped" if old and old["snapshot_id"] == snapshot_id else "imported",
                trades=len(trades), snapshot_id=snapshot_id)


def register_recommendations(db, entries, now=None):
    now = now or datetime.now(timezone.utc)
    with db.transaction() as conn:
        conn.execute(text(f"SELECT pg_advisory_xact_lock({LOCK})"))
        for entry in entries:
            code = code5(str(entry["code"]))
            day = trade_day(entry["recommended_on"], optional=True)
            if not entry.get("reason") or not entry.get("source"):
                raise ScreeningError("推薦理由と出典が必要です。")
            context = None
            if day:
                runs = representative(list(conn.execute(select(s.runs).where(s.runs.c.asof < day)).mappings()))
                if runs:
                    run = max(runs, key=lambda row:(row["asof"], row["created_at"], row["id"]))
                    row = conn.execute(select(s.results).where(s.results.c.run_id == run["id"], s.results.c.code == code)).mappings().first()
                    if row:
                        context = dict(run_id=run["id"], asof=str(run["asof"]), s1=row["s1"], s2=row["s2"], s3=row["s3"],
                                       features=row["features"], payload=row["payload"])
            content = {**entry, "screening_at_registration":context}
            identity = digest(entry)
            stmt = insert(s.recommendations).values(id=identity, code=code, name=entry["name"], recommended_on=day,
                recorded_at=now, reason=entry["reason"], risks=entry.get("risks", ""), source=entry["source"], content=content)
            conn.execute(stmt.on_conflict_do_nothing(index_elements=["id"]))
            for trade in active_trades(conn):
                if trade["code"] == code and trade["side"] == "買付" and (day is None or day <= trade["trade_date"]):
                    conn.execute(insert(s.portfolio_links).values(trade_key=trade["trade_key"], recommendation_id=identity)
                        .on_conflict_do_nothing())
    return len(entries)


def portfolio_codes(conn):
    return set(conn.execute(select(s.portfolio_trades.c.code).join(s.portfolio_sources,
        s.portfolio_trades.c.snapshot_id == s.portfolio_sources.c.snapshot_id)).scalars())


def calculate_position(trades, quotes, sessions, asof, price_cutoff=None):
    """Moving-average acquisition cost, adjusted share count and net sale proceeds."""
    trades = sorted((row for row in trades if row["trade_date"] <= asof), key=lambda row:(row["trade_date"], row["ordinal"]))
    if not trades:
        return None
    first = trades[0]["trade_date"]
    needed = {day for day in sessions if first <= day <= (price_cutoff or asof)}
    by_day = defaultdict(list)
    for row in trades:
        by_day[row["trade_date"]].append(row)
    quantity = cost = realized = Decimal(0)
    state = "評価済み"
    uncertain = None
    for day in sorted(needed | set(by_day)):
        quote = quotes.get(day)
        factor = quote.get("factor") if quote else None
        if quantity and day in needed:
            if factor is None or factor <= 0:
                uncertain = "株価・調整係数の欠損"
            else:
                quantity /= factor
        for row in by_day[day]:
            amount = row["quantity"] * row["unit_price"]
            if row["side"] == "買付":
                quantity += row["quantity"]
                cost += amount + row["costs"]
            elif row["quantity"] > quantity:
                uncertain = "売却数量と保有履歴の照合待ち"
            else:
                basis = cost * row["quantity"] / quantity
                realized += amount - row["costs"] - basis
                quantity -= row["quantity"]
                cost -= basis
    last_trade = trades[-1]["trade_date"]
    valid = [day for day in needed if quotes.get(day, {}).get("close") is not None]
    price_day = max(valid) if valid else None
    if not sessions:
        state = "取引カレンダー待ち"
    elif quantity and (price_day is None or price_day < last_trade):
        state = "買付日以降の終値待ち"
    elif quantity and price_day != max(needed, default=None):
        state = "最新終値の取得待ち"
    state = uncertain or state
    price = quotes[price_day]["close"] if price_day and state == "評価済み" else None
    value = quantity * price if price is not None else Decimal(0) if not quantity and state == "評価済み" else None
    unrealized = value - cost if value is not None else None
    return dict(code=trades[-1]["code"], name=trades[-1]["name"], account=trades[-1]["account"],
        quantity=quantity if state in {"評価済み", "買付日以降の終値待ち", "最新終値の取得待ち"} else None,
        cost=cost if uncertain is None else None,
        average_cost=cost / quantity if quantity and uncertain is None else None, price_day=price_day, price=price,
        market_value=value, unrealized=unrealized, return_pct=unrealized / cost * 100 if unrealized is not None and cost else None,
        realized=realized if state == "評価済み" else None, status=state, first_trade=first,
        last_trade=last_trade, asof=asof, version=VERSION)


def portfolio_data(conn):
    trades = active_trades(conn)
    codes = {row["code"] for row in trades}
    quotes = defaultdict(dict)
    for row in conn.execute(select(s.prices).where(s.prices.c.code.in_(codes))).mappings():
        quotes[row["code"]][row["day"]] = dict(row)
    sessions = set(conn.execute(select(s.calendar.c.day).where(s.calendar.c.kind.in_(["1", "2"]))).scalars())
    groups = defaultdict(list)
    for row in trades:
        groups[(row["code"], row["account"])].append(row)
    return trades, groups, quotes, sessions


def evaluate_portfolio(db, now=None):
    now = now or datetime.now(JST)
    from .tracking import latest_closed_day
    with db.transaction() as conn:
        conn.execute(text(f"SELECT pg_advisory_xact_lock({LOCK})"))
        _, groups, quotes, sessions = portfolio_data(conn)
        if not groups or not sessions:
            return dict(saved=0, securities=len(groups))
        cal = TradingCalendar({row["day"]:row["kind"] for row in conn.execute(select(s.calendar)).mappings()})
        asof = latest_closed_day(cal, now)
        count = 0
        for (code, account), trades in groups.items():
            days = sorted(day for day in sessions if trades[0]["trade_date"] <= day <= asof and day in quotes[code])
            for day in days:
                position = calculate_position(trades, quotes[code], sessions, day)
                signature = digest([VERSION, trades, sorted(str(d) for d in sessions if d <= day),
                    [(str(d), row["digest"]) for d, row in sorted(quotes[code].items()) if d <= day]])
                stmt = insert(s.portfolio_values).values(day=day, code=code, account=account, input_hash=signature,
                    content=position, created_at=now)
                saved = conn.execute(stmt.on_conflict_do_nothing().returning(s.portfolio_values.c.day)).scalar_one_or_none()
                count += saved is not None
    return dict(saved=count, securities=len(groups))


def overview(db, now=None):
    now = now or datetime.now(JST)
    from .tracking import latest_closed_day
    with db.transaction(readonly=True) as conn:
        trades, groups, quotes, sessions = portfolio_data(conn)
        cal = TradingCalendar({row["day"]:row["kind"] for row in conn.execute(select(s.calendar)).mappings()})
        cutoff = latest_closed_day(cal, now) if sessions else now.astimezone(JST).date()
        recommendations = {row["trade_key"]:dict(row) for row in conn.execute(select(
            s.recommendations, s.portfolio_links.c.trade_key).join(
            s.portfolio_links, s.recommendations.c.id == s.portfolio_links.c.recommendation_id)).mappings()}
        runs = representative(list(conn.execute(select(s.runs).where(s.runs.c.asof <= now.astimezone(JST).date())).mappings()))
        latest_run = max(runs, key=lambda row:(row["asof"], row["created_at"], row["id"])) if runs else None
        flags = {row["code"]:dict(row) for row in conn.execute(select(s.results).where(
            s.results.c.run_id == latest_run["id"])).mappings()} if latest_run else {}
        sources = [dict(row) for row in conn.execute(select(s.portfolio_sources)).mappings()]
    positions = []
    for (code, _), group in groups.items():
        position = calculate_position(group, quotes[code], sessions, now.astimezone(JST).date(), cutoff)
        if position is None:
            continue
        linked = {recommendations[row["trade_key"]]["id"]:recommendations[row["trade_key"]]
                  for row in group if row["trade_key"] in recommendations and row["trade_date"] <= position["asof"]}
        flag = flags.get(code)
        position.update(recommendations=list(linked.values()), screening_date=latest_run["asof"] if flag else None,
            stage1=flag["s1"] if flag else "未観測", stage2=flag["s2"] if flag else "未観測",
            stage3=flag["s3"] if flag else "未観測")
        positions.append(position)
    return dict(positions=positions, trades=trades, sources=sources)
