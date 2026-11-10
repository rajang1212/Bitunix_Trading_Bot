from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from store import data_directory


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def control_path() -> Path:
    return data_directory() / "runtime-control.json"


def pid_path() -> Path:
    return data_directory() / "engine.pid"


def state_path() -> Path:
    return data_directory() / "engine-state.json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_control() -> dict[str, Any]:
    path = control_path()
    default = {"pause_new_trades": False, "stop_requested": False, "flex_fund_target": "DAY"}
    if not path.exists():
        return default
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError
    except Exception:
        return default
    target = str(raw.get("flex_fund_target") or "DAY").upper()
    if target not in ("DAY", "SWING"):
        target = "DAY"
    return {
        "pause_new_trades": bool(raw.get("pause_new_trades", False)),
        "stop_requested": bool(raw.get("stop_requested", False)),
        "flex_fund_target": target,
    }


def write_control(*, pause_new_trades: bool | None = None, stop_requested: bool | None = None,
                  flex_fund_target: str | None = None) -> dict[str, Any]:
    current = read_control()
    if pause_new_trades is not None:
        current["pause_new_trades"] = bool(pause_new_trades)
    if stop_requested is not None:
        current["stop_requested"] = bool(stop_requested)
    if flex_fund_target is not None:
        target = str(flex_fund_target).upper()
        if target not in ("DAY", "SWING"):
            raise ValueError("flex_fund_target must be DAY or SWING")
        current["flex_fund_target"] = target
    current["updated_utc"] = _iso()
    _atomic_json(control_path(), current)
    return current


def set_pause(value: bool) -> dict[str, Any]:
    return write_control(pause_new_trades=value)


def set_flex_fund_target(target: str) -> dict[str, Any]:
    return write_control(flex_fund_target=target)


def request_stop() -> dict[str, Any]:
    return write_control(stop_requested=True)


def prepare_start() -> dict[str, Any]:
    return write_control(stop_requested=False)


def write_pid(pid: int) -> None:
    p = pid_path(); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(str(int(pid)), encoding="ascii")


def clear_pid(expected_pid: int | None = None) -> None:
    p = pid_path()
    if not p.exists():
        return
    if expected_pid is not None:
        try:
            if int(p.read_text(encoding="ascii").strip()) != int(expected_pid):
                return
        except Exception:
            return
    try:
        p.unlink()
    except OSError:
        pass


def read_pid() -> int | None:
    try:
        return int(pid_path().read_text(encoding="ascii").strip())
    except Exception:
        return None


def pid_is_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok and code.value == STILL_ACTIVE)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def engine_running() -> bool:
    pid = read_pid()
    if pid_is_running(pid):
        return True
    if pid is not None:
        clear_pid(pid)
    return False


def write_engine_state(**fields: Any) -> None:
    payload: dict[str, Any] = {}
    path = state_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payload.update(raw)
        except Exception:
            pass
    payload.update(fields)
    payload["updated_utc"] = _iso()
    _atomic_json(path, payload)


def read_engine_state() -> dict[str, Any]:
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def launch_engine(app_dir: Path) -> int:
    if engine_running():
        pid = read_pid()
        if pid is None:
            raise RuntimeError("Trading engine is already running.")
        return pid
    prepare_start()
    logs = data_directory() / "logs"; logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / "engine.log"
    python_exe = Path(sys.executable)
    if os.name == "nt" and python_exe.name.lower() == "pythonw.exe":
        alt = python_exe.with_name("python.exe")
        if alt.exists():
            python_exe = alt
    log_handle = log_path.open("a", encoding="utf-8", buffering=1)
    kwargs: dict[str, Any] = {
        "cwd": str(app_dir),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([str(python_exe), "multiagent.py", "run"], **kwargs)
    log_handle.close()
    write_pid(proc.pid)
    write_engine_state(status="STARTING", pid=proc.pid, started_utc=_iso())
    return int(proc.pid)
