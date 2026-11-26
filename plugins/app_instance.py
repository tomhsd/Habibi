"""
One Habibi at a time.

When Habibi starts and finds an older copy of itself still running (often the
one left in the system tray), it closes the old copy. Without this the new
copy loses Ctrl+Alt+Q, Ctrl+Alt+D and the phone Remote Control port 8000 to
the old one, and two assistants answer at once.

Runs while plugins load (before the drawing hotkey and the Remote Control
server start); the emergency-stop hotkey, which starts earlier, is registered
again after the old copy is gone. Switch it off in ⚙ PLUGINS to allow two copies.

Also: if Habibi was started with a Python that can't run the offline voice
(Python 3.15 from an old shortcut or the auto-start entry) and the ready-made
Python 3.12 copy (.venv312) is in the folder, it restarts itself with that one.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PLUGIN = {
    "name": "habibi_instances",
    "description": (
        "Lists other running copies of Habibi and can close them. Use when the user "
        "asks whether Habibi is running twice, or to close the other/old Habibi. "
        "action: 'list' (default) or 'close'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {"action": {"type": "STRING", "description": "list | close"}},
        "required": [],
    },
}


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()


def _norm(p) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(p)))
    except Exception:
        return ""


def is_habibi(name: str, cmdline: list[str], cwd: str, base_dir: Path = BASE_DIR) -> bool:
    """True if a process (name, command line, working folder) is a Habibi app
    (not one of its helper scripts). Pure function so it can be tested."""
    name = (name or "").lower()
    args = [a for a in (cmdline or []) if a]
    if any(a == "--run-script" for a in args):
        return False                                  # reminders / updater helpers
    if name == "habibi.exe":
        return True
    if not name.startswith("python"):
        return False
    base = _norm(base_dir)
    for a in args[1:3]:                               # python [-u] main.py …
        if a.lower().endswith("main.py"):
            path = a if os.path.isabs(a) else os.path.join(cwd or "", a)
            return _norm(path) == _norm(Path(base) / "main.py")
    return False


def _family(psutil) -> set[int]:
    """This process, its parents and its children — never touched."""
    me = psutil.Process()
    ids = {me.pid}
    try:
        ids |= {p.pid for p in me.parents()}
        ids |= {p.pid for p in me.children(recursive=True)}
    except Exception:
        pass
    return ids


def find_others() -> list:
    try:
        import psutil
    except ImportError:
        return []
    skip = _family(psutil)
    found = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "cwd"]):
        try:
            if p.info["pid"] in skip:
                continue
            if is_habibi(p.info.get("name") or "", p.info.get("cmdline") or [],
                         p.info.get("cwd") or ""):
                found.append(p)
        except Exception:
            continue
    return found


def close_others(log=print) -> int:
    """Close older Habibi copies. Returns how many were closed. Never raises."""
    try:
        import psutil
        others = find_others()
        if not others:
            return 0
        for p in others:
            try:
                p.terminate()
            except Exception:
                pass
        gone, alive = psutil.wait_procs(others, timeout=5)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
        if alive:
            psutil.wait_procs(alive, timeout=3)
        time.sleep(0.5)                               # let Windows free hotkeys and ports
        return len(others)
    except Exception as e:
        log(f"SYS: Couldn't close the other Habibi: {e}")
        return 0


VENV_PYTHONW = BASE_DIR / ".venv312" / "Scripts" / "pythonw.exe"


def should_switch_python(frozen: bool, current_exe: str, venv_exe: Path,
                         has_offline_voice: bool, already_switched: bool) -> bool:
    """True when Habibi was started with a Python that can't run the offline
    voice (e.g. Python 3.15 from an old shortcut or auto-start entry) while the
    ready-made Python 3.12 copy (.venv312) sits next to it. Pure, for tests."""
    if frozen or already_switched or has_offline_voice:
        return False
    if not venv_exe.exists():
        return False
    return _norm(Path(current_exe).parent) != _norm(venv_exe.parent)


def _switch_python(log=print) -> bool:
    """Restart Habibi with .venv312 when needed. Returns True if it did."""
    try:
        import importlib.util
        import subprocess
        if sys.platform != "win32":
            return False
        has_voice = importlib.util.find_spec("faster_whisper") is not None
        if not should_switch_python(getattr(sys, "frozen", False), sys.executable,
                                    VENV_PYTHONW, has_voice,
                                    os.environ.get("HABIBI_PY_SWITCHED") == "1"):
            return False
        env = dict(os.environ, HABIBI_PY_SWITCHED="1")
        subprocess.Popen([str(VENV_PYTHONW), str(BASE_DIR / "main.py")],
                         cwd=str(BASE_DIR), env=env,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        log("Started with a Python that can't run the offline voice — "
            "restarting with Habibi's Python 3.12.")
        return True
    except Exception as e:
        log(f"Couldn't switch to Python 3.12: {e}")
        return False


def _log(text: str) -> None:
    print(f"[Instances] {text}")


def _on_load() -> None:
    try:
        from memory.config_manager import get_plugin_enabled
        if not get_plugin_enabled(PLUGIN["name"]):
            return
    except Exception:
        pass
    if _switch_python(_log):
        os._exit(0)                                   # the 3.12 copy takes over
    n = close_others(_log)
    if n:
        _log(f"Closed {n} older Habibi window(s).")
        try:
            from core import safety
            safety.start_hotkey(log=_log)             # take Ctrl+Alt+Q back
        except Exception:
            pass
    _note["closed"] = n


_note = {"closed": 0, "told": False}
if os.environ.get("HABIBI_NO_INSTANCE_CHECK") != "1":
    _on_load()


def setup_ui(player) -> None:
    """Say it in the HUD log once the window exists."""
    if _note["closed"] and not _note["told"]:
        _note["told"] = True
        try:
            player.write_log(f"SYS: Closed {_note['closed']} older Habibi that was still "
                             "running — hotkeys and Remote Control are back.")
        except Exception:
            pass


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str((parameters or {}).get("action") or "list").lower().strip()
    if action == "close":
        n = close_others(_log)
        return f"Closed {n} other Habibi." if n else "No other Habibi is running."
    others = find_others()
    if not others:
        return "Only this Habibi is running."
    return f"{len(others)} other Habibi running (process ids: {', '.join(str(p.pid) for p in others)})."
