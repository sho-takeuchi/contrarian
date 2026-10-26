"""Optional post-screening hook. The screener remains usable without analysis extras."""
def optional_import(root, folder):
    try:
        import os
        from dotenv import dotenv_values
        enabled = os.environ.get("ANALYSIS_AUTO_IMPORT", dotenv_values(root / ".env").get("ANALYSIS_AUTO_IMPORT", "false"))
        if str(enabled).lower() != "true" or not (folder / "run.json").is_file():
            return True
        from .config import Config
        config = Config.load(root)
    except ModuleNotFoundError:
        return True
    except Exception:
        return False
    try:
        from .db import Database
        from .ingest import import_run
        import_run(Database(config.database_url), folder)
        return True
    except Exception:
        return False
