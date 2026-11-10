from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

TASK_NAME = "BitunixDayTraderV1DailyLearning"


def install() -> int:
    if os.name != "nt":
        print("Windows Task Scheduler setup skipped: this helper is intended for Windows.")
        return 0
    script = Path(__file__).resolve().with_name("31-run-daily-learning.cmd")
    task_command = f'cmd.exe /c ""{script}""'
    cmd = [
        "schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "21:00",
        "/TN", TASK_NAME, "/TR", task_command,
    ]
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode == 0:
        print("Daily 21:00 local learning task installed.")
        print(f"Task: {TASK_NAME}")
    else:
        print("Could not install the Windows scheduled task automatically.")
        print((result.stdout or "") + (result.stderr or ""))
        print("The running multi-agent app still performs the same 21:00 check internally.")
    return result.returncode


def remove() -> int:
    if os.name != "nt":
        return 0
    result = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], text=True, capture_output=True)
    print((result.stdout or "") + (result.stderr or ""))
    return result.returncode


if __name__ == "__main__":
    action = (sys.argv[1] if len(sys.argv) > 1 else "install").lower()
    raise SystemExit(remove() if action == "remove" else install())
