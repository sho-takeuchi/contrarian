import argparse
from datetime import date
import json
from pathlib import Path
import subprocess
import sys

from ..core import ScreeningError


def main(argv=None):
    parser = argparse.ArgumentParser(description="contrarian ローカル分析・追跡")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    sub = parser.add_subparsers(dest="command", required=True)
    db_parser = sub.add_parser("db")
    db_parser.add_argument("action", choices=["upgrade"])
    for name in ("import-runs", "index-docs"):
        p = sub.add_parser(name)
        p.add_argument("--from", dest="source", type=Path, required=True)
    p = sub.add_parser("daily-update")
    p.add_argument("--if-due", action="store_true")
    p = sub.add_parser("review-legacy-run")
    p.add_argument("run_id")
    p.add_argument("--rules-version", required=True)
    p.add_argument("--note", required=True)
    sub.add_parser("update-prices")
    sub.add_parser("evaluate-outcomes")
    p = sub.add_parser("analyze-conditions")
    p.add_argument("--feature", default="daily_ma_gap")
    p.add_argument("--horizon", type=int, choices=[5,20,60,120,250], default=20)
    p.add_argument("--basis", choices=["close","next_open"], default="close")
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--rules-key")
    p = sub.add_parser("schedule")
    p.add_argument("action", choices=["install","status","remove"])
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("ui")
    sub.add_parser("doctor")
    args = parser.parse_args(argv)
    try:
        from .config import Config
        config = Config.load(args.root)
        if args.command == "ui":
            return subprocess.call([sys.executable, "-m", "streamlit", "run", str(Path(__file__).with_name("ui.py")),
                "--server.address", "127.0.0.1", "--browser.gatherUsageStats", "false", "--", "--root", str(config.root)])
        if args.command == "schedule":
            from .schedule import manage
            print(manage(config.root, args.action, args.dry_run))
            return 0
        from .db import Database
        db = Database(config.database_url)
        result = None
        exit_code = 0
        if args.command == "db":
            db.upgrade()
            result = {"database":"ready", "pgvector":db.ping()}
        elif args.command == "doctor":
            from .rag import Ollama
            result = {}
            for name, check in (("database",db.ping),
                                ("chat_model",lambda:Ollama(config).profile(config.chat_model)),
                                ("embedding_model",lambda:Ollama(config).profile(config.embed_model))):
                try:
                    result[name] = check()
                except ScreeningError as exc:
                    result[name] = str(exc)
                    exit_code = 2
        elif args.command == "import-runs":
            from .ingest import import_runs
            result = import_runs(db, args.source)
            exit_code = 2 if any(r["status"] == "failed" for r in result) else 0
        elif args.command == "index-docs":
            from .rag import Ollama, index_document
            paths = [args.source] if args.source.is_file() else sorted(p for p in args.source.rglob("*")
                        if p.suffix.lower() in {".md", ".txt", ".docx"} and p.is_file())
            result = [index_document(db, Ollama(config), p) for p in paths]
        elif args.command == "review-legacy-run":
            from .ingest import review_legacy_run
            result = review_legacy_run(db, args.run_id, args.rules_version, args.note)
        elif args.command == "daily-update":
            from .pipeline import daily_update
            result = daily_update(config, db, args.if_due)
            exit_code = 0 if result.get("complete") else 2
        elif args.command == "update-prices":
            from .tracking import update_prices
            result = update_prices(db, config.api_key)
            exit_code = 0 if result["complete"] else 2
        elif args.command == "evaluate-outcomes":
            from .tracking import evaluate_outcomes
            result = {"measurements":evaluate_outcomes(db)}
        elif args.command == "analyze-conditions":
            from .analytics import condition_report
            result = condition_report(db, args.feature, args.horizon, args.basis, args.start, args.end,
                                      args.rules_key, output_root=config.root / "outputs" / "analysis_reports")
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        return exit_code
    except ModuleNotFoundError:
        print('分析用依存をインストールしてください: python -m pip install -e ".[analysis]"', file=sys.stderr)
        return 2
    except ScreeningError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"分析処理に失敗しました ({type(exc).__name__})。設定・入力を確認してください。", file=sys.stderr)
        return 2
