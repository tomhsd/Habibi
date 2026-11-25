"""
actions/window_manager.py — find, arrange and snap windows (multi-monitor).
"""
from __future__ import annotations

import ctypes
import sys

from core import automation as A
from core import safety


def _user32():
    return ctypes.windll.user32


def _monitors() -> list[dict]:
    import win32api
    mons = []
    for i, (hmon, _hdc, _rect) in enumerate(win32api.EnumDisplayMonitors()):
        info = win32api.GetMonitorInfo(hmon)
        l, t, r, b = info["Work"]
        mons.append({"n": i + 1, "left": l, "top": t, "width": r - l, "height": b - t,
                     "primary": bool(info.get("Flags", 0) & 1)})
    mons.sort(key=lambda m: (not m["primary"], m["left"]))
    for i, m in enumerate(mons):
        m["n"] = i + 1
    return mons


def _top_windows() -> list[tuple[int, str]]:
    import win32gui
    out = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            if t and t not in ("Program Manager",) and not win32gui.GetParent(hwnd):
                out.append((hwnd, t))
    win32gui.EnumWindows(_cb, None)
    return out


def _find(title: str) -> tuple[int, str]:
    if not title:
        hwnd = _user32().GetForegroundWindow()
        import win32gui
        return hwnd, win32gui.GetWindowText(hwnd)
    t = title.lower()
    wins = _top_windows()
    for hwnd, name in wins:
        if name.lower() == t:
            return hwnd, name
    for hwnd, name in wins:
        if t in name.lower():
            return hwnd, name
    raise LookupError(f"No open window matches '{title}'.")


def _focus(hwnd: int) -> None:
    import win32con
    import win32gui
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        # Alt press lets SetForegroundWindow work from a background process
        _user32().keybd_event(0x12, 0, 0, 0)
        win32gui.SetForegroundWindow(hwnd)
    finally:
        _user32().keybd_event(0x12, 0, 2, 0)


def _place(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    import win32con
    import win32gui
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.MoveWindow(hwnd, int(x), int(y), int(w), int(h), True)


def _monitor_of(hwnd: int) -> dict:
    import win32gui
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    cx, cy = (l + r) // 2, (t + b) // 2
    for m in _monitors():
        if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
            return m
    return _monitors()[0]


def window_manager(parameters: dict, player=None) -> str:
    err = A.windows_only()
    if err:
        return err
    import win32con
    import win32gui
    action = str(parameters.get("action", "list"))
    try:
        if action == "list":
            rows = [f"• {t}" for _, t in _top_windows()[:40]]
            return "Open windows:\n" + "\n".join(rows)
        if action == "monitors":
            return "Monitors:\n" + "\n".join(
                f"{m['n']}. {m['width']}×{m['height']}" + (" (main)" if m["primary"] else "")
                for m in _monitors())
        if action == "show_desktop":
            before = A.snapshot()
            import pyautogui
            pyautogui.hotkey("win", "d")
            return "Showing the desktop. " + A.describe_change(before)

        hwnd, name = _find(str(parameters.get("window", "")))
        b = safety.blocked_app("", name)
        if b:
            return f"'{b}' is on your blocked list."
        before = A.snapshot()
        if action == "focus":
            _focus(hwnd)
        elif action == "minimize":
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        elif action == "maximize":
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        elif action == "restore":
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        elif action == "close":
            def _close() -> str:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                return f"Asked '{name}' to close (it may ask to save). " + A.describe_change(before, 0.6)
            return safety.guarded("destructive", f"Close '{name}'",
                                  "Unsaved work in it could be lost", _close)
        elif action in ("snap", "move_to_monitor", "move", "resize"):
            m = _monitor_of(hwnd)
            if action == "move_to_monitor":
                n = int(parameters.get("monitor", 2) or 2)
                mons = _monitors()
                if not 1 <= n <= len(mons):
                    return f"There are only {len(mons)} monitor(s)."
                m = mons[n - 1]
                _place(hwnd, m["left"], m["top"], m["width"], m["height"])
                win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
            elif action == "snap":
                side = str(parameters.get("position", "left")).lower()
                L, T, W, H = m["left"], m["top"], m["width"], m["height"]
                spots = {
                    "left": (L, T, W // 2, H), "right": (L + W // 2, T, W - W // 2, H),
                    "top": (L, T, W, H // 2), "bottom": (L, T + H // 2, W, H - H // 2),
                    "top_left": (L, T, W // 2, H // 2), "top_right": (L + W // 2, T, W - W // 2, H // 2),
                    "bottom_left": (L, T + H // 2, W // 2, H - H // 2),
                    "bottom_right": (L + W // 2, T + H // 2, W - W // 2, H - H // 2),
                    "center": (L + W // 6, T + H // 8, W * 2 // 3, H * 3 // 4),
                }
                if side == "full":
                    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
                elif side in spots:
                    _place(hwnd, *spots[side])
                else:
                    return "position must be left, right, top, bottom, top_left, top_right, bottom_left, bottom_right, center or full."
            else:
                l, t, r, bt = win32gui.GetWindowRect(hwnd)
                x = int(parameters.get("x", l) if parameters.get("x") is not None else l)
                y = int(parameters.get("y", t) if parameters.get("y") is not None else t)
                w = int(parameters.get("width", r - l) or (r - l))
                h = int(parameters.get("height", bt - t) or (bt - t))
                _place(hwnd, x, y, w, h)
            _focus(hwnd)
        else:
            return "Unknown action. Use list, monitors, focus, minimize, maximize, restore, close, snap, move_to_monitor, move, resize, show_desktop."
        return f"{action.replace('_', ' ').title()}: '{name}'. " + A.describe_change(before, 0.3)
    except LookupError as e:
        return str(e)


TOOL = {
    "name": "window_manager",
    "description": (
        "Arrange windows precisely: list open windows, list monitors, focus (bring to front), "
        "minimize, maximize, restore, close (asks first), snap(position: left/right/top/bottom/"
        "top_left/top_right/bottom_left/bottom_right/center/full), move_to_monitor(monitor), "
        "move(x,y), resize(width,height), show_desktop. `window` = part of its title "
        "(blank = window in front). Always focus a window before typing into it."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | monitors | focus | minimize | maximize | restore | close | snap | move_to_monitor | move | resize | show_desktop"},
            "window": {"type": "STRING", "description": "Part of the window title; blank = front window"},
            "position": {"type": "STRING", "description": "snap: left, right, top, bottom, top_left, top_right, bottom_left, bottom_right, center, full"},
            "monitor": {"type": "INTEGER", "description": "move_to_monitor: 1 = main monitor, 2 = second…"},
            "x": {"type": "INTEGER"}, "y": {"type": "INTEGER"},
            "width": {"type": "INTEGER"}, "height": {"type": "INTEGER"},
        },
        "required": ["action"],
    },
    "handler": window_manager,
}
