"""
Real Windows smoke test for Habibi's computer-control skills.
Runs on the GitHub Windows build machine:  python packaging/smoke_test_windows.py
Opens Notepad, types into it with UI Automation, reads it back with OCR,
searches files, records the screen, and checks the safety rules.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from core import safety  # noqa: E402
from core.action_loader import discover_actions  # noqa: E402
from core.plugin_loader import discover_plugins  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, fn):
    try:
        ok, info = fn()
    except Exception as e:
        ok, info = False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}"
    results.append((name, ok, str(info)[:400]))
    print(("PASS " if ok else "FAIL ") + name + " — " + str(info)[:400].replace("\n", " | "), flush=True)


# allow everything without the HUD confirm (no UI in CI), no blocked apps
safety.save({"level": "free", "speed": "fast", "blocked_apps": [], "blocked_folders": []})
reg = discover_actions(ROOT / "actions", set())
plugs = discover_plugins(ROOT / "plugins", reg.names(), logger=print)
run = lambda tool, **p: plugs.run(tool, p) if plugs.has(tool) else reg.run(tool, p, {})

_CONTROL = ["ui_control", "screen_text", "window_manager", "office", "macro", "system_tools",
            "find_files", "clipboard_history", "screen_recorder", "media_player"]
check("PC-control tools load as plugins", lambda: (
    all(plugs.has(n) for n in _CONTROL), [r.name + ("" if r.valid else f" ✗ {r.error}") for r in plugs._all_records]))
check("…and no longer as core actions", lambda: (not any(reg.has(n) for n in _CONTROL), "ok"))

np = subprocess.Popen(["notepad.exe"])
time.sleep(3)
MARK = "HABIBI OCR TEST 4821"

check("window_manager list sees Notepad", lambda: ("Notepad" in (r := run("window_manager", action="list")), r))
check("window_manager focus Notepad", lambda: ("Notepad" in (r := run("window_manager", action="focus", window="Notepad")), r))
check("ui_control list_elements", lambda: (len((r := run("ui_control", action="list_elements", window="Notepad")).splitlines()) > 2, r[:300]))
check("ui_control type", lambda: ("Typed" in (r := run("ui_control", action="type", window="Notepad", text=MARK)), r))
time.sleep(0.5)
check("ui_control read back", lambda: ("4821" in (r := run("ui_control", action="read", window="Notepad")), r[:300]))
check("window_manager maximize", lambda: ("Maximize" in (r := run("window_manager", action="maximize", window="Notepad")), r))
time.sleep(0.8)
check("screen_text OCR finds typed text", lambda: ("Found" in (r := run("screen_text", action="find", text="4821")), r))
check("screen_text read", lambda: (len(r := run("screen_text", action="read")) > 20, r[:200]))
check("window_manager snap left", lambda: ("Snap" in (r := run("window_manager", action="snap", window="Notepad", position="left")), r))
check("window_manager monitors", lambda: ("Monitors" in (r := run("window_manager", action="monitors")), r))

# safety: blocked app + emergency stop
safety.save({"level": "free", "speed": "fast", "blocked_apps": ["notepad"], "blocked_folders": []})
check("blocked app is refused", lambda: ("blocked" in (r := run("ui_control", action="type", window="Notepad", text="x")).lower(), r))
safety.save({"level": "free", "speed": "fast", "blocked_apps": [], "blocked_folders": []})
safety.emergency_stop(2)
check("emergency stop blocks tools (plugins too)", lambda: ("Emergency stop" in (r := run("system_tools", action="disks")), r))
time.sleep(2.2)

docs = Path.home() / "Documents"
docs.mkdir(exist_ok=True)
probe = docs / "habibi_smoke_probe_7731.txt"
probe.write_text("habibi smoke test", encoding="utf-8")
check("find_files search", lambda: ("habibi_smoke_probe_7731" in (r := run("find_files", action="search", name="habibi_smoke_probe_7731")), r))
check("system_tools processes", lambda: ("CPU" in (r := run("system_tools", action="processes", count=5)), r[:200]))
check("system_tools disks", lambda: ("free of" in (r := run("system_tools", action="disks")), r))
check("system_tools battery", lambda: (bool(r := run("system_tools", action="battery")), r))
check("system_tools startup_list", lambda: (bool(r := run("system_tools", action="startup_list")), r[:200]))
check("system_tools power_plan", lambda: ("Power plans" in (r := run("system_tools", action="power_plan")), r))
check("system_tools clean_temp (report only)", lambda: ("Temporary files" in (r := run("system_tools", action="clean_temp")), r))

import pyperclip  # noqa: E402
pyperclip.copy("habibi-clip-test-55")
time.sleep(2.5)
check("clipboard history", lambda: ("habibi-clip-test-55" in (r := run("clipboard_history", action="list")), r[:200]))

check("screen_recorder start", lambda: ("Recording the screen" in (r := run("screen_recorder", action="start", fps=10)), r))
time.sleep(3)
check("screen_recorder stop saves mp4", lambda: ("Recording saved" in (r := run("screen_recorder", action="stop")), r))

check("macro list", lambda: ("macro" in (r := run("macro", action="list")).lower(), r))
check("office handles missing Office", lambda: (bool(r := run("office", app="excel", action="read")), r))
from memory.config_manager import save_plugin_enabled as set_plugin_enabled  # noqa: E402
set_plugin_enabled("find_files", False)
check("a plugin can be switched off", lambda: ("disabled" in (r := run("find_files", action="search", name="x")), r))
set_plugin_enabled("find_files", True)
check("activity log written", lambda: (safety.LOG_FILE.exists() and safety.LOG_FILE.stat().st_size > 100,
                                         f"{safety.LOG_FILE.stat().st_size if safety.LOG_FILE.exists() else 0} bytes"))

# ── window/field targeting: Habibi's own window in front must not receive the typing ──
from core import target  # noqa: E402
import ctypes  # noqa: E402
target.start_tracker()
_np = target.find("notepad")
check("target finds Notepad by app name", lambda: ("notepad" in target.process_of(_np).lower(), target.describe(_np)))
check("bring_front confirms focus", lambda: (target.bring_front(_np) and target.is_front(_np), target.describe(_np)))
time.sleep(0.6)
import tkinter as tk  # noqa: E402
hud = tk.Tk(); hud.title("Habibi HUD stand-in"); hud.geometry("400x200+50+50")
hud.update()
_hud_hwnd = target.root_of(hud.winfo_id())
target.bring_front(_hud_hwnd); hud.focus_force(); hud.update(); time.sleep(0.5)
check("own window detected in front", lambda: (target.is_own(target.root_of(ctypes.windll.user32.GetForegroundWindow())), "ok"))
check("blank window = the app you used, not Habibi",
      lambda: (target.last_user_window() == _np, target.describe(target.last_user_window())))
check("ui_control type with Habibi in front goes to Notepad",
      lambda: ("Typed" in (r := run("ui_control", action="type", text=" TARGET-OK-77")) and "Notepad" in r, r))
time.sleep(0.4)
check("…and the text really is in Notepad", lambda: ("TARGET-OK-77" in (r := run("ui_control", action="read", window="Notepad")), r[-200:]))
target.bring_front(_hud_hwnd); hud.update(); time.sleep(0.4)
check("computer_control type (Bangla) with Habibi in front goes to Notepad",
      lambda: ("Typed" in (r := run("computer_control", action="type", text=" আমি-ঠিক-আছি")), r))
time.sleep(0.5)
check("…Bangla text arrived intact", lambda: ("আমি-ঠিক-আছি" in (r := run("ui_control", action="read", window="Notepad")), r[-200:]))
hud.destroy()

np.kill()
probe.unlink(missing_ok=True)
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
Path("smoke_results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
sys.exit(1 if failed else 0)
