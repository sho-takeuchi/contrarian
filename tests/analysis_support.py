"""Synthetic fixtures: no API keys, real securities or production data."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from jp_screening.core import JST, TradingCalendar
from jp_screening.reporting import counts, write_json, write_results
from jp_screening.provenance import enrich_manifest

START = date(2026, 1, 5)
CALENDAR = [dict(Date=str(START + timedelta(days=i)),
    HolDiv="1" if (START + timedelta(days=i)).weekday() < 5 and i != 1 else "0") for i in range(-30, 440)]
CAL = TradingCalendar.from_rows(CALENDAR)
DAYS = CAL.sessions(START, START + timedelta(days=400))
NOW = datetime.combine(DAYS[5], datetime.min.time(), JST).replace(hour=19)


def result(code="10000", flags=("通過", "通過", "通過"), day=START):
    status = next((x for x in flags if x != "通過"), "通過")
    return dict(code=code, name="架空企業", product="株式", asof=str(day), status=status, reason="合成検証",
        stage1_status=flags[0], stage2_status=flags[1], stage3_status=flags[2],
        current_close=Decimal(100), lowest_close=Decimal(80), upper_limit=Decimal(120),
        m1_low=Decimal(90), m2_low=Decimal(91), m3_low=Decimal(92), m4_low=Decimal(85),
        m1_close=Decimal(100), sma3=Decimal(95), w1_close=Decimal(100), sma5_week=Decimal(98),
        previous_sma5_week=Decimal(97), d1_close=Decimal(100), sma5_day=Decimal(102),
        previous_sma5_day=Decimal(103))


def run_folder(root, name="run1", day=START, results=None, scope="all", mode="all", legacy=False, hour=18):
    folder = Path(root) / "outputs" / name
    folder.mkdir(parents=True)
    rows = results if results is not None else [result(day=day)]
    write_results(folder, rows, {}, {}, {})
    manifest = dict(run_id=name, asof=str(day), mode=mode, synthetic=False,
        settings=dict(stage=3, monthly_close_policy="last_trade_in_month"), counts=counts(rows),
        created_at=datetime.combine(day, datetime.min.time(), JST).replace(hour=hour).isoformat(), calendar=CALENDAR)
    if not legacy:
        enrich_manifest(manifest, folder, scope=scope)
    write_json(folder / "run.json", manifest)
    return folder


def quotes(code="10000", days=None):
    days = days or DAYS[:6]
    return [dict(Date=str(day), Code=code, O=100, H=110, L=90, C=100+i, AdjFactor=1) for i, day in enumerate(days)]


class FakeAPI:
    def __init__(self, data=None, published=True):
        self.data = data if data is not None else {"10000": quotes()}
        self.published = published
        self.calls = []

    def rows(self, path):
        return CALENDAR

    def daily(self, code=None, start=None, end=None, day=None):
        self.calls.append((code, start, end, day))
        if day:
            return [dict(Date=str(day), C=100)] if self.published else []
        return self.data.get(code, [])


class FakeLLM:
    config = SimpleNamespace(embed_model="fake:1", chat_model="fake:1")

    def __init__(self, intent='{"kind":"candidates"}'):
        self.intent = intent
        self.messages = []

    def profile(self, model):
        return "fake@version1"

    def embed(self, texts):
        return [[1.0, 0.5, 0.25] for _ in texts]

    def chat(self, messages, schema=None):
        self.messages.append(messages)
        return self.intent if schema else "保存した計算結果と資料に基づく説明です。[D1]"
