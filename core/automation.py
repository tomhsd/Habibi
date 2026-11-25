"""
core/automation.py — shared helpers for the precise computer-control skills
(ui_control, screen_text, window_manager, macro…).

Most importantly: verify. Every action takes a snapshot before, acts, waits,
then reports whether the screen really changed — so Habibi can notice a click
that didn't work and try another way (see → act → check).
"""
from __future__ import annotations

import sys
import time

import numpy as np

from core import safety

DESTRUCTIVE_WORDS = ("delete", "remove", "uninstall", "erase", "format", "discard",
                     "send", "submit", "pay", "purchase", "buy", "confirm order",
                     "empty recycle", "permanently", "reset", "sign out", "log out")


def is_destructive(label: str) -> bool:
    l = (label or "").lower()
    return any(w in l for w in DESTRUCTIVE_WORDS)


def foreground_title() -> str:
    return safety.foreground_app()[1]


def _thumb() -> np.ndarray | None:
    try:
        import mss
        with mss.mss() as s:
            mon = s.monitors[1]
            img = np.asarray(s.grab(mon))[:, :, :3]
        g = img.mean(axis=2)
        h, w = g.shape
        return g[: h - h % 18, : w - w % 32].reshape(18, (h - h % 18) // 18, 32, (w - w % 32) // 32).mean(axis=(1, 3))
    except Exception:
        return None


def snapshot() -> tuple[str, np.ndarray | None]:
    return foreground_title(), _thumb()


def describe_change(before, wait: float | None = None) -> str:
    """Compare the screen now with `before` and say what happened."""
    time.sleep(0.35 if wait is None else wait)
    safety.check_stop()
    t0, img0 = before
    t1, img1 = snapshot()
    changed_title = t0 != t1
    diff = float(np.abs(img1 - img0).mean()) if img0 is not None and img1 is not None else -1.0
    if changed_title:
        return f"✓ Screen changed — front window is now '{t1}'."
    if diff > 1.5:
        return f"✓ Screen changed (window '{t1}')."
    if diff < 0:
        return f"(Could not check the screen. Front window: '{t1}'.)"
    return (f"⚠ Nothing visibly changed in '{t1}'. The action may not have worked — "
            f"check with screen_text read or try another way.")


def click_at(x: int, y: int, button: str = "left", double: bool = False) -> None:
    import pyautogui
    safety.check_stop()
    pyautogui.FAILSAFE = True
    pyautogui.moveTo(x, y, duration=0.12 if safety.load()["speed"] != "fast" else 0)
    safety.check_stop()
    pyautogui.click(x, y, clicks=2 if double else 1, interval=0.08, button=button)


def windows_only() -> str | None:
    if sys.platform != "win32":
        return "This works on Windows only."
    return None
