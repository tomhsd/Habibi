"""
Test of the offline_mode plugin and the core voice-takeover hook. No Qt, no
network, no Gemini: the connection check and the settings store are faked.

    python packaging/offline_selftest.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["HABIBI_NO_INSTANCE_CHECK"] = "1"   # never close real processes in tests

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
shutil.copy(ROOT / "plugins" / "_offline_engine.py", tmp / "_offline_engine.py")
shutil.copy(ROOT / "plugins" / "_offline_setup.py", tmp / "_offline_setup.py")
shutil.copy(ROOT / "plugins" / "offline_cache.py", tmp / "offline_cache.py")
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
        self.tools_run: list = []
        self.speaking: list = []
        self._loop = None

    def tool_declarations(self):
        return [
            {"name": "open_app", "description": "Open an app",
             "parameters": {"type": "OBJECT", "properties": {
                 "app_name": {"type": "STRING", "description": "app"}}}},
            {"name": "web_search", "description": "online only",
             "parameters": {"type": "OBJECT", "properties": {}}},
        ]

    async def run_tool(self, name, args=None):
        self.tools_run.append((name, dict(args or {})))
        return f"Opened {dict(args or {}).get('app_name', '?')}."

    def set_speaking(self, on):
        self.speaking.append(on)

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
eng = om._load_sibling("_offline_engine")
eng.Brain.available = lambda self: (setattr(self, "error", "no Ollama in the test") or False)
eng.Mouth.say = lambda self, text: "none"      # no speakers in the test
eng.Ears.load = lambda self: (setattr(self, "error", "no Whisper in the test") or False)


async def session_test():
    host._loop = asyncio.get_running_loop()
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

# ── 4. offline engine pieces ────────────────────────────────────────────────
import numpy as np  # noqa: E402

vad = eng.Utterances()
rng = np.random.default_rng(1)
quiet = lambda: (rng.normal(0, 40, eng.BLOCK)).astype(np.int16)          # noqa: E731
t = np.arange(eng.BLOCK) / eng.SAMPLE_RATE
loud = lambda: (3000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)    # noqa: E731
got = [vad.feed(quiet()) for _ in range(30)]
got += [vad.feed(loud()) for _ in range(15)]
got += [vad.feed(quiet()) for _ in range(15)]
utts = [g for g in got if g is not None]
check(len(utts) == 1 and 0.9 < len(utts[0]) / eng.SAMPLE_RATE < 2.5,
      "listening: one spoken phrase becomes one utterance")
got = [vad.feed(loud()) for _ in range(2)] + [vad.feed(quiet()) for _ in range(20)]
check(all(g is None for g in got), "listening: a short click is ignored")

decls = FakeHost().tool_declarations()
tools = eng.offline_tools(decls)
names = [x["function"]["name"] for x in tools]
check(names == ["open_app"], "only offline-safe tools are offered to the local model")
check(tools[0]["function"]["parameters"]["type"] == "object"
      and tools[0]["function"]["parameters"]["properties"]["app_name"]["type"] == "string",
      "tool schemas are converted for Ollama")

calls = []


def fake_post(url, payload, timeout):
    calls.append(payload)
    if len(calls) == 1:
        return {"message": {"content": "", "tool_calls": [
            {"function": {"name": "open_app", "arguments": {"app_name": "notepad"}}}]}}
    if len(calls) == 2:
        return {"message": {"content": "Notepad is open."}}
    return {"message": {"content": "", "tool_calls": [
        {"function": {"name": "web_search", "arguments": "{\"query\": \"news\"}"}}]}}


ran = []
brain = eng.Brain("qwen2.5:3b", lambda n, a: (ran.append((n, a)) or "Opened notepad."),
                  tools, post=fake_post)
ans = brain.reply("open notepad")
check(ran == [("open_app", {"app_name": "notepad"})], "local model's tool call runs the tool")
check(ans == "Notepad is open.", "local model answers after the tool result")
check(calls[1]["messages"][-1]["role"] == "tool", "tool result goes back to the model")
check("internet" in calls[0]["messages"][0]["content"], "system prompt says what needs internet")
check(calls[0]["options"]["num_predict"] <= 120, "offline answers are kept short (fast on CPU)")
ran.clear()
brain.reply("search the news")
check(ran == [], "a tool that isn't offered is never run")

# ── 5. spoken "go offline" while on Gemini rebuilds the voice loop ──────────
om.set_mode(om.MODE_AUTO)
before = host.reconnects
msg = rec.run({"action": "offline"})
check("offline" in msg.lower(), "tool: 'offline' answers")
time.sleep(7.5)
check(host.reconnects > before, "tool: switching to offline asks the core to reconnect")
check("Mode: Always offline" in rec.run({"action": "status"}), "tool: status reports the mode")
rec.run({"action": "auto"})
check(om.get_mode() == om.MODE_AUTO, "tool: 'auto' switches back to automatic")

# ── 6. saved info (offline cache) and setup ─────────────────────────────────
cache_rec = reg._plugins.get("saved_info")
check(cache_rec is not None and cache_rec.valid, "saved_info loads as a plugin")
oc = sys.modules["plugins.offline_cache"]
oc.CACHE_FILE = tmp / "offline_cache.json"
check("don't have any saved" in cache_rec.run({"kind": "news"}), "saved_info: says when nothing is saved")
oc.put("rates:USD", {"USD": 1.0, "BDT": 120.0, "SAR": 3.75})
oc.put("news:top", [{"title": "Rain expected", "source": "BSS"}])
ans = cache_rec.run({"from_currency": "usd", "to_currency": "bdt", "amount": 100})
check("12,000.00 BDT" in ans and "saved just now" in ans, "saved_info: converts money and says the age")
ans = cache_rec.run({"from_currency": "SAR", "to_currency": "BDT"})
check("32.00 BDT" in ans, "saved_info: converts between two non-dollar currencies")
check("Rain expected" in cache_rec.run({"kind": "news"}), "saved_info: reads saved headlines")
check(oc.age_text(time.time() - 3 * 3600) == "saved 3 hours ago", "saved_info: age wording")
check("saved_info" in eng.OFFLINE_TOOLS, "saved_info is offered to the local model")
NET["up"] = False
ok, msg = om._setup_offline({})
check(not ok and "internet" in msg, "setup button: explains it needs the internet once")
st = om._load_sibling("_offline_setup")
check(callable(st.run_setup) and "en" in st.PIPER_VOICES, "setup helper loads")

# ── 7. main.py is wired to the hook ─────────────────────────────────────────
src = (ROOT / "main.py").read_text(encoding="utf-8")
check(src.count("voice_takeover") >= 2, "main.py asks plugins before connecting and before backoff")
check("_run_plugin_voice" in src and "plugin_text_handler" in src, "main.py runs the plugin voice loop")
check("def set_link_status" in (ROOT / "ui.py").read_text(encoding="utf-8"), "ui.py has the footer badge hook")

# ── 8. one Habibi at a time ─────────────────────────────────────────────────

import importlib.util as _ilu
_sp = _ilu.spec_from_file_location("app_instance_t", ROOT / "plugins" / "app_instance.py")
ai = _ilu.module_from_spec(_sp); _sp.loader.exec_module(ai)
_b = Path("/x/Habibi")
check(ai.is_habibi("Habibi.exe", [], "", _b), "instances: spots Habibi.exe")
check(ai.is_habibi("pythonw.exe", ["pythonw", "main.py"], "/x/Habibi", _b), "instances: spots python main.py in the Habibi folder")
check(not ai.is_habibi("python.exe", ["python", "/y/main.py"], "/", _b), "instances: ignores another project's main.py")
check(not ai.is_habibi("Habibi.exe", ["Habibi.exe", "--run-script", "r.py"], "", _b), "instances: leaves helper scripts alone")
check(ai._note["closed"] == 0, "instances: does nothing when switched off")
_venv = Path(tmp) / ".venv312" / "Scripts" / "pythonw.exe"
_venv.parent.mkdir(parents=True, exist_ok=True)
_venv.write_text("")
check(ai.should_switch_python(False, r"C:\Py315\pythonw.exe", _venv, False, False),
      "python switch: Python without the offline voice restarts with .venv312")
check(not ai.should_switch_python(False, str(_venv), _venv, False, False),
      "python switch: already on .venv312 stays")
check(not ai.should_switch_python(False, r"C:\Py315\pythonw.exe", _venv, True, False),
      "python switch: a Python that has the offline voice stays")
check(not ai.should_switch_python(False, r"C:\Py315\pythonw.exe", _venv, False, True),
      "python switch: never loops")
check(not ai.should_switch_python(True, r"C:\Habibi\Habibi.exe", _venv, False, False),
      "python switch: the .exe never switches")
check(not ai.should_switch_python(False, r"C:\Py315\pythonw.exe", Path(tmp) / "nope.exe", False, False),
      "python switch: no .venv312, no switch")

# ── 9. "Hey Habibi" wake word ───────────────────────────────────────────────
_sp = _ilu.spec_from_file_location("wake_habibi_t", ROOT / "plugins" / "wake_habibi.py")
wh = _ilu.module_from_spec(_sp); _sp.loader.exec_module(wh)
for _t in ("Hey Habibi.", "habibi", "Hey, Habeebi!", "Ha bibi", "hey habibi open notepad", "حبيبي"):
    check(wh.heard_habibi(_t), f"hey habibi: hears {_t!r}")
for _t in ("", "hey baby", "hobby", "Habib said hello", "maybe", "Hey Jarvis"):
    check(not wh.heard_habibi(_t), f"hey habibi: ignores {_t!r}")
_regs = pl.PluginRegistry({}, print)
check(_regs.sleep_listeners(object()) == [], "sleep_listener hook: no plugins, no feeds")
_src = (ROOT / "main.py").read_text(encoding="utf-8")
check("self.feed_sleep_listeners(indata)" in _src, "main.py feeds plugin wake words while asleep")

shutil.rmtree(tmp, ignore_errors=True)
print()
print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
