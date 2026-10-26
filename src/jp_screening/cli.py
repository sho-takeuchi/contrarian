import argparse
from datetime import datetime
import logging
import os
from pathlib import Path
import sys

from .core import ScreeningError
from .listings import prepare_listings
from .runner import initialize, replay, run_live, run_lock


def main(argv=None):
    parser = argparse.ArgumentParser(description="日本株・REIT 月足・週足・日足スクリーニング")
    parser.add_argument("command", choices=("sample", "all", "weekly", "daily", "replay", "recompute", "prepare-listings", "demo"))
    parser.add_argument("--stage",type=int,choices=(1,2,3),help="判定段階。sample/allは省略時1。2は週足まで、3は日足まで判定")
    parser.add_argument("--codes", nargs="+", help="銘柄コード。スペースまたはカンマ区切り")
    parser.add_argument("--listing-file", type=Path, help="JPXの上場日入りCSV")
    parser.add_argument("--listing-url", help="JPX公式CSVのHTTPS URL")
    parser.add_argument("--run-id", help="weekly/daily/replay/recomputeで使用する保存済み実行ID")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2], help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        # Fail before any output directory or file is created if credentials are missing.
        if args.command in {"sample", "all"} and not os.getenv("JQUANTS_API_KEY", "").strip():
            raise ScreeningError("JQUANTS_API_KEY が未設定です。READMEの手順で設定したPowerShellから実行してください。")
        initialize(root)
        with run_lock(root):
            if args.command == "prepare-listings":
                data = prepare_listings(root / "inputs", args.listing_file, args.listing_url)
                print(f"上場日を保存しました: {len(data)}件")
                return 0
            if args.command in {"weekly", "daily", "replay", "recompute"}:
                if not args.run_id:
                    parser.error("weekly/daily/replay/recomputeには --run-id が必要です")
                if args.command == "replay" and args.stage is not None:
                    parser.error("replayは保存時の段階を再現します。段階変更はweekly/daily/recomputeで指定してください")
                if args.command == "weekly" and args.stage not in (None,2):
                    parser.error("weeklyは第2段階まで実行します")
                if args.command == "daily" and args.stage not in (None,3):
                    parser.error("dailyは第3段階まで実行します")
                folder, summary = replay(root, args.run_id, current_rules=args.command != "replay",
                    stage=2 if args.command == "weekly" else 3 if args.command == "daily" else args.stage)
            elif args.command == "demo":
                from .demo import run_demo
                folder, manifest = run_demo(root)
                summary = manifest["counts"]
                print("合成データによる動作確認です。実際の銘柄・投資候補ではありません。")
            else:
                codes = [c for arg in (args.codes or []) for c in arg.split(",") if c]
                folder, manifest = run_live(root, args.command, os.environ["JQUANTS_API_KEY"],
                                           codes, args.listing_file, args.listing_url, stage=args.stage or 1)
                summary = manifest["counts"]
        print("結果:", folder)
        print("件数:", summary)
        from .analysis.integration import optional_import
        if not optional_import(root, folder):
            print("スクリーニング完了・DB取り込み失敗。analysis import-runs で再試行できます。", file=sys.stderr)
            return 3
        return 0
    except ScreeningError as exc:
        print("確認が必要です:", str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        # Do not dump response bodies, signed URLs, headers or local secret values.
        print(f"入力・保存データを確認してください ({type(exc).__name__})。READMEの手順を参照してください。", file=sys.stderr)
        return 2

