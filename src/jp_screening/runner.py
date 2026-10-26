"""Live orchestration and self-contained, network-free snapshot replay."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta
from hashlib import sha256
import csv
import gzip
import json
import logging
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .api import Client
from .core import (JST, MONTHLY_CLOSE_POLICY, Bar, D, ScreeningError, TradingCalendar, adjusted_bars, api_bars,
                   code5, evaluate, monthly_bars, shift_month, years_before)
from .listings import load_or_prepare
from .reporting import SafeCsvWriter, counts, fingerprint, write_json, write_results
from .storage import Store, json_text
from .verification import compare_prices, reference_check
from .weekly import WEEKLY_SETTINGS, evaluate_weekly, reference_check_weekly
from .daily import DAILY_SETTINGS, evaluate_daily, reference_check_daily
from .provenance import enrich_manifest

LOG = logging.getLogger(__name__)


def initialize(root):
    for name in ("inputs", "data", "data/downloads", "data/snapshots", "logs", "outputs"):
        (root / name).mkdir(parents=True, exist_ok=True)


@contextmanager
def run_lock(root):
    """Single writer; OS releases the lock after a crash, no stale PID deletion."""
    handle = (root / "data" / "run.lock").open("a+b")
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ScreeningError("別のスクリーニングが実行中です。終了後に再実行してください。") from None
        yield
    finally:
        handle.close()


def eligible(meta):
    code = code5(meta["Code"])
    stock = str(meta.get("ProdCat")) == "011" and str(meta.get("Mkt")) in {"0111", "0112", "0113"} and code.endswith("0")
    reit = str(meta.get("ProdCat")) == "013"
    return stock or reit


def choose_sample(metas, listings, asof, splits, n=10):
    valid = [m for m in sorted(metas, key=lambda x: x["Code"])
             if listings.get(m["Code"]) and listings[m["Code"]] <= years_before(asof, 8)]
    selected = []
    def add(predicate, limit):
        for m in valid:
            if m not in selected and predicate(m):
                selected.append(m)
                limit -= 1
                if limit == 0 or len(selected) == n:
                    return
    # Reserve split coverage first, then each product/age group.
    add(lambda m: m["Code"] in splits, 1)
    for product in ("011", "013"):
        for older in (False, True):
            if len(selected) < n:
                add(lambda m: m.get("ProdCat") == product and
                    (listings[m["Code"]] <= years_before(asof, 10)) == older, 2)
    if len(selected) < n:
        add(lambda m: True, n-len(selected))
    return sorted(selected, key=lambda m: m["Code"])


def context(client, now):
    # Get full entitled calendar, including future month-end and oldest-month suffix.
    calendar_rows = list(client.rows("/markets/calendar"))
    cal = TradingCalendar.from_rows(calendar_rows)
    asof = cal.target(now)
    published = client.daily(day=asof)
    if not published or any(r.get("Date") != asof.isoformat() for r in published):
        raise ScreeningError(f"更新待ち: {asof} の日足が公開されていません。")
    if not any(r.get("C") is not None for r in published):
        raise ScreeningError(f"更新待ち・全市場無約定: {asof} の有効終値がありません。")
    meta = list(client.rows("/equities/master", date=asof.isoformat()))
    if not meta:
        raise ScreeningError("判定対象日の銘柄一覧が取得できません。")
    for row in meta:
        row["Code"] = code5(row["Code"])
        row["ProdCat"] = str(row.get("ProdCat", ""))
    return cal, calendar_rows, asof, published, meta


def sync_bulk(client, store, root, start, asof):
    files = list(client.rows("/bulk/list", endpoint="/equities/bars/daily",
                            **{"from": str(start), "to": str(asof)}))
    if not files:
        raise ScreeningError("契約範囲内の株価一括ファイルがありません。")
    for index, item in enumerate(sorted(files, key=lambda x: x["Key"]), 1):
        if not all(k in item for k in ("Key", "LastModified", "Size")):
            raise ScreeningError("一括ファイル一覧の形式が不正です。")
        if store.file_current(item):
            continue
        name = sha256((item["Key"] + str(item["LastModified"])).encode()).hexdigest()[:24] + ".csv.gz"
        path = root / "data" / "downloads" / name
        LOG.info("株価ファイル取得 %s/%s", index, len(files))
        # A completed download left by an interrupted import can be reused.
        if not path.exists() or path.stat().st_size != int(item["Size"]):
            link = client.get("/bulk/get", key=item["Key"])
            if not isinstance(link.get("url"), str):
                raise ScreeningError("一括ダウンロードURLがありません。")
            client.download(link["url"], path)
        store.import_gzip(item, path)
    return files


def repair_history(client, store, code, start, asof, cal, listing):
    rows = store.prices(code, start, asof)
    expected = cal.sessions(max(start, listing), asof)
    actual = {r["Date"] for r in rows}
    missing = [day for day in expected if day.isoformat() not in actual]
    if missing:
        LOG.info("取得漏れ再取得: %s (%s営業日)", code, len(missing))
        extra = client.daily(code=code, start=missing[0], end=missing[-1])
        if extra:
            store.replace_source(f"repair/{code}/{asof}", extra)
            rows = store.prices(code, start, asof)
        actual = {r["Date"] for r in rows}
        if any(day.isoformat() not in actual for day in expected):
            raise ScreeningError("日足の取得漏れが再取得後も残っています。")
    return rows


def validate_sample(client, store, sample, listings, cal, asof, start):
    validation = []
    api_by_code = {}
    for i, meta in enumerate(sample, 1):
        code = meta["Code"]
        LOG.info("少数照合 %s/%s: %s", i, len(sample), code)
        rows = client.daily(code=code, start=start, end=asof)
        store.save_api(code, rows)
        raw = repair_history(client, store, code, start, asof, cal, listings[code])
        a, b = api_bars(rows, asof), adjusted_bars(raw, asof)
        diffs = compare_prices(a, b)
        if not a:
            diffs.append("API日足が空")
        result, months = evaluate(meta, listings[code], a, cal, asof, start)
        ref_errors = reference_check(result, months, a, listings[code], asof, cal)
        validation.append(dict(code=code, price_rows=len(a), price_match=not diffs,
                               reference_match=not ref_errors, differences=diffs[:30], reference_errors=ref_errors))
        api_by_code[code] = rows
    return validation, api_by_code


def failure(meta, listing, asof, message):
    return dict(code=meta["Code"], name=meta.get("CoName", ""),
                product="REIT" if meta.get("ProdCat") == "013" else "株式",
                listing_date=str(listing or ""), asof=str(asof), status="判定不能", reason=message,
                condition_a=None, condition_b=None, condition_c=None)


def write_snapshot_line(stream, meta, listing, rows, kind, failure_message=None):
    stream.write(json_text(dict(meta=meta, listing=str(listing) if listing else None,
                               rows=rows, kind=kind, failure_message=failure_message)) + "\n")


def run_live(root, mode, key, codes=None, listing_file=None, listing_url=None, stage=1):
    if stage not in {1,2,3}:
        raise ScreeningError("実装済みの段階は1・2・3です。")
    now = datetime.now(JST)
    run_id = now.strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:8]
    folder = root / "outputs" / run_id
    folder.mkdir()
    handler = logging.FileHandler(root / "logs" / f"{run_id}.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    store = Store(root / "data" / "prices.sqlite3")
    try:
        client = Client(key)
        listings = load_or_prepare(root / "inputs", listing_file, listing_url)
        cal, calendar_rows, asof, published, metas = context(client, now)
        start = years_before(now.date(), 10)
        metas = sorted((m for m in metas if eligible(m)), key=lambda m: m["Code"])
        by_code = {m["Code"]: m for m in metas}
        if codes:
            requested = sorted({code5(c) for c in codes})
            invalid = [c for c in requested if c not in by_code]
            if invalid:
                raise ScreeningError("対象外または未上場の指定コード: " + ",".join(invalid))
            selected = [by_code[c] for c in requested]
        else:
            selected = metas
        files = sync_bulk(client, store, root, start, asof)
        # Current date endpoint is the publication check and latest-day supplement.
        store.replace_source(f"daily/{asof}", published)
        sample = choose_sample(selected, listings, asof, store.split_codes())
        if mode == "sample" and codes:
            sample = [m for m in selected if listings.get(m["Code"]) and listings[m["Code"]] <= years_before(asof, 8)]
        if not sample:
            raise ScreeningError("上場日を確認できる上場8年以上の検証銘柄がありません。上場日CSVを確認してください。")
        validation, api_rows = validate_sample(client, store, sample, listings, cal, asof, start)
        write_json(folder / "validation.json", validation)
        with (folder / "validation_codes.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = SafeCsvWriter(f)
            writer.writerow(["銘柄コード", "銘柄名", "商品区分", "東証上場日"])
            writer.writerows((m["Code"], m.get("CoName", ""), m["ProdCat"], listings[m["Code"]]) for m in sample)
        if any(not v["price_match"] or not v["reference_match"] for v in validation):
            raise ScreeningError("少数照合が不一致です。validation.jsonを確認してください。全銘柄判定は実行していません。")
        if mode == "sample":
            selected = selected if codes else sample
        snapshot = root / "data" / "snapshots" / f"{run_id}.jsonl.gz"
        results, audits, weekly_audits, daily_audits = [], {}, {}, {}
        with gzip.open(snapshot, "wt", encoding="utf-8", newline="\n") as stream:
            for index, meta in enumerate(selected, 1):
                code, listing = meta["Code"], listings.get(meta["Code"])
                rows, kind, error, bars = [], "raw", None, []
                try:
                    if listing and listing <= years_before(asof, 8):
                        if mode == "sample":
                            rows, kind = api_rows[code], "api"
                            bars = api_bars(rows, asof)
                        else:
                            rows = repair_history(client, store, code, start, asof, cal, listing)
                            bars = adjusted_bars(rows, asof)
                    else:
                        bars = []
                    result, months = evaluate(meta, listing, bars, cal, asof, start)
                except ScreeningError as exc:
                    error = str(exc)
                    result, months = failure(meta, listing, asof, error), {}
                if stage >= 2:
                    result, weeks = evaluate_weekly(result,bars,cal,asof)
                    if reference_check_weekly(result,weeks):
                        raise ScreeningError(f"週足判定の別計算照合に不一致: {code}")
                    weekly_audits[code] = weeks
                if stage == 3:
                    result, days = evaluate_daily(result,bars,cal,asof)
                    if reference_check_daily(result,days):
                        raise ScreeningError(f"日足判定の別計算照合に不一致: {code}")
                    daily_audits[code] = days
                results.append(result)
                audits[code] = months
                write_snapshot_line(stream, meta, listing, rows, kind, error)
                if index % 50 == 0:
                    LOG.info("月足判定 %s/%s", index, len(selected))
        # Independently check at most five passed and five failed stocks.
        checks = []
        for status in ("通過", "不通過"):
            for result in [r for r in results if r["status"] == status][:5]:
                errors = reference_check(result, audits[result["code"]],
                    [Bar(asof, result["code"], result["current_close"], None, None)],
                    listings[result["code"]], asof, cal)
                checks.append(dict(code=result["code"], status=status, reference_errors=errors))
        write_json(folder / "spot_checks.json", checks)
        if any(c["reference_errors"] for c in checks):
            raise ScreeningError("全銘柄実行後の別計算照合に不一致があります。")
        write_results(folder, results, audits, weekly=weekly_audits if stage >= 2 else None,
                      daily=daily_audits if stage == 3 else None)
        manifest = dict(run_id=run_id, mode=mode, synthetic=False, asof=str(asof), start=str(start),
            calendar=calendar_rows, snapshot=str(snapshot), snapshot_sha256=file_hash(snapshot),
            result_sha256=fingerprint(results), counts=counts(results), created_at=now.isoformat(),
            files=store.file_manifest(), validation=validation, spot_checks=checks,
            settings=dict(stage=stage, years=10, min_listing_years=8, base_rate="0.2", yen_floor="100", cap="0.5",
                          monthly_close_policy=MONTHLY_CLOSE_POLICY),
            listing_source=json.loads((root / "inputs" / "listing_dates.source.json").read_text(encoding="utf-8")))
        if stage >= 2:
            manifest["settings"]["weekly"] = WEEKLY_SETTINGS
            manifest["weekly_reference_checks"] = {"records":sum(r["stage1_status"]=="通過" for r in results),"errors":0}
        if stage == 3:
            manifest["settings"]["daily"] = DAILY_SETTINGS
            manifest["daily_reference_checks"] = {"records":sum(r["stage2_status"]=="通過" for r in results),"errors":0}
        enrich_manifest(manifest, folder, "sample" if mode == "sample" else "specified" if codes else "all")
        write_json(folder / "run.json", manifest)
        store.save_run(run_id, manifest)
        return folder, manifest
    except ScreeningError as exc:
        write_json(folder / "error.json", dict(status="実行未完了", reason=str(exc), created_at=now.isoformat()))
        raise
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        message = f"入力・キャッシュ・保存データの処理に失敗しました ({type(exc).__name__})。"
        write_json(folder / "error.json", dict(status="実行未完了", reason=message, created_at=now.isoformat()))
        raise ScreeningError(message) from None
    finally:
        store.close()
        logging.getLogger().removeHandler(handler)
        handler.close()


def file_hash(path):
    digest = sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def replay(root, run_id, *, current_rules=False, stage=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ScreeningError("実行IDの形式が不正です。")
    manifest = json.loads((root / "outputs" / run_id / "run.json").read_text(encoding="utf-8"))
    snapshot = Path(manifest["snapshot"]).resolve()
    if not snapshot.is_relative_to((root / "data" / "snapshots").resolve()):
        raise ScreeningError("スナップショットがプロジェクト外を参照しています。")
    if file_hash(snapshot) != manifest["snapshot_sha256"]:
        raise ScreeningError("保存データのハッシュが一致しません。")
    cal = TradingCalendar.from_rows(manifest["calendar"])
    asof, start = date.fromisoformat(manifest["asof"]), date.fromisoformat(manifest["start"])
    saved_policy = manifest.get("settings", {}).get("monthly_close_policy", "month_end")
    close_policy = MONTHLY_CLOSE_POLICY if current_rules else saved_policy
    stage = manifest.get("settings", {}).get("stage",1) if stage is None else stage
    if stage not in {1,2,3}:
        raise ScreeningError("実装済みの段階は1・2・3です。")
    saved_weekly = manifest.get("settings", {}).get("weekly", {})
    require_breakout = False if current_rules else saved_weekly.get("require_breakout", True)
    results, audits, weekly_audits, daily_audits = [], {}, {}, {}
    with gzip.open(snapshot, "rt", encoding="utf-8") as stream:
        for index, line in enumerate(stream, 1):
            item = json.loads(line)
            listing = date.fromisoformat(item["listing"]) if item["listing"] else None
            meta, rows = item["meta"], item["rows"]
            bars = []
            if item.get("failure_message"):
                result, months = failure(meta, listing, asof, item["failure_message"]), {}
            else:
                bars = api_bars(rows, asof) if item["kind"] == "api" else adjusted_bars(rows, asof)
                result, months = evaluate(meta, listing, bars, cal, asof, start, close_policy=close_policy)
                errors = reference_check(result, months, bars, listing, asof, cal)
                if errors:
                    raise ScreeningError(f"保存データ再計算の別計算照合に不一致: {meta['Code']}")
            if stage >= 2:
                result, weeks = evaluate_weekly(result,bars,cal,asof,require_breakout=require_breakout)
                if reference_check_weekly(result,weeks,require_breakout=require_breakout):
                    raise ScreeningError(f"保存データ週足再計算の別計算照合に不一致: {meta['Code']}")
                weekly_audits[meta["Code"]] = weeks
            if stage == 3:
                result, days = evaluate_daily(result,bars,cal,asof)
                if reference_check_daily(result,days):
                    raise ScreeningError(f"保存データ日足再計算の別計算照合に不一致: {meta['Code']}")
                daily_audits[meta["Code"]] = days
            results.append(result)
            audits[meta["Code"]] = months
            if index % 100 == 0:
                LOG.info("保存データ再計算 %s/%s", index, manifest["counts"]["total"])
    if not current_rules and fingerprint(results) != manifest["result_sha256"]:
        raise ScreeningError("再計算結果が元の結果と一致しません。")
    suffix = ("_DEMO" if manifest.get("synthetic") else "") + (
        f"_stage{stage}" if current_rules and stage >= 2 else "_recompute" if current_rules else "_replay")
    folder = root / "outputs" / (datetime.now(JST).strftime("%Y%m%d_%H%M%S_%f") + suffix)
    folder.mkdir()
    write_results(folder, results, audits, weekly=weekly_audits if stage >= 2 else None,
                      daily=daily_audits if stage == 3 else None)
    run_info = dict(original=run_id, identical=fingerprint(results) == manifest["result_sha256"],
        current_rules=current_rules, stage=stage, monthly_close_policy=close_policy,
        synthetic=manifest.get("synthetic", False), counts=counts(results), result_sha256=fingerprint(results))
    write_json(folder / ("recompute.json" if current_rules else "replay.json"), run_info)
    if current_rules:
        new_manifest = {**manifest, "run_id":folder.name, "mode":"recompute", "original_run_id":run_id,
            "created_at":datetime.now(JST).isoformat(), "counts":counts(results),
            "result_sha256":fingerprint(results),
            "settings":{**manifest.get("settings", {}), "stage":stage,"monthly_close_policy":close_policy}}
        if stage >= 2:
            new_manifest["settings"]["weekly"] = WEEKLY_SETTINGS
            new_manifest["weekly_reference_checks"] = {"records":sum(r["stage1_status"]=="通過" for r in results),"errors":0}
        else:
            new_manifest["settings"].pop("weekly",None)
            new_manifest.pop("weekly_reference_checks",None)
        if stage == 3:
            new_manifest["settings"]["daily"] = DAILY_SETTINGS
            new_manifest["daily_reference_checks"] = {"records":sum(r["stage2_status"]=="通過" for r in results),"errors":0}
        else:
            new_manifest["settings"].pop("daily",None)
            new_manifest.pop("daily_reference_checks",None)
        # Validation of raw prices remains applicable; evaluate all records again
        # above with the current aggregation rule instead of claiming an old check.
        new_manifest["source_validation"] = new_manifest.pop("validation", new_manifest.get("source_validation", []))
        new_manifest["source_spot_checks"] = new_manifest.pop("spot_checks", new_manifest.get("source_spot_checks", []))
        new_manifest["reference_checks"] = {"records":len(results), "errors":0}
        enrich_manifest(new_manifest, folder)
        write_json(folder / "run.json", new_manifest)
        previous_statuses = {}
        with (root / "outputs" / run_id / "all_results.csv").open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                previous_statuses[row["銘柄コード"]] = row["判定"]
        transitions = {}
        for result in results:
            change = previous_statuses.get(result["code"], "不明") + " → " + result["status"]
            transitions[change] = transitions.get(change, 0) + 1
        write_json(folder / "change_summary.json", {"before":manifest["counts"], "after":counts(results),
            "previous_policy":saved_policy, "current_policy":close_policy,
            "previous_weekly_settings":manifest.get("settings", {}).get("weekly"),
            "current_weekly_settings":WEEKLY_SETTINGS if stage >= 2 else None,
            "current_daily_settings":DAILY_SETTINGS if stage == 3 else None, "transitions":transitions})
    if manifest.get("synthetic"):
        (folder / "SYNTHETIC_DATA.txt").write_text(manifest["warning"], encoding="utf-8")
    return folder, counts(results)

