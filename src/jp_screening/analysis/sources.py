"""Free, credential-separated evidence acquisition with explicit coverage states."""
from datetime import date, datetime, time as day_time, timedelta, timezone
from io import BytesIO
import ipaddress
import json
import re
import socket
import time
import unicodedata
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser

import requests
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..api import Client
from ..core import JST, ScreeningError, code5
from . import schema as s
from .db import jsonable
from .domain import digest
from .monitoring import upsert

UTC = timezone.utc
USER_AGENT = "ContrarianResearch/1.0"
FIN_LABELS = {"Sales":"売上高", "OP":"営業利益", "OdP":"経常利益", "NP":"純利益", "EPS":"1株利益",
    "FSales":"売上高予想", "FOP":"営業利益予想", "FNP":"純利益予想", "FDivAnn":"年間配当予想",
    "NxFSales":"翌期売上高予想", "NxFOP":"翌期営業利益予想", "CurPerType":"対象期間",
    "CurFYSt":"事業年度開始", "CurFYEn":"事業年度終了", "DocType":"開示種別"}


def stamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None


def normalize(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    for token in ("株式会社", "有限会社", "co.,ltd.", "co., ltd.", "corporation", "incorporated"):
        value = value.replace(token, "")
    return re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]", "", value)


def matches_company(body, code, aliases):
    normalized = normalize(body)
    if re.search(r"(?:証券コード|銘柄コード|TSE[: ]|TYO[: ]|<|＜)\s*" + re.escape(code[:4]) + r"(?:0)?(?:\.T)?(?:>|＞|\b)", body.upper()):
        return True
    for alias in aliases:
        target = normalize(alias)
        # Short generic names require a stock-market context as well.
        if len(target) >= 3 and target in normalized:
            if len(target) >= 7 or re.search(r"株|決算|業績|stock|shares|earnings|securities", body, re.I):
                return True
    return False


def public_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ScreeningError("公開HTTPS URL以外は取得しません。")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ScreeningError("ローカル・内部ネットワークのURLは取得しません。")
    except (socket.gaierror, ValueError):
        raise ScreeningError("公開ホストを確認できません。") from None
    return url


class PublicHTTP:
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.robots = {}
        self.last_gdelt = 0

    def get(self, url, params=None, max_size=5_000_000, redirects=3):
        for _ in range(redirects + 1):
            public_url(url)
            try:
                with self.session.get(url, params=params, headers={"User-Agent":USER_AGENT},
                        timeout=(8, 25), allow_redirects=False, stream=True) as response:
                    if 300 <= response.status_code < 400:
                        url, params = urljoin(url, response.headers.get("Location", "")), None
                        continue
                    if response.status_code != 200:
                        raise ScreeningError(f"公開情報の取得待ち（HTTP {response.status_code}）")
                    data = bytearray()
                    for piece in response.iter_content(65536):
                        data.extend(piece)
                        if len(data) > max_size:
                            raise ScreeningError("公開資料のサイズ上限を超えました。")
                    return bytes(data), response.headers.get("Content-Type", ""), url
            except requests.RequestException:
                raise ScreeningError("公開情報の通信を再試行します。") from None
        raise ScreeningError("公開情報の転送上限を超えました。")

    def article(self, url):
        from bs4 import BeautifulSoup
        public_url(url)
        host = urlparse(url)
        origin = f"https://{host.netloc}"
        if origin not in self.robots:
            rules = RobotFileParser(origin + "/robots.txt")
            try:
                raw, _, _ = self.get(origin + "/robots.txt", max_size=500_000, redirects=0)
                rules.parse(raw.decode("utf-8", "replace").splitlines())
            except ScreeningError as exc:
                if "404" in str(exc):
                    rules.parse([])
                else:
                    return "", None, "本文取得許可を確認できない"
            self.robots[origin] = rules
        if not self.robots[origin].can_fetch(USER_AGENT, url):
            return "", None, "本文の自動取得対象外"
        raw, content_type, final = self.get(url, redirects=0)
        if "html" not in content_type.lower():
            return "", None, "本文形式が未対応"
        soup = BeautifulSoup(raw, "html.parser")
        # Do not treat a paywall/login preview as article content.
        if re.search(r'"isAccessibleForFree"\s*:\s*false', soup.get_text(" "), re.I) or re.search(
                r'"isAccessibleForFree"\s*:\s*false', raw.decode("utf-8", "replace"), re.I):
            return "", None, "本文にアクセス制限あり"
        published = None
        for prop in ("article:published_time", "datePublished", "pubdate"):
            meta = soup.find("meta", attrs={"property":prop}) or soup.find("meta", attrs={"name":prop})
            if meta:
                published = stamp(meta.get("content"))
                if published:
                    break
        if published is None:
            for script in soup.find_all("script", attrs={"type":"application/ld+json"}):
                try:
                    data = json.loads(script.string or script.get_text())
                    candidates = data if isinstance(data, list) else [data]
                    checked = 0
                    while candidates and checked < 1000:
                        candidate = candidates.pop()
                        checked += 1
                        if isinstance(candidate, dict):
                            candidates.extend(candidate.get("@graph", []))
                            published = stamp(candidate.get("datePublished")) or published
                except (ValueError, TypeError):
                    continue
        for item in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            item.decompose()
        article = soup.find("article") or soup.find("main")
        if not article:
            return "", published, "記事本文の範囲を特定できない"
        body = article.get_text("\n", strip=True)[:40000]
        if len(body) < 100:
            return "", published, "本文を十分に取得できない"
        return body, published, "本文取得済み" if published else "発表日時を確認できない"


class Providers:
    def __init__(self, config, api=None):
        self.config, self.api = config, api or Client(config.api_key)
        self.http = PublicHTTP()
        self.edinet_lists = {}

    def master(self, day):
        return list(self.api.rows("/equities/master", date=str(day)))

    def financial(self, code, start, end):
        result = []
        for row in self.api.rows("/fins/summary", code=code):
            if code5(row.get("Code", "")) != code or not (str(start) <= row.get("DiscDate", "") <= str(end)):
                continue
            published = stamp(f"{row['DiscDate']}T{row.get('DiscTime','00:00:00')}+09:00")
            body = "\n".join(f"{label}: {row[key]}" for key,label in FIN_LABELS.items() if row.get(key) not in (None, ""))
            result.append(dict(provider="jquants", source_key=str(row["DiscNo"]),
                title=f"財務開示 {row.get('DocType','')}", url="https://jpx-jquants.com/ja/spec/fin-summary",
                published_at=published, body=body, content={"original":row, "body_state":"数値資料", "matched":True}))
        return result

    def edinet(self, code, start, end, db, now):
        if not self.config.edinet_api_key:
            raise ScreeningError("準備待ち：EDINET_API_KEY未設定")
        from pypdf import PdfReader
        result = []
        for offset in range((end-start).days + 1):
            day = start + timedelta(days=offset)
            cache_id = digest(["edinet-list", str(day)])
            if day not in self.edinet_lists:
                with db.transaction(readonly=True) as conn:
                    stored = conn.execute(select(s.monitor_fetches).where(s.monitor_fetches.c.id == cache_id)).mappings().first()
                if stored and stored["state"] == "ready" and stored["updated_at"].astimezone(JST).date() == now.date():
                    listing = stored["content"]["rows"]
                else:
                    try:
                        response = requests.get("https://api.edinet-fsa.go.jp/api/v2/documents.json",
                            params={"date":str(day), "type":2, "Subscription-Key":self.config.edinet_api_key},
                            timeout=(10, 30), allow_redirects=False)
                        if response.status_code != 200:
                            raise ScreeningError("EDINETの認証・稼働状況を確認してください。")
                        value = response.json()
                        if str(value.get("metadata", {}).get("status", "200")) != "200":
                            raise ScreeningError("EDINETの一覧が取得できません。")
                        listing = value.get("results", [])
                    except (requests.RequestException, ValueError):
                        raise ScreeningError("EDINETの取得を再試行します。") from None
                    with db.transaction() as conn:
                        upsert(conn, s.monitor_fetches, dict(id=cache_id, provider="edinet-list", code="00000", state="ready",
                            content={"rows":listing}, updated_at=now), ["id"])
                self.edinet_lists[day] = listing
            for row in self.edinet_lists[day]:
                if row.get("secCode") != code or row.get("docTypeCode") not in {
                    "030", "040", "050", "060", "070", "080", "090", "100", "120", "130", "140", "150", "160", "170", "180", "190"}:
                    continue
                key = row["docID"]
                content = {"original":row, "matched":True, "body_state":"本文未取得"}
                body = ""
                if str(row.get("withdrawalStatus", "0")) != "0" or str(row.get("disclosureStatus", "0")) != "0":
                    content["body_state"] = "取下げ・不開示"
                elif row.get("pdfFlag") == "1":
                    with db.transaction(readonly=True) as conn:
                        old = conn.execute(select(s.monitor_sources).where(s.monitor_sources.c.provider == "edinet",
                            s.monitor_sources.c.source_key == key, s.monitor_sources.c.code == code).order_by(s.monitor_sources.c.fetched_at.desc())).mappings().first()
                    if old and old["body"]:
                        body = old["body"]
                    else:
                        try:
                            response = requests.get(f"https://api.edinet-fsa.go.jp/api/v2/documents/{key}",
                                params={"type":2, "Subscription-Key":self.config.edinet_api_key},
                                timeout=(10, 40), allow_redirects=False)
                            if response.status_code != 200 or len(response.content) > 25_000_000:
                                raise ScreeningError("EDINET本文を取得できません。")
                            pdf = PdfReader(BytesIO(response.content))
                            body = "\n".join(page.extract_text() or "" for page in list(pdf.pages)[:100])[:100000]
                        except Exception:
                            content["body_state"] = "本文取得待ち"
                    if body:
                        content["body_state"] = "本文取得済み"
                published = stamp(str(row.get("submitDateTime", "")).replace(" ", "T") + "+09:00")
                result.append(dict(provider="edinet", source_key=key, title=row.get("docDescription") or key,
                    url=f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{key}", published_at=published,
                    body=body, content=content))
        return result

    def news(self, code, aliases, english, start, end, now):
        if not english:
            raise ScreeningError("英語法人名を確認できないためニュース取得を保留")
        earliest = now.date() - timedelta(days=89)
        start = max(start, earliest)
        wait = 5 - (time.monotonic() - self.http.last_gdelt)
        if wait > 0:
            time.sleep(wait)
        self.http.last_gdelt = time.monotonic()
        search_name = re.sub(r"\b(CO|LTD|INC|CORPORATION|LIMITED)\b[., ]*", " ", english, flags=re.I)
        search_name = re.sub(r"[.,]+", " ", search_name)
        search_name = " ".join(search_name.split()).strip() or english
        query = '"' + search_name.replace('"', ' ') + '" sourcecountry:japan'
        raw, _, _ = self.http.get("https://api.gdeltproject.org/api/v2/doc/doc", params=dict(
            query=query, mode="artlist", format="json", maxrecords=250, sort="datedesc",
            startdatetime=datetime.combine(start, day_time.min, JST).astimezone(UTC).strftime("%Y%m%d%H%M%S"),
            enddatetime=min(now, datetime.combine(end, day_time.max, JST)).astimezone(UTC).strftime("%Y%m%d%H%M%S")))
        try:
            value = json.loads(raw)
            articles = value["articles"]
        except (ValueError, KeyError, TypeError):
            raise ScreeningError("GDELTの応答を再確認します。") from None
        result = []
        for item in articles:
            url, title = item.get("url", ""), item.get("title", "")
            matched = matches_company(title, code, aliases)
            result.append(dict(provider="gdelt", source_key=url, title=title, url=url,
                published_at=None, body="", content={"matched":matched, "body_state":"本文取得待ち",
                    "aliases":aliases, "query":query,
                    "first_seen":item.get("seendate"), "language":item.get("language"),
                    "coverage_start":str(earliest), "truncated":len(articles) >= 250}))
        return result


def save_source(db, code, item, now):
    if not item["url"].startswith("https://"):
        return None
    content = jsonable(item["content"])
    source_id = digest([code, item["provider"], item["source_key"], item["body"], content, str(item["published_at"])])
    row = dict(id=source_id, code=code, **{**item, "content":content}, fetched_at=now, document_id=None, index_state="未索引")
    with db.transaction() as conn:
        conn.execute(insert(s.monitor_sources).values(**row).on_conflict_do_nothing(index_elements=["id"]))
    return source_id


def enrich_news(db, http, now, limit=10):
    """Bound article work per scheduler tick; retain pending metadata for resumption."""
    with db.transaction(readonly=True) as conn:
        rows = list(conn.execute(select(s.monitor_sources).where(s.monitor_sources.c.provider == "gdelt").order_by(
            s.monitor_sources.c.fetched_at, s.monitor_sources.c.id)).mappings())
    latest = {}
    for row in rows:
        latest[row["code"], row["source_key"]] = row
    pending = [r for r in latest.values() if r["content"].get("body_state") == "本文取得待ち"
               and (not r["content"].get("body_attempted_at") or
                    now - stamp(r["content"]["body_attempted_at"]) >= timedelta(minutes=15))]
    for row in sorted(pending, key=lambda r:(not r["content"].get("matched"), r["fetched_at"]))[:limit]:
        try:
            body, published, state = http.article(row["url"])
        except ScreeningError:
            body, published, state = "", None, "本文取得待ち"
        content = {**row["content"], "body_state":state, "body_attempted_at":now.isoformat()}
        content["matched"] = matches_company(row["title"] + "\n" + body, row["code"], content.get("aliases", []))
        item = {key:row[key] for key in ("provider", "source_key", "title", "url")}
        item.update(body=body, published_at=published, content=content)
        save_source(db, row["code"], item, now + timedelta(microseconds=1))


def collect(db, config, now=None, api=None, providers=None):
    now = (now or datetime.now(JST)).astimezone(JST)
    providers = providers or Providers(config, api)
    with db.transaction(readonly=True) as conn:
        periods = list(conn.execute(select(s.monitor_periods).where(s.monitor_periods.c.active.is_(True))).mappings())
    relevant = [p for p in periods if not p["end_date"] or now.date() <= p["end_date"] + timedelta(days=7)]
    if not relevant:
        return []
    ranges = {}
    for p in relevant:
        start, end = p["start_date"] - timedelta(days=30), p["end_date"] or now.date()
        previous = ranges.get(p["code"])
        ranges[p["code"]] = (min(start, previous[0]), max(end, previous[1])) if previous else (start,end)
    # Persist one master lookup per day; never invent an English company alias.
    master_key = digest(["monitor-master", str(now.date())])
    with db.transaction(readonly=True) as conn:
        cached = conn.execute(select(s.monitor_fetches).where(s.monitor_fetches.c.id == master_key)).mappings().first()
    try:
        master_rows = cached["content"]["rows"] if cached else providers.master(now.date())
        master = {code5(row["Code"]):row for row in master_rows}
        if not cached:
            with db.transaction() as conn:
                upsert(conn, s.monitor_fetches, dict(id=master_key, provider="master", code="00000", state="ready",
                    content={"rows":master_rows}, updated_at=now), ["id"])
    except Exception:
        master = {}
    reports = []
    for code, (first, end) in sorted(ranges.items()):
        company = master.get(code, {})
        aliases = [company.get("CoName", ""), company.get("CoNameEn", "")]
        aliases += [p["content"]["name"] for p in relevant if p["code"] == code]
        for provider in ("jquants", "edinet", "gdelt"):
            cooldown_id = digest(["gdelt-cooldown"])
            if provider == "gdelt":
                with db.transaction(readonly=True) as conn:
                    cooldown = conn.execute(select(s.monitor_fetches).where(s.monitor_fetches.c.id == cooldown_id)).mappings().first()
                if cooldown and now - cooldown["updated_at"].astimezone(JST) < timedelta(hours=1):
                    with db.transaction() as conn:
                        upsert(conn, s.monitor_fetches, dict(id=digest([provider, code]), provider=provider, code=code,
                            state="retry", content={"note":"ニュースAPIの制限・接続待ちです。1時間後に再試行します。"}, updated_at=now), ["id"])
                    reports.append(dict(provider=provider, code=code, state="retry"))
                    continue
            fetch_id = digest([provider, code])
            with db.transaction(readonly=True) as conn:
                old = conn.execute(select(s.monitor_fetches).where(s.monitor_fetches.c.id == fetch_id)).mappings().first()
            if old and old["updated_at"].astimezone(JST).date() == now.date() and (old["state"] in {"ready", "no_data", "partial"}
                    or (old["state"] == "waiting_key" and not config.edinet_api_key)):
                reports.append(dict(provider=provider, code=code, state=old["state"]))
                continue
            if old and old["state"] == "retry" and now - old["updated_at"].astimezone(JST) < timedelta(minutes=15):
                reports.append(dict(provider=provider, code=code, state=old["state"]))
                continue
            start = max(first, date.fromisoformat(old["content"]["covered_to"]) - timedelta(days=7)) if old and old["state"] in {"ready", "no_data", "partial"} else first
            try:
                if provider == "jquants":
                    items = providers.financial(code, start, end)
                elif provider == "edinet":
                    items = providers.edinet(code, start, end, db, now)
                else:
                    items = providers.news(code, aliases, company.get("CoNameEn", ""), start, end, now)
                ids = [save_source(db, code, item, now) for item in items]
                partial = any(item["content"].get("truncated") for item in items)
                state = "partial" if partial else "ready" if items else "no_data"
                content = dict(count=len(ids), covered_from=str(start), covered_to=str(end),
                    note="取得上限に達しました。収集範囲に不足があります。" if partial else "該当情報なし" if not items else "取得済み")
            except Exception as exc:
                if provider == "gdelt" and any(token in str(exc) for token in ("429", "通信", "応答", "公開ホスト")):
                    with db.transaction() as conn:
                        upsert(conn, s.monitor_fetches, dict(id=cooldown_id, provider="gdelt-cooldown", code="00000", state="retry",
                            content={"note":"提供制限・通信待ち"}, updated_at=now), ["id"])
                state = "waiting_key" if provider == "edinet" and not config.edinet_api_key else "retry"
                content = dict(note="準備待ち：EDINETキー未設定" if state == "waiting_key" else
                    "取得未完了：認証・契約範囲・通信・提供状況を次回確認します。", exception_type=type(exc).__name__)
            with db.transaction() as conn:
                upsert(conn, s.monitor_fetches, dict(id=fetch_id, provider=provider, code=code,
                    state=state, content=content, updated_at=now), ["id"])
            reports.append(dict(provider=provider, code=code, state=state))
    if hasattr(providers, "http"):
        enrich_news(db, providers.http, now)
    return reports
