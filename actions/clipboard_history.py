"""
actions/clipboard_history.py — remembers the last 30 things you copied.

"Paste what I copied 5 minutes ago", "show my clipboard history". The history
lives only in memory (never saved to disk) and nothing is recorded while a
blocked app (password manager, banking…) is in front.
"""
from __future__ import annotations

import sys
import threading
import time

from core import automation as A
from core import safety

_history: list[tuple[float, str]] = []
_lock = threading.Lock()
_started = False


def _watch() -> None:
    import pyperclip
    last = None
    while True:
        try:
            txt = pyperclip.paste()
            if txt and txt != last and len(txt) < 20000:
                last = txt
                if not safety.blocked_app():
                    with _lock:
                        _history[:] = [h for h in _history if h[1] != txt]
                        _history.insert(0, (time.time(), txt))
                        del _history[30:]
        except Exception:
            pass
        time.sleep(1.0)


def start() -> None:
    global _started
    if _started or sys.platform != "win32":
        return
    _started = True
    threading.Thread(target=_watch, name="ClipboardHistory", daemon=True).start()


start()   # begins when Habibi loads its skills


def _ago(t: float) -> str:
    s = int(time.time() - t)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


def clipboard_history(parameters: dict, player=None) -> str:
    err = A.windows_only()
    if err:
        return err
    action = str(parameters.get("action", "list"))
    with _lock:
        items = list(_history)
    if action == "list":
        if not items:
            return "Nothing copied since Habibi started."
        return "Clipboard history (1 = newest):\n" + "\n".join(
            f"{i}. ({_ago(t)}) {txt[:90].replace(chr(10), ' ')}{'…' if len(txt) > 90 else ''}"
            for i, (t, txt) in enumerate(items[:15], 1))
    if action == "clear":
        with _lock:
            _history.clear()
        return "Clipboard history cleared."
    if action in ("copy", "paste"):
        n = int(parameters.get("index", 1) or 1)
        if not 1 <= n <= len(items):
            return f"There are only {len(items)} items in the history."
        txt = items[n - 1][1]
        import pyperclip
        pyperclip.copy(txt)
        if action == "copy":
            return f"Item {n} is on the clipboard again."

        def _do() -> str:
            safety.guard_foreground()
            import pyautogui
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "v")
            return f"Pasted item {n} into '{A.foreground_title()}'."
        return safety.guarded("input", "Paste from history", txt[:80], _do)
    return "Actions: list, copy(index), paste(index), clear."


TOOL = {
    "name": "clipboard_history",
    "description": (
        "History of the last 30 things the user copied (text). list — show them (1 = newest); "
        "paste(index) — paste item N into the front window; copy(index) — put it back on the "
        "clipboard; clear."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | paste | copy | clear"},
            "index": {"type": "INTEGER", "description": "Item number from list (1 = newest)"},
        },
        "required": ["action"],
    },
    "handler": clipboard_history,
}
