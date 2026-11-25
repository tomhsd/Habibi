"""
actions/macro.py — record what you do once, replay it by voice.

"Record a macro called report" → Habibi records your mouse clicks, scrolling
and keys until you say "stop recording" (or 10 minutes pass). "Run the report
macro" replays it exactly, at the speed you choose. Ctrl+Alt+Q stops a replay
at once. Macros are saved in config/macros/.

Nothing is recorded while a blocked app (⚙ → 🛡 SAFETY) is in front — so
passwords typed into a password manager or banking app are never captured.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path

from core import automation as A
from core import safety

MAX_RECORD_S = 600


def _dir() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    d = base / "config" / "macros"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(name: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_\- ]", "", name).strip().replace(" ", "_").lower() or "macro"
    return _dir() / f"{safe}.json"


class _Recorder:
    def __init__(self):
        self.events: list = []
        self.name = ""
        self.t0 = 0.0
        self.listeners = []
        self.skipped = 0
        self.active = False

    def _add(self, ev: dict) -> None:
        if not self.active:
            return
        if time.time() - self.t0 > MAX_RECORD_S:
            threading.Thread(target=stop_recording, daemon=True).start()
            return
        if safety.blocked_app():
            self.skipped += 1
            return
        ev["t"] = round(time.time() - self.t0, 3)
        self.events.append(ev)

    def start(self, name: str) -> None:
        from pynput import keyboard, mouse

        def on_click(x, y, button, pressed):
            self._add({"k": "click", "x": x, "y": y, "b": button.name, "p": pressed})

        def on_scroll(x, y, dx, dy):
            self._add({"k": "scroll", "x": x, "y": y, "dx": dx, "dy": dy})

        def _key(key):
            try:
                if getattr(key, "char", None):
                    return {"c": key.char}
                if getattr(key, "name", None):
                    return {"n": key.name}
                return {"vk": key.vk}
            except Exception:
                return None

        def on_press(key):
            k = _key(key)
            if k:
                self._add({"k": "down", **k})

        def on_release(key):
            k = _key(key)
            if k:
                self._add({"k": "up", **k})

        self.events, self.name, self.t0, self.skipped = [], name, time.time(), 0
        self.listeners = [mouse.Listener(on_click=on_click, on_scroll=on_scroll),
                          keyboard.Listener(on_press=on_press, on_release=on_release)]
        for l in self.listeners:
            l.start()
        self.active = True

    def stop(self) -> tuple[str, int, int]:
        self.active = False
        for l in self.listeners:
            try:
                l.stop()
            except Exception:
                pass
        self.listeners = []
        return self.name, len(self.events), self.skipped


_rec = _Recorder()


def stop_recording() -> str:
    if not _rec.active:
        return "No macro is being recorded."
    name, n, skipped = _rec.stop()
    events = _rec.events
    _file(name).write_text(json.dumps({"name": name, "created": int(time.time()), "events": events}),
                           encoding="utf-8")
    note = f" ({skipped} actions in blocked apps were not recorded)" if skipped else ""
    return f"Macro '{name}' saved with {len(events)} steps{note}."


safety.on_stop(lambda: _rec.active and threading.Thread(target=stop_recording, daemon=True).start())


def _play(events: list, speed: float) -> str:
    from pynput import keyboard, mouse
    from pynput.keyboard import Key, KeyCode
    m, k = mouse.Controller(), keyboard.Controller()
    held = set()

    def _keyobj(ev):
        if "c" in ev:
            return ev["c"]
        if "n" in ev:
            return getattr(Key, ev["n"], None)
        return KeyCode.from_vk(ev["vk"])

    start, last_t = time.time(), 0.0
    try:
        for ev in events:
            wait = (ev["t"] - last_t) / max(0.1, speed)
            last_t = ev["t"]
            end = time.time() + min(wait, 5.0)          # long idle gaps are shortened
            while time.time() < end:
                safety.check_stop()
                time.sleep(min(0.02, max(0, end - time.time())))
            safety.check_stop()
            if ev["k"] == "click":
                m.position = (ev["x"], ev["y"])
                btn = getattr(mouse.Button, ev["b"], mouse.Button.left)
                (m.press if ev["p"] else m.release)(btn)
            elif ev["k"] == "scroll":
                m.position = (ev["x"], ev["y"])
                m.scroll(ev["dx"], ev["dy"])
            elif ev["k"] in ("down", "up"):
                key = _keyobj(ev)
                if key is None:
                    continue
                if ev["k"] == "down":
                    k.press(key)
                    held.add(key)
                else:
                    k.release(key)
                    held.discard(key)
    finally:
        for key in list(held):                        # never leave Ctrl/Shift stuck down
            try:
                k.release(key)
            except Exception:
                pass
    return f"Finished in {time.time() - start:.1f} s."


def macro(parameters: dict, player=None) -> str:
    err = A.windows_only()
    if err:
        return err
    action = str(parameters.get("action", "list"))
    name = str(parameters.get("name", "")).strip()
    try:
        if action == "record":
            if _rec.active:
                return f"Already recording '{_rec.name}'. Say stop recording first."
            if not name:
                return "What should the macro be called?"
            _rec.start(name)
            return (f"Recording macro '{name}' now — do the task with your mouse and keyboard, "
                    f"then say 'stop recording'.")
        if action == "stop":
            return stop_recording()
        if action == "list":
            items = sorted(_dir().glob("*.json"))
            if not items:
                return "No macros yet. Say 'record a macro called …' to make one."
            out = []
            for f in items:
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    dur = d["events"][-1]["t"] if d["events"] else 0
                    out.append(f"• {d.get('name', f.stem)} — {len(d['events'])} steps, {dur:.0f} s")
                except Exception:
                    pass
            return "Macros:\n" + "\n".join(out)
        if action == "delete":
            f = _file(name)
            if not f.exists():
                return f"No macro called '{name}'."
            return safety.guarded("destructive", f"Delete macro '{name}'", "", lambda: (f.unlink(), f"Deleted macro '{name}'.")[1])
        if action == "play":
            f = _file(name)
            if not f.exists():
                return f"No macro called '{name}'. Say 'list macros' to see them."
            events = json.loads(f.read_text(encoding="utf-8"))["events"]
            speed = float(parameters.get("speed", 1.0) or 1.0)
            safety.guard_foreground()
            before = A.snapshot()
            return safety.guarded("input", f"Run macro '{name}'", f"{len(events)} steps",
                                  lambda: f"Macro '{name}': " + _play(events, speed) + " " + A.describe_change(before, 0.2))
        return "Actions: record(name), stop, play(name, speed), list, delete(name)."
    except ImportError:
        return "Macros need pynput (pip install pynput)."
    except PermissionError as e:
        return str(e)


TOOL = {
    "name": "macro",
    "description": (
        "Record and replay the user's own mouse/keyboard actions. record(name) starts recording "
        "(the user then does the task), stop saves it, play(name, speed: 0.5 slower … 2 faster) "
        "replays it exactly, list, delete(name). Use when the user says 'record a macro', "
        "'remember how I do this', 'do the X macro'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "record | stop | play | list | delete"},
            "name": {"type": "STRING", "description": "Macro name, e.g. 'daily report'"},
            "speed": {"type": "NUMBER", "description": "play speed: 1 = as recorded, 2 = twice as fast"},
        },
        "required": ["action"],
    },
    "handler": macro,
}
