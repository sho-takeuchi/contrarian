"""Credential-safe HTTP access with pagination, retries and a sliding-window limiter."""
from collections import deque
from decimal import Decimal
import json
import logging
from pathlib import Path
import time

import requests

from .core import ScreeningError

BASE = "https://api.jquants.com/v2"
LOG = logging.getLogger(__name__)


class Client:
    def __init__(self, key, session=None, clock=time.monotonic, sleep=time.sleep):
        if not key or not key.strip():
            raise ScreeningError("JQUANTS_API_KEY が未設定です。READMEの設定手順を確認してください。")
        self._key = key.strip()
        self.session = session or requests.Session()
        self.clock, self.sleep = clock, sleep
        self.calls = deque()

    def _limit(self):
        while self.calls and self.clock() - self.calls[0] >= 60:
            self.calls.popleft()
        if len(self.calls) >= 90:
            self.sleep(max(0, 60 - (self.clock() - self.calls[0])) + 0.02)
            return self._limit()
        self.calls.append(self.clock())

    def get(self, api_path, /, **params):
        # The Bulk API has an `endpoint` query parameter. Keep the request
        # path positional-only so query names cannot collide with it.
        if not api_path.startswith("/") or ":" in api_path or ".." in api_path:
            raise ScreeningError("不正なAPIパスです。")
        for attempt in range(4):
            self._limit()
            try:
                response = self.session.get(BASE + api_path, params=params,
                    headers={"x-api-key": self._key}, timeout=(10, 90), allow_redirects=False)
            except requests.RequestException:
                if attempt == 3:
                    raise ScreeningError(f"API通信失敗: {api_path}") from None
                self.sleep(2 ** attempt)
                continue
            # Requests preserves custom headers across redirects. Never send
            # the API key to a redirect destination, even on the same origin.
            if 300 <= response.status_code < 400:
                raise ScreeningError("認証付きAPI通信のリダイレクトを拒否しました。")
            if response.status_code in {401, 403}:
                raise ScreeningError("API認証または契約範囲を確認してください（401/403）。")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 3:
                    raise ScreeningError(f"API再試行上限: {api_path} ({response.status_code})")
                wait = 2 ** attempt
                try:
                    wait = max(wait, min(float(response.headers.get("Retry-After", 0)), 300))
                except ValueError:
                    pass
                LOG.warning("API再試行 %s (%s)", api_path, response.status_code)
                self.sleep(wait)
                continue
            if response.status_code != 200:
                raise ScreeningError(f"API取得失敗: {api_path} ({response.status_code})")
            try:
                value = json.loads(response.text, parse_float=Decimal)
            except (ValueError, TypeError):
                raise ScreeningError(f"API応答がJSONではありません: {api_path}") from None
            if not isinstance(value, dict):
                raise ScreeningError("API応答の形式が不正です。")
            return value
        raise AssertionError("unreachable")

    def rows(self, api_path, /, **params):
        seen = set()
        while True:
            page = self.get(api_path, **params)
            rows = page.get("data")
            if not isinstance(rows, list):
                raise ScreeningError(f"API応答にdata配列がありません: {api_path}")
            yield from rows
            cursor = page.get("pagination_key")
            if not cursor:
                break
            if cursor in seen:
                raise ScreeningError("ページングキーの循環を検出しました。")
            seen.add(cursor)
            params["pagination_key"] = cursor

    def daily(self, code=None, start=None, end=None, day=None):
        params = {}
        if code:
            params["code"] = code
        if day:
            params["date"] = str(day)
        else:
            params.update({"from": str(start), "to": str(end)})
        return list(self.rows("/equities/bars/daily", **params))

    def download(self, url, destination: Path):
        # Signed URLs never receive x-api-key and never appear in logs/errors.
        if not url.startswith("https://"):
            raise ScreeningError("ダウンロードURLがHTTPSではありません。")
        tmp = destination.with_suffix(destination.suffix + ".part")
        for attempt in range(4):
            try:
                with requests.get(url, stream=True, timeout=(15, 120)) as response:
                    response.raise_for_status()
                    with tmp.open("wb") as f:
                        for chunk in response.iter_content(1024 * 1024):
                            f.write(chunk)
                tmp.replace(destination)
                return
            except (requests.RequestException, OSError):
                if tmp.exists():
                    tmp.unlink()
                if attempt == 3:
                    raise ScreeningError("一括ファイルのダウンロードに失敗しました。再実行してください。") from None
                self.sleep(2 ** attempt)

