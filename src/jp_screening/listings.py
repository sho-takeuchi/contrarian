"""JPX official CSV import with explicit provenance; never infer an IPO date from prices."""
import csv
from datetime import date, datetime
from hashlib import sha256
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse

import requests

from .core import JST, ScreeningError, code5

PORTAL = "https://clientportal.jpx.co.jp/ClientPortal/s/Issue?language=ja"


def decode_csv(raw):
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ScreeningError("CSVの文字コードを判定できません（UTF-8/CP932対応）。")


def parse_date(text):
    text = str(text).strip()
    if not text or text in {"-", "―"}:
        return None
    text = re.sub(r"[年月/.]", "-", text).replace("日", "").strip("-")
    try:
        if re.fullmatch(r"\d{8}", text):
            return datetime.strptime(text, "%Y%m%d").date()
        parts = [int(x) for x in text.split("-")]
        return date(*parts)
    except (ValueError, TypeError):
        raise ScreeningError("上場日は西暦の年月日で指定してください。") from None


def read_listings(path):
    reader = csv.DictReader(io.StringIO(decode_csv(Path(path).read_bytes())))
    names = [n.strip() for n in (reader.fieldnames or [])]
    code_col = next((n for n in names if n in {"Code", "code", "コード", "銘柄コード", "証券コード"}), None)
    day_col = next((n for n in names if n in {"listing_date", "ListingDate", "東証上場日", "東証上場年月日", "上場日", "上場年月日"}), None)
    if not code_col or not day_col:
        raise ScreeningError("上場日CSVにコード列と東証上場日列が必要です。READMEの列名を確認してください。")
    reader.fieldnames = names
    result = {}
    for row in reader:
        if not row.get(code_col, "").strip():
            continue
        code = code5(row[code_col])
        day = parse_date(row.get(day_col, ""))
        if code in result and result[code] != day:
            raise ScreeningError(f"上場日CSVに異なる上場日の重複があります: {code}")
        result[code] = day
    return result


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if ".csv" in href.lower():
                self.links.append(href)


def official_url(url):
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname == "jpx.co.jp" or (parsed.hostname or "").endswith(".jpx.co.jp"))


def prepare_listings(inputs: Path, source_file=None, url=None):
    target = inputs / "listing_dates.csv"
    provenance = inputs / "listing_dates.source.json"
    if source_file:
        path = Path(source_file).resolve()
        data = path.read_bytes()
        source = str(path)
    else:
        try:
            if not url:
                response = requests.get(PORTAL, timeout=30)
                response.raise_for_status()
                parser = Links()
                parser.feed(response.text)
                links = [urljoin(PORTAL, h) for h in parser.links if official_url(urljoin(PORTAL, h))]
                if len(links) != 1:
                    raise ScreeningError("JPXポータルの全件CSVを自動取得できません。inputs/listing_dates.csv に保存するか --listing-file で指定してください。")
                url = links[0]
            if not official_url(url):
                raise ScreeningError("上場日CSVの自動取得元はHTTPSのJPX公式サイトに限定しています。")
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            if not official_url(response.url):
                raise ScreeningError("上場日CSVがJPX外へリダイレクトされました。")
            data, source = response.content, url
        except requests.RequestException:
            raise ScreeningError("JPX上場日CSVの取得に失敗しました。公式CSVを手動保存し --listing-file で指定してください。") from None
    # Validate before replacing a previous good file.
    tmp = inputs / "listing_dates.csv.part"
    tmp.write_bytes(data)
    try:
        listings = read_listings(tmp)
        if not listings:
            raise ScreeningError("上場日CSVが空です。")
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink()
    provenance.write_text(json.dumps(dict(source=source, fetched_at=datetime.now(JST).isoformat(),
        sha256=sha256(data).hexdigest(), entries=len(listings)), ensure_ascii=False, indent=2), encoding="utf-8")
    return listings


def load_or_prepare(inputs, source_file=None, url=None):
    target = inputs / "listing_dates.csv"
    if source_file or url or not target.exists():
        return prepare_listings(inputs, source_file, url)
    provenance = inputs / "listing_dates.source.json"
    if provenance.exists():
        try:
            saved = json.loads(provenance.read_text(encoding="utf-8"))
            if saved.get("sha256") == sha256(target.read_bytes()).hexdigest():
                return read_listings(target)
        except (ValueError, OSError):
            pass
    # A manually dropped file is imported too, so that source/time/hash are recorded.
    return prepare_listings(inputs, target)
