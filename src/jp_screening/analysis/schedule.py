from datetime import datetime
import getpass
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from xml.sax.saxutils import escape

from ..core import ScreeningError

LABEL = "jp.contrarian.analysis"


def command(root):
    return [sys.executable, "-m", "jp_screening.analysis", "--root", str(root), "daily-update", "--if-due"]


def mac_definition(root):
    return dict(Label=LABEL, ProgramArguments=command(root), WorkingDirectory=str(root),
                StartInterval=900, RunAtLoad=True, ProcessType="Background",
                StandardOutPath=str(root / "logs" / "analysis-schedule.log"),
                StandardErrorPath=str(root / "logs" / "analysis-schedule-error.log"))


def windows_definition(root):
    args = command(root)
    arguments = subprocess.list2cmdline(args[1:])
    user = getpass.getuser()
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger>
  <TimeTrigger><Repetition><Interval>PT15M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
  <StartBoundary>{datetime.now().replace(microsecond=0).isoformat()}</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
  <Principals><Principal id="User"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
  <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT0S</ExecutionTimeLimit></Settings>
  <Actions Context="User"><Exec><Command>{escape(args[0])}</Command><Arguments>{escape(arguments)}</Arguments>
  <WorkingDirectory>{escape(str(root))}</WorkingDirectory></Exec></Actions>
</Task>'''


def manage(root, action, dry_run=False):
    root = Path(root).resolve()
    if sys.platform == "darwin":
        definition = mac_definition(root)
        path = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
        if dry_run:
            return plistlib.dumps(definition).decode()
        domain = f"gui/{os.getuid()}"
        if action == "install":
            (root / "logs").mkdir(exist_ok=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
            path.write_bytes(plistlib.dumps(definition))
            cmd = ["launchctl", "bootstrap", domain, str(path)]
        elif action == "remove":
            subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
            path.unlink(missing_ok=True)
            return "removed"
        else:
            cmd = ["launchctl", "print", domain + "/" + LABEL]
    elif sys.platform == "win32":
        definition = windows_definition(root)
        if dry_run:
            return definition
        if action == "install":
            path = root / "data" / "analysis-task.xml"
            path.parent.mkdir(exist_ok=True)
            path.write_text(definition, encoding="utf-16")
            cmd = ["schtasks", "/Create", "/TN", LABEL, "/XML", str(path), "/F"]
        elif action == "remove":
            cmd = ["schtasks", "/Delete", "/TN", LABEL, "/F"]
        else:
            cmd = ["schtasks", "/Query", "/TN", LABEL]
    else:
        raise ScreeningError("定期実行の登録はmacOS・Windowsに対応しています。")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ScreeningError("定期実行の登録状態をOSのスケジューラで確認してください。")
    return result.stdout or "ok"
