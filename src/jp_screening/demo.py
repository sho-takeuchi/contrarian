"""Clearly labelled synthetic fixtures exercising the complete offline pipeline."""
from datetime import date, datetime, timedelta
import gzip

from .core import JST, MONTHLY_CLOSE_POLICY, D, TradingCalendar, adjusted_bars, evaluate, shift_month
from .reporting import counts, fingerprint, write_json, write_results
from .runner import file_hash, write_snapshot_line


def fixture_data():
    # Weekdays are intentional synthetic sessions, not the real JPX holiday calendar.
    start, end, asof = date(2016, 10, 1), date(2026, 10, 31), date(2026, 10, 21)
    calendar_rows, day = [], start
    while day <= end:
        calendar_rows.append(dict(Date=str(day), HolDiv="1" if day.weekday() < 5 else "0"))
        day += timedelta(days=1)
    cal = TradingCalendar.from_rows(calendar_rows)
    recent = ["2026-06", "2026-07", "2026-08", "2026-09"]
    datasets = []
    for code, prod, label in (("10000", "011", "合成_通過"), ("20000", "011", "合成_上限超過"),
                               ("30000", "013", "合成_安値割れ"), ("40000", "011", "合成_終値欠損")):
        rows = []
        for day in cal.sessions(start, asof):
            key = day.strftime("%Y-%m")
            c, lo = D(140), D(120)
            if key == "2020-01":
                c, lo = D(100), D(90)
            if key in recent:
                i = recent.index(key)
                c = [D(110), D(115), D(120), D(130)][i]
                lo = [D(100), D(110), D(105), D(115)][i]
            if key == "2026-10":
                c, lo = D(130), D(125)
            if day == asof and code == "20000":
                c = D(151)
            if key == "2026-08" and code == "30000":
                lo = D(99)
            values = dict(O=str(c), H=str(c+10), L=str(lo), C=str(c))
            if day == asof and code == "40000":
                values = dict(O=None, H=None, L=None, C=None)
            rows.append(dict(Date=str(day), Code=code, AdjFactor="1", **values))
        meta = dict(Code=code, CoName=label, ProdCat=prod, Mkt="0111")
        datasets.append((meta, date(2010, 1, 4), rows))
    return cal, calendar_rows, asof, start, datasets


def run_demo(root):
    cal, calendar_rows, asof, start, datasets = fixture_data()
    run_id = datetime.now(JST).strftime("%Y%m%d_%H%M%S_%f") + "_DEMO"
    folder = root / "outputs" / run_id
    folder.mkdir()
    snapshot = root / "data" / "snapshots" / f"{run_id}.jsonl.gz"
    results, audits = [], {}
    with gzip.open(snapshot, "wt", encoding="utf-8", newline="\n") as f:
        for meta, listing, rows in datasets:
            result, months = evaluate(meta, listing, adjusted_bars(rows, asof), cal, asof, start)
            results.append(result)
            audits[meta["Code"]] = months
            write_snapshot_line(f, meta, listing, rows, "raw")
    write_results(folder, results, audits)
    manifest = dict(run_id=run_id, mode="demo", synthetic=True,
        warning="合成データです。実際の銘柄や投資候補ではありません。休日も合成カレンダーです。",
        asof=str(asof), start=str(start), calendar=calendar_rows, snapshot=str(snapshot),
        snapshot_sha256=file_hash(snapshot), result_sha256=fingerprint(results), counts=counts(results),
        settings={"monthly_close_policy":MONTHLY_CLOSE_POLICY})
    write_json(folder / "run.json", manifest)
    (folder / "SYNTHETIC_DATA.txt").write_text(manifest["warning"], encoding="utf-8")
    return folder, manifest
