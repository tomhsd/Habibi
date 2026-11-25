"""
core/safety.py — the rules Habibi follows when it controls the computer.

• Permission level (⚙ → 🛡 SAFETY):
      careful   — asks before anything that clicks, types or changes something
      balanced  — asks before deleting, closing, killing, installing, sending (default)
      free      — asks only before irreversible things (delete, kill, uninstall)
  Questions go through core/confirm.py (CONFIRM / CANCEL on the HUD).
• 🛑 Emergency stop — Ctrl+Alt+Q (works in any app) stops every automation at
  once: running macros, OCR clicks, typing, recordings. Tools refuse to act for
  a few seconds afterwards.
• Blocked apps / folders — Habibi never looks at (screenshot/OCR), clicks in or
  types into a blocked app, and never touches files in a blocked folder.
• Activity log — every tool Habibi runs is written to config/activity_log.jsonl.
• Speed — slow / normal / fast pause between automated steps.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


SAFETY_FILE = _base_dir() / "config" / "safety.json"
LOG_FILE = _base_dir() / "config" / "activity_log.jsonl"

LEVELS = ("careful", "balanced", "free")
SPEEDS = {"slow": 0.8, "normal": 0.25, "fast": 0.05}
DEFAULT_BLOCKED_APPS = ["keepass", "1password", "bitwarden", "lastpass", "bank"]

# which kinds of action each level asks about
_ASK = {
    "careful": {"input", "change", "destructive", "irreversible"},
    "balanced": {"destructive", "irreversible"},
    "free": {"irreversible"},
}

_lock = threading.Lock()
_stop_until = 0.0
_cfg_cache: dict | None = None
_cfg_mtime = 0.0


class Stopped(Exception):
    """Raised inside long automations when the emergency stop is pressed."""


# ── settings ─────────────────────────────────────────────────────────────────

def load() -> dict:
    global _cfg_cache, _cfg_mtime
    try:
        m = SAFETY_FILE.stat().st_mtime
    except OSError:
        m = 0.0
    if _cfg_cache is not None and m == _cfg_mtime:
        return dict(_cfg_cache)
    cfg = {"level": "balanced", "speed": "normal",
           "blocked_apps": list(DEFAULT_BLOCKED_APPS), "blocked_folders": []}
    try:
        cfg.update(json.loads(SAFETY_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    if cfg["level"] not in LEVELS:
        cfg["level"] = "balanced"
    if cfg["speed"] not in SPEEDS:
        cfg["speed"] = "normal"
    _cfg_cache, _cfg_mtime = dict(cfg), m
    return cfg


def save(cfg: dict) -> None:
    global _cfg_cache
    SAFETY_FILE.parent.mkdir(parents=True, exist_ok=True)
    SAFETY_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    _cfg_cache = None


def step_delay() -> float:
    return SPEEDS[load()["speed"]]


def pause() -> None:
    """Wait between automated steps (speed setting) and honour the emergency stop."""
    end = time.time() + step_delay()
    while True:
        check_stop()
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(0.05, left))


# ── emergency stop ───────────────────────────────────────────────────────────

def emergency_stop(seconds: float = 4.0) -> None:
    global _stop_until
    with _lock:
        _stop_until = time.time() + seconds
    for cb in list(_stop_listeners):
        try:
            cb()
        except Exception:
            pass


def stopped() -> bool:
    return time.time() < _stop_until


def check_stop() -> None:
    if stopped():
        raise Stopped("Emergency stop pressed")


_stop_listeners: list = []


def on_stop(cb) -> None:
    """Register a function called when the emergency stop is pressed."""
    _stop_listeners.append(cb)


def start_hotkey(log=print) -> bool:
    """Ctrl+Alt+Q anywhere in Windows → emergency_stop(). Returns True if registered."""
    if sys.platform != "win32":
        return False

    def _loop():
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x0312
        if not user32.RegisterHotKey(None, 0xB00B, MOD_ALT | MOD_CONTROL | MOD_NOREPEAT, ord("Q")):
            log("SYS: Emergency stop hotkey Ctrl+Alt+Q is used by another app — "
                "use the STOP button instead.")
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                emergency_stop()
                log("ERR: 🛑 EMERGENCY STOP — all automation stopped.")

    threading.Thread(target=_loop, name="EmergencyStopHotkey", daemon=True).start()
    return True


# ── blocked apps / folders ───────────────────────────────────────────────────

def foreground_app() -> tuple[str, str]:
    """(process name, window title) of the window in front."""
    if sys.platform != "win32":
        return "", ""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ""
        try:
            import psutil
            name = psutil.Process(pid.value).name()
        except Exception:
            pass
        return name, buf.value
    except Exception:
        return "", ""


def blocked_app(process: str = "", title: str = "") -> str:
    """Name of the blocked-list entry matching this app/window, or ''."""
    if not process and not title:
        process, title = foreground_app()
        try:                         # Habibi itself is in front → check the app it will act on
            if sys.platform == "win32":
                from core import target
                fg = target.root_of(__import__("ctypes").windll.user32.GetForegroundWindow())
                if target.is_own(fg):
                    h = target.last_user_window()
                    if h:
                        process, title = target.process_of(h), target.title_of(h)
        except Exception:
            pass
    hay = f"{process} {title}".lower()
    for entry in load().get("blocked_apps", []):
        e = str(entry).strip().lower()
        if e and e in hay:
            return entry
    return ""


def guard_foreground() -> None:
    """Raise PermissionError if the app in front is blocked."""
    b = blocked_app()
    if b:
        raise PermissionError(f"'{b}' is on your blocked list, so I won't look at it, "
                              f"click in it or type in it.")


def blocked_path(path) -> str:
    try:
        p = str(Path(path).expanduser().resolve()).lower()
    except Exception:
        p = str(path).lower()
    for entry in load().get("blocked_folders", []):
        try:
            e = str(Path(str(entry)).expanduser().resolve()).lower()
        except Exception:
            e = str(entry).lower()
        if e and (p == e or p.startswith(e.rstrip("\\/") + "\\") or p.startswith(e.rstrip("\\/") + "/")):
            return entry
    return ""


# ── permission ───────────────────────────────────────────────────────────────

def needs_confirm(kind: str) -> bool:
    """kind: read | open | input | change | destructive | irreversible"""
    return kind in _ASK[load()["level"]]


def guarded(kind: str, title: str, detail: str, run) -> str:
    """Run now, or put a CONFIRM on the HUD first if the permission level says so."""
    if stopped():
        return "Emergency stop is active — not doing anything right now."
    if not needs_confirm(kind):
        return run()
    from core import confirm
    return confirm.request(key=f"safety_{kind}", title=title, detail=detail, run=run)


# ── activity log ─────────────────────────────────────────────────────────────

_SECRET_WORDS = ("password", "token", "api_key", "secret", "pin")


def log_activity(tool: str, params: dict, result: str) -> None:
    try:
        clean = {}
        for k, v in (params or {}).items():
            if any(w in str(k).lower() for w in _SECRET_WORDS):
                clean[k] = "•••"
            else:
                s = str(v)
                clean[k] = s if len(s) <= 200 else s[:200] + "…"
        line = json.dumps({"time": datetime.now().isoformat(timespec="seconds"), "tool": tool,
                           "params": clean, "result": str(result)[:300]}, ensure_ascii=False)
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2_000_000:
                LOG_FILE.replace(LOG_FILE.with_suffix(".old.jsonl"))
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
