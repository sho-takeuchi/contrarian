"""Opt-in, disposable OS scheduler registration check (no API or real job).

Uses a unique task name and a harmless file-writing command, then removes the
registration even on failure. Windows CI checks registration and argv execution;
an interactive logged-in desktop is still needed for timed InteractiveToken runs.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4

from jp_screening.analysis import schedule


def main():
    if sys.platform not in {"darwin", "win32"}:
        raise RuntimeError("This smoke check requires macOS or Windows.")
    with tempfile.TemporaryDirectory(prefix="contrarian schedule ") as tmp:
        root = Path(tmp)
        marker = root / "executed.txt"
        label = "jp.contrarian.analysis.test-" + uuid4().hex[:12]
        argv = [sys.executable, "-c", "from pathlib import Path; Path(" + repr(str(marker)) + ").write_text('ok', encoding='utf-8')"]
        with patch.object(schedule,"LABEL",label), patch.object(schedule,"command",return_value=argv):
            try:
                schedule.manage(root,"install")
                schedule.manage(root,"status")
                if sys.platform=="darwin":
                    deadline = time.monotonic()+20
                    while not marker.exists() and time.monotonic()<deadline:
                        time.sleep(0.2)
                else:
                    # Service-hosted CI has no interactive desktop session.
                    subprocess.run(argv,check=True,timeout=20,cwd=root)
                assert marker.read_text(encoding="utf-8")=="ok"
                print("Scheduler registration/status and command execution passed on " + sys.platform)
            finally:
                schedule.manage(root,"remove")
        if sys.platform=="darwin":
            assert not (Path.home()/"Library/LaunchAgents"/(label+".plist")).exists()


if __name__=="__main__":
    main()
