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
  • Typed commands while offline: answered by a local model if Ollama is
    running, otherwise Habibi says so.

Later parts plug into voice_session(): local listening (Whisper), thinking
(Ollama) and speaking (Piper), a trimmed tool list, and the shared data cache.
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

BADGE_OFFLINE = "OFFLINE · LOCAL"
BADGE_COLOR = "#ffb347"


def _check_connection(_values=None):
    """Settings button: (ok, message)."""
    ok = _reachable()
    _state.record(ok)
    return ok, ("Gemini is reachable — Habibi can be online."
                if ok else "Gemini can't be reached — Habibi would run offline.")


PLUGIN_SETTINGS = {
    "namespace": NAMESPACE,
    "title": "OFFLINE MODE",
    "fields": [
        {"key": "mode", "label": "Mode", "type": "choice",
         "options": list(MODES), "default": MODE_AUTO},
        {"key": "check_every", "label": "Check the connection every (seconds)",
         "type": "choice", "options": ["15", "30", "60"], "default": "30"},
    ],
    "action": {"label": "CHECK CONNECTION", "run": _check_connection},
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


def _local_reply(text: str) -> str | None:
    """Text answer from a local model if Ollama / LM Studio is running, else None."""
    try:
        from core import llm_client
        url, _ = llm_client.get_llm_settings()
        import requests
        probe = (f"{url}/v1/models" if llm_client.get_llm_provider() == "openai"
                 else f"{url}/api/tags")
        if requests.get(probe, timeout=2).status_code != 200:
            return None
        if llm_client.get_llm_provider() == "openai":
            return None      # text path below is Ollama-only for now
        return llm_client.call_llm_text(
            text,
            system=("You are Habibi, a friendly desktop assistant running offline "
                    "on the user's PC. Answer briefly. You can't browse the web."),
            timeout=90,
        ) or None
    except Exception as e:
        print(f"[Offline] local model unavailable: {e}")
        return None


def _make_text_handler(host):
    def handle(text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        low = text.lower().strip(" .!?")
        if low in ("go online", "online", "online mode", "switch to online", "use gemini"):
            set_mode(MODE_ONLINE)
            _say(host, "Switching back to Gemini.")
            return
        if low in ("auto", "auto mode", "automatic", "go auto"):
            set_mode(MODE_AUTO)
            _say(host, "Automatic mode: I'll switch back to Gemini when the connection is steady.")
            return
        if low in ("status", "are you online", "are you offline", "offline status"):
            _say(host, status_text())
            return
        reply = _local_reply(text)
        if reply:
            _say(host, reply)
        else:
            _say(host, "I'm offline right now and no local model is set up yet, "
                       "so I can't answer that. I'll be back as soon as the internet is.")
    return handle


async def voice_session(host) -> None:
    """Runs the voice loop while offline. Returns to hand control back to Gemini."""
    from memory.config_manager import get_plugin_enabled
    _ensure_monitor()
    _state.active = True
    _state.wake.set()
    ui = host.ui
    try:
        ui.set_link_status(BADGE_OFFLINE, BADGE_COLOR)
    except Exception:
        pass
    try:
        ui.set_state("IDLE")
    except Exception:
        pass
    why = ("you chose Always offline" if get_mode() == MODE_OFFLINE
           else "Gemini can't be reached")
    _say(host, f"Offline mode — {why}. Typed commands still reach me; "
               "I'll switch back to Gemini on my own when the connection is steady.")
    host.plugin_text_handler = _make_text_handler(host)
    try:
        while True:
            await asyncio.sleep(1)
            if not get_plugin_enabled(PLUGIN["name"]):
                break
            if _should_hand_back():
                break
    finally:
        _state.active = False
        host.plugin_text_handler = None
        try:
            ui.set_link_status("")
        except Exception:
            pass
    _say(host, "Connection is back — switching to Gemini.")


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
