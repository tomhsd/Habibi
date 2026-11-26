"""
Test of the offline_mode plugin and the core voice-takeover hook. No Qt, no
network, no Gemini: the connection check and the settings store are faked.

    python packaging/offline_selftest.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import memory.config_manager as cm  # noqa: E402

# ── fake settings store (never touches config/api_keys.json) ────────────────
_store: dict = {}
_enabled: dict = {}
cm.get_plugin_config = lambda ns: dict(_store.get(ns, {}))
cm.save_plugin_config = lambda ns, vals: _store.__setitem__(ns, dict(vals))
cm.get_plugin_enabled = lambda name: _enabled.get(name, True)

import core.plugin_loader as pl  # noqa: E402
pl.get_plugin_enabled = cm.get_plugin_enabled
pl.get_plugin_config = cm.get_plugin_config

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILS.append(label)


# ── 1. loader picks up the voice hooks, and only valid ones ─────────────────
tmp = Path(tempfile.mkdtemp(prefix="habibi_offline_"))
shutil.copy(ROOT / "plugins" / "offline_mode.py", tmp / "offline_mode.py")
(tmp / "half_hook.py").write_text(
    "PLUGIN = {'name': 'half_hook', 'description': 'x', 'parameters': {'type': 'OBJECT'}}\n"
    "def run(parameters): return 'ok'\n"
    "def voice_takeover(host, failures=0): return True\n"
    "def voice_session(host): return None   # not async -> must be ignored\n",
    encoding="utf-8")
reg = pl.discover_plugins(tmp, set(), logger=lambda m: print(f"  [loader] {m}"))
rec = reg._plugins.get("offline_mode")
check(rec is not None and rec.valid, "offline_mode loads as a plugin")
check(rec is not None and rec.voice_wants is not None and rec.voice_session is not None,
      "offline_mode declares the voice hooks")
half = reg._plugins.get("half_hook")
check(half is not None and half.voice_session is None,
      "a plugin with a non-async voice_session gets no takeover")

om = sys.modules["plugins.offline_mode"]

# ── fake connection check ───────────────────────────────────────────────────
NET = {"up": True}
om._reachable = lambda: NET["up"]
om.ONLINE_STREAK_TO_RETURN = 2
om.OFFLINE_RECHECK = 0.2


class FakeUI:
    def __init__(self):
        self.badge = None
        self.log: list[str] = []

    def set_link_status(self, text, color=None):
        self.badge = text

    def set_state(self, s):
        pass

    def write_log(self, t):
        self.log.append(t)


class FakeHost:
    def __init__(self):
        self.ui = FakeUI()
        self.plugin_text_handler = None
        self.reconnects = 0

    def request_reconnect(self, keep_context=True, reason=""):
        self.reconnects += 1


host = FakeHost()
_enabled["half_hook"] = False   # keep the test plugin out of the way

# ── 2. who takes over, by mode ──────────────────────────────────────────────
om.set_mode(om.MODE_ONLINE)
NET["up"] = False
check(reg.voice_takeover(host, 5) is None, "Always online: never takes over")

om.set_mode(om.MODE_OFFLINE)
NET["up"] = True
got = reg.voice_takeover(host, 0)
check(got is not None and got.name == "offline_mode", "Always offline: takes over even with internet")

om.set_mode(om.MODE_AUTO)
om._state.record(True)
check(reg.voice_takeover(host, 0) is None, "Auto + reachable: stays on Gemini")
NET["up"] = False
check(reg.voice_takeover(host, 1) is not None, "Auto + Gemini failed + unreachable: takes over at once")
NET["up"] = True
check(reg.voice_takeover(host, 1) is None, "Auto + one failure but reachable: keeps retrying Gemini")
check(reg.voice_takeover(host, 3) is not None, "Auto + 3 failures in a row: offline even if HTTPS works")
check(_store.get("offline_mode", {}).get("mode") == om.MODE_AUTO, "mode is saved in plugin settings")

# ── 3. voice_session runs, answers typed text, hands back when steady ───────
NET["up"] = False
om._state.record(False)
om._local_reply = lambda text: None      # no Ollama in the test


async def session_test():
    task = asyncio.create_task(rec.voice_session(host))
    await asyncio.sleep(1.5)
    check(not task.done(), "offline session keeps running while unreachable")
    check(host.ui.badge == om.BADGE_OFFLINE, "footer badge shows OFFLINE")
    check(callable(host.plugin_text_handler), "typed commands are routed to offline mode")
    host.plugin_text_handler("what's the weather?")
    check(any("offline" in t.lower() for t in host.ui.log), "typed command gets an offline answer")
    host.plugin_text_handler("status")
    check(any("Mode: Auto" in t for t in host.ui.log), "typed 'status' reports the mode")
    NET["up"] = True                      # internet is back
    t0 = time.monotonic()
    await asyncio.wait_for(task, timeout=10)
    check(time.monotonic() - t0 < 10, "hands back to Gemini once the connection is steady")
    check(host.ui.badge == "", "badge restored after hand-back")
    check(host.plugin_text_handler is None, "text routing cleared after hand-back")

    # Always offline must NOT hand back even with internet
    om.set_mode(om.MODE_OFFLINE)
    task = asyncio.create_task(rec.voice_session(host))
    await asyncio.sleep(2)
    check(not task.done(), "Always offline: stays offline with internet up")
    host.plugin_text_handler("go online")
    await asyncio.wait_for(task, timeout=5)
    check(om.get_mode() == om.MODE_ONLINE, "typed 'go online' switches back")

    # disabling the plugin hands back too
    om.set_mode(om.MODE_OFFLINE)
    task = asyncio.create_task(rec.voice_session(host))
    await asyncio.sleep(1.2)
    _enabled["offline_mode"] = False
    await asyncio.wait_for(task, timeout=5)
    check(True, "disabling the plugin hands back")
    _enabled["offline_mode"] = True


asyncio.run(session_test())

# ── 4. spoken "go offline" while on Gemini rebuilds the voice loop ──────────
om.set_mode(om.MODE_AUTO)
before = host.reconnects
msg = rec.run({"action": "offline"})
check("offline" in msg.lower(), "tool: 'offline' answers")
time.sleep(7.5)
check(host.reconnects > before, "tool: switching to offline asks the core to reconnect")
check("Mode: Always offline" in rec.run({"action": "status"}), "tool: status reports the mode")
rec.run({"action": "auto"})
check(om.get_mode() == om.MODE_AUTO, "tool: 'auto' switches back to automatic")

# ── 5. main.py is wired to the hook ─────────────────────────────────────────
src = (ROOT / "main.py").read_text(encoding="utf-8")
check(src.count("voice_takeover") >= 2, "main.py asks plugins before connecting and before backoff")
check("_run_plugin_voice" in src and "plugin_text_handler" in src, "main.py runs the plugin voice loop")
check("def set_link_status" in (ROOT / "ui.py").read_text(encoding="utf-8"), "ui.py has the footer badge hook")

shutil.rmtree(tmp, ignore_errors=True)
print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
