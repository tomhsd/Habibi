"""
Offline mode — Habibi keeps working when Gemini can't be reached.

Part 1 (this file): the switch.
  • Watches whether Gemini is reachable (a plain HTTPS check, every 15–60 s,
    every 10 s while offline so it comes back quickly). "Offline" means
    "Gemini can't be reached", so a missing VPN counts too.
  • ⚙ PLUGINS → OFFLINE MODE: Auto / Always online / Always offline.
  • Takes over the voice loop through the core voice hook
    (voice_takeover / voice_session, see core/plugin_loader.py) and hands it
    back to Gemini as soon as the connection is steady again.
  • Footer badge: "OFFLINE · LOCAL" while it runs.
  • Offline voice (plugins/_offline_engine.py): mic → Whisper → a local
    model in Ollama (default qwen2.5:3b) with a short list of offline tools →
    Piper voice, or the built-in Windows voice if no Piper voice is installed.
    Typed commands go through the same path. Wake word and voice lock still apply.
  • "go online", "auto", "status" work by voice or typing, without the model.

  • ⚙ SET UP OFFLINE VOICE: one-time download of everything above
    (plugins/_offline_setup.py). Saved news and exchange rates: plugins/offline_cache.py.
"""
from __future__ import annotations

import asyncio
import threading
import time

PLUGIN = {
    "name": "offline_mode",
    "description": (
        "Habibi's online/offline switch. Use when the user asks whether Habibi "
        "is online or offline, asks to switch to offline mode, back to online "
        "(Gemini) mode, or to automatic switching. action: 'status' (default), "
        "'offline' (always offline), 'online' (always online) or 'auto'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "status | offline | online | auto",
            },
        },
        "required": [],
    },
}

NAMESPACE = "offline_mode"
MODE_AUTO, MODE_ONLINE, MODE_OFFLINE = "Auto", "Always online", "Always offline"
MODES = (MODE_AUTO, MODE_ONLINE, MODE_OFFLINE)

# Any HTTP answer from here means Gemini's servers are reachable (a 404 is fine).
CHECK_URL = "https://generativelanguage.googleapis.com/"
CHECK_HOST = ("generativelanguage.googleapis.com", 443)
CHECK_TIMEOUT = 4.0
OFFLINE_RECHECK = 10            # seconds between checks while offline
ONLINE_STREAK_TO_RETURN = 2     # steady successes needed before handing back
FAILS_MEANS_OFFLINE = 3         # Gemini connects lost in a row → offline even if HTTPS works

DEFAULT_MODEL = "qwen2.5:3b"     # ~2 GB in RAM, good tool use on CPU

BADGE_OFFLINE = "OFFLINE · LOCAL"
BADGE_COLOR = "#ffb347"


def _setup_offline(values=None):
    """Settings button: install / download everything offline mode needs."""
    if not _reachable():
        return False, "Setup needs the internet once — connect and try again."
    host = _host

    def log(t: str) -> None:
        print(f"[OfflineSetup] {t}")
        if host is not None:
            try:
                host.ui.write_log(t)
            except Exception:
                pass
    try:
        return _load_sibling("_offline_setup").run_setup(dict(values or {}), log)
    except Exception as e:
        return False, f"Setup failed: {e}"


PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "OFFLINE MODE",
    "fields": [
        {"key": "mode", "label": "Mode", "type": "choice",
         "options": list(MODES), "default": MODE_AUTO},
        {"key": "check_every", "label": "Check the connection every (seconds)",
         "type": "choice", "options": ["15", "30", "60"], "default": "30"},
        {"key": "model", "label": "Local model (Ollama)", "type": "text",
         "default": "qwen2.5:3b", "placeholder": "qwen2.5:3b  or  llama3.2:3b"},
        {"key": "stt_model", "label": "Speech recognition (bigger = better, slower)",
         "type": "choice", "options": ["tiny", "base", "small"], "default": "base"},
        {"key": "language", "label": "Spoken language", "type": "choice",
         "options": ["auto", "en", "bn", "ar", "hi"], "default": "auto"},
    ],
    "action": {"label": "SET UP OFFLINE VOICE (one-time download)", "run": _setup_offline},
}


# ── settings ────────────────────────────────────────────────────────────────

_cfg_cache: dict = {"t": 0.0, "v": {}}
_mode_override: str | None = None    # only used if saving the setting failed
_host = None                         # the running HabibiLive (from voice_takeover)
_last_nudge = 0.0
_nudge_not_before = 0.0              # lets Habibi finish its spoken reply first


def _config() -> dict:
    now = time.monotonic()
    if now - _cfg_cache["t"] > 3:
        try:
            from memory.config_manager import get_plugin_config
            _cfg_cache["v"] = get_plugin_config(NAMESPACE)
        except Exception:
            _cfg_cache["v"] = {}
        _cfg_cache["t"] = now
    return _cfg_cache["v"]


def get_mode() -> str:
    m = str(_config().get("mode") or _mode_override or MODE_AUTO)
    return m if m in MODES else MODE_AUTO


def set_mode(mode: str) -> None:
    """Save the mode in ⚙ settings, so the settings form and commands agree."""
    global _mode_override
    _mode_override = None
    try:
        from memory.config_manager import get_plugin_config, save_plugin_config
        vals = get_plugin_config(NAMESPACE)
        vals["mode"] = mode
        save_plugin_config(NAMESPACE, vals)
    except Exception as e:
        print(f"[Offline] could not save mode: {e}")
        _mode_override = mode
    _cfg_cache["t"] = 0.0
    _cfg_cache["v"] = dict(_cfg_cache["v"], mode=mode)
    _state.wake.set()


def _interval() -> int:
    try:
        return max(5, int(_config().get("check_every") or 30))
    except (TypeError, ValueError):
        return 30


# ── reachability ────────────────────────────────────────────────────────────

def _reachable() -> bool:
    """True if Gemini's servers answer. Honours proxy/VPN settings via requests;
    falls back to a raw TCP connect if requests is missing. Never raises."""
    try:
        import requests
        requests.head(CHECK_URL, timeout=CHECK_TIMEOUT, allow_redirects=False)
        return True
    except ImportError:
        pass
    except Exception:
        return False
    try:
        import socket
        with socket.create_connection(CHECK_HOST, timeout=CHECK_TIMEOUT):
            return True
    except Exception:
        return False


class _State:
    """Latest connection result plus a success streak (so one lucky check
    doesn't flip Habibi back online in the middle of a bad connection)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.online: bool | None = None     # None = not checked yet
        self.streak = 0                     # consecutive successes
        self.fails = 0                      # consecutive failures
        self.checked_at = 0.0
        self.active = False                 # True while offline mode runs the voice loop
        self.wake = threading.Event()       # ask the monitor to check now

    def record(self, ok: bool) -> None:
        with self.lock:
            self.online = ok
            self.streak = self.streak + 1 if ok else 0
            self.fails = 0 if ok else self.fails + 1
            self.checked_at = time.monotonic()

    def steady_online(self) -> bool:
        with self.lock:
            return bool(self.online) and self.streak >= ONLINE_STREAK_TO_RETURN


_state = _State()
_monitor_started = False
_monitor_lock = threading.Lock()


def _wants_offline_now() -> bool:
    """While Gemini is running: should offline mode take over right away?"""
    mode = get_mode()
    if mode == MODE_OFFLINE:
        return True
    return mode == MODE_AUTO and _state.fails >= 2


def _nudge_core(force: bool = False) -> None:
    """Ask the core to rebuild its voice loop, so voice_takeover is asked again.
    Throttled; no-op while offline mode already runs or before Habibi started."""
    global _last_nudge
    host = _host
    now = time.monotonic()
    if (host is None or _state.active or now < _nudge_not_before
            or (not force and now - _last_nudge < 20)):
        return
    _last_nudge = time.monotonic()
    try:
        host.request_reconnect(keep_context=True, reason="offline mode")
    except Exception as e:
        print(f"[Offline] could not switch: {e}")


def _monitor() -> None:
    while True:
        try:
            _state.record(_reachable())
            if _wants_offline_now():
                _nudge_core()
        except Exception:
            pass
        wait = OFFLINE_RECHECK if (_state.active or _state.online is False) else _interval()
        _state.wake.wait(wait)
        _state.wake.clear()


def _ensure_monitor() -> None:
    global _monitor_started
    with _monitor_lock:
        if _monitor_started:
            return
        _monitor_started = True
        threading.Thread(target=_monitor, name="offline-mode-monitor", daemon=True).start()


# ── core voice hook ─────────────────────────────────────────────────────────

def voice_takeover(host, failures: int = 0) -> bool:
    """Core asks this before each Gemini connect. Runs off the event loop."""
    global _host
    _host = host
    _ensure_monitor()
    mode = get_mode()
    if mode == MODE_OFFLINE:
        return True
    if mode == MODE_ONLINE:
        return False
    if failures >= FAILS_MEANS_OFFLINE:
        return True
    if failures >= 1:
        ok = _reachable()          # Gemini just failed: check now, don't wait for the monitor
        _state.record(ok)
        return not ok
    return _state.online is False


def _should_hand_back() -> bool:
    mode = get_mode()
    if mode == MODE_ONLINE:
        return True
    if mode == MODE_OFFLINE:
        return False
    return _state.steady_online()


def _say(host, text: str) -> None:
    try:
        host.ui.write_log(f"HABIBI: {text}")
    except Exception:
        print(f"[Offline] {text}")


_assistant = None        # the running _offline_engine.OfflineAssistant, if any


def _quick_command(text: str) -> str | None:
    """Mode commands answered without the model (spoken or typed)."""
    low = (text or "").lower().strip(" .!?")
    if low in ("go online", "online", "online mode", "switch to online", "use gemini"):
        set_mode(MODE_ONLINE)
        return "Switching back to Gemini."
    if low in ("auto", "auto mode", "automatic", "go auto"):
        set_mode(MODE_AUTO)
        return "Automatic mode: I'll switch back to Gemini when the connection is steady."
    if low in ("status", "are you online", "are you offline", "offline status"):
        return status_text()
    return None


def _make_text_handler(host):
    def handle(text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        a = _assistant
        if a is not None:
            a.handle_text(text)          # speaks the answer too
            return
        quick = _quick_command(text)
        _say(host, quick or "I'm offline and my local voice isn't running, so I can only "
                            "switch modes right now ('go online', 'auto', 'status').")
    return handle


def _load_sibling(stem: str):
    """Import plugins/<stem>.py by path (works from source and from the .exe,
    where plugins/ is shipped as plain files)."""
    import importlib.util
    import sys
    from pathlib import Path
    name = f"plugins.{stem}"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return mod


def _make_assistant(host):
    eng = _load_sibling("_offline_engine")      # lazy: numpy / sounddevice
    cfg = _config()
    a = eng.OfflineAssistant(host, {
        "model": str(cfg.get("model") or DEFAULT_MODEL).strip(),
        "stt_model": str(cfg.get("stt_model") or "base"),
        "language": str(cfg.get("language") or "auto"),
    }, log=lambda t: _log(host, t))
    a.quick = _quick_command
    return a


def _log(host, text: str) -> None:
    try:
        host.ui.write_log(text)
    except Exception:
        print(f"[Offline] {text}")


async def voice_session(host) -> None:
    """Runs the voice loop while offline. Returns to hand control back to Gemini."""
    global _assistant
    from memory.config_manager import get_plugin_enabled
    _ensure_monitor()
    _state.active = True
    _state.wake.set()
    ui = host.ui
    try:
        ui.set_link_status(BADGE_OFFLINE, BADGE_COLOR)
    except Exception:
        pass
    why = ("you chose Always offline" if get_mode() == MODE_OFFLINE
           else "Gemini can't be reached")
    _log(host, f"SYS: Offline mode — {why}. Running on this PC; "
               "I'll switch back to Gemini on my own when the connection is steady.")
    host.plugin_text_handler = _make_text_handler(host)

    def keep_going() -> bool:
        return get_plugin_enabled(PLUGIN["name"]) and not _should_hand_back()

    try:
        try:
            _assistant = _make_assistant(host)
        except Exception as e:
            _assistant = None
            _log(host, f"SYS: Offline voice couldn't start ({e}). Typed mode commands still work.")
        if _assistant is not None:
            await _assistant.run(keep_going)
        while keep_going():
            await asyncio.sleep(1)
    finally:
        _assistant = None
        _state.active = False
        host.plugin_text_handler = None
        try:
            ui.set_link_status("")
        except Exception:
            pass
    _log(host, "SYS: Connection is back — switching to Gemini.")


# ── tool (spoken / typed commands) ──────────────────────────────────────────

def status_text() -> str:
    mode = get_mode()
    if _state.online is None:
        conn = "not checked yet"
    else:
        conn = "reachable" if _state.online else "not reachable"
    running = "offline (local)" if _state.active else "online (Gemini)"
    return f"Habibi is {running}. Mode: {mode}. Gemini is {conn}."


def run(parameters: dict, player=None, session_memory=None) -> str:
    _ensure_monitor()
    action = str((parameters or {}).get("action") or "status").strip().lower()
    if action in ("offline", "go_offline", "always offline"):
        global _nudge_not_before
        _nudge_not_before = time.monotonic() + 6
        set_mode(MODE_OFFLINE)
        threading.Timer(6.5, _nudge_core, kwargs={"force": True}).start()   # after the reply is spoken
        return ("Switching to offline mode now. Say or type 'go online' "
                "when you want Gemini back.")
    if action in ("online", "go_online", "always online"):
        set_mode(MODE_ONLINE)
        return "Staying online with Gemini. Offline mode won't switch on by itself."
    if action in ("auto", "automatic"):
        set_mode(MODE_AUTO)
        return "Automatic: I'll go offline when Gemini can't be reached and come back on my own."
    if action == "check":
        _state.record(_reachable())
    return status_text()
