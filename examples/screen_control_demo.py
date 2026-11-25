"""
examples/screen_control_demo.py — how Habibi sees and controls the screen.

A short, self-contained walk-through of the three layers Habibi uses
(the full versions live in plugins/ui_control.py, plugins/screen_text.py
and core/automation.py):

  1. CAPTURE   — take a screenshot (mss) and read the UI Automation tree
                 (pywinauto, backend "uia": the same data screen readers use)
  2. LOCATE    — find an element by its name/type, or find text with OCR
                 (Windows.Media.Ocr) and get its exact screen position
  3. INTERACT  — click / type through UI Automation patterns, falling back to
                 real mouse/keyboard input at the located position
  4. VERIFY    — read the value back and compare screenshots before/after

Safety: Ctrl+Alt+Q (Habibi's emergency stop) and the blocked-apps list are
honoured when Habibi's core.safety is importable. Moving the mouse into a
screen corner also aborts (pyautogui fail-safe).

About "full" access on Windows: a normal program cannot click or type into a
window that runs as Administrator (Task Manager, installers, regedit) — Windows
blocks it (UIPI). `can_control()` below detects that case. To control those
windows too, start Habibi with "Run as administrator".

Run on Windows:
    python examples/screen_control_demo.py            # opens Notepad and demos everything
    python examples/screen_control_demo.py --dump     # just print the tree of the front window
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:                                    # Habibi's emergency stop + blocked apps
    from core import safety
except Exception:                       # demo still works on its own
    safety = None


def check_stop() -> None:
    if safety:
        safety.check_stop()


# ─────────────────────────────────────────────────────────────────────────────
# 1. CAPTURE
# ─────────────────────────────────────────────────────────────────────────────

def screenshot(region: dict | None = None) -> tuple[np.ndarray, int, int]:
    """BGRA pixels of the whole desktop (all monitors) or a region, plus its
    top-left offset so image coordinates can be turned into screen coordinates."""
    import mss
    with mss.mss() as s:
        mon = region or s.monitors[0]
        img = np.asarray(s.grab(mon))
    return img, mon["left"], mon["top"]


def foreground_window():
    """The window in front, as a pywinauto UIA wrapper."""
    from pywinauto import Desktop
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    return Desktop(backend="uia").window(handle=hwnd).wrapper_object()


def find_window(title_part: str, timeout: float = 10):
    """First visible top-level window whose title contains `title_part`."""
    from pywinauto import Desktop
    end = time.time() + timeout
    while time.time() < end:
        for w in Desktop(backend="uia").windows(visible_only=True):
            if title_part.lower() in (w.window_text() or "").lower():
                return w
        time.sleep(0.3)
    raise LookupError(f"No window with '{title_part}' in its title")


def element_tree(win, max_nodes: int = 400) -> list[dict]:
    """Every element in a window: name, control type, automation id, screen
    rectangle, and whether it can be used right now."""
    out, queue = [], [(win, 0)]
    while queue and len(out) < max_nodes:
        el, depth = queue.pop(0)
        try:
            kids = el.children()
        except Exception:
            kids = []
        for k in kids:
            info = k.element_info
            try:
                r = k.rectangle()
                rect = (r.left, r.top, r.right, r.bottom)
            except Exception:
                rect = None
            out.append({
                "el": k, "depth": depth + 1,
                "name": (info.name or "").strip(),
                "type": info.control_type or "",
                "id": info.automation_id or "",
                "rect": rect,
                "enabled": _safe(k.is_enabled, False),
                "visible": _safe(k.is_visible, False),
            })
            queue.append((k, depth + 1))
    return out


def print_tree(nodes: list[dict], limit: int = 60) -> None:
    for n in nodes[:limit]:
        flag = "" if n["enabled"] and n["visible"] else "  (hidden/disabled)"
        print(f"{'  ' * n['depth']}[{n['type']}] '{n['name']}'"
              f"{'  id=' + n['id'] if n['id'] else ''}  {n['rect']}{flag}")
    if len(nodes) > limit:
        print(f"  … {len(nodes) - limit} more")


def ocr_words(region: dict | None = None) -> list[dict]:
    """Every word on screen with its exact screen position (Windows OCR)."""
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
    from winrt.windows.storage.streams import DataWriter

    img, ox, oy = screenshot(region)
    img = np.repeat(np.repeat(img, 2, axis=0), 2, axis=1)      # small UI text reads better at 2×
    scale = 2
    maxdim = int(OcrEngine.max_image_dimension)
    img = img[:maxdim, :maxdim]
    h, w = img.shape[:2]
    writer = DataWriter()
    writer.write_bytes(bytes(np.ascontiguousarray(img).tobytes()))
    bmp = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, w, h, BitmapAlphaMode.PREMULTIPLIED)
    engine = OcrEngine.try_create_from_user_profile_languages()
    result = asyncio.run(_recognize(engine, bmp))
    words = []
    for li, line in enumerate(result.lines):
        for word in line.words:
            r = word.bounding_rect
            words.append({"text": word.text, "line": li,
                          "x": ox + (r.x + r.width / 2) / scale,      # centre, in screen pixels
                          "y": oy + (r.y + r.height / 2) / scale})
    return words


async def _recognize(engine, bmp):
    return await engine.recognize_async(bmp)


# ─────────────────────────────────────────────────────────────────────────────
# 2. LOCATE
# ─────────────────────────────────────────────────────────────────────────────

def find_element(win, name: str = "", control_type: str = ""):
    """Best match by name (exact > starts with > contains) and type,
    preferring elements that are visible and enabled."""
    q, want = name.lower(), control_type.lower()
    best, best_score = None, -1
    for n in element_tree(win, max_nodes=2500):
        if want and n["type"].lower() != want:
            continue
        nm = n["name"].lower()
        if not q:
            score = 1
        elif nm == q or n["id"].lower() == q:
            score = 100
        elif nm.startswith(q):
            score = 70
        elif q in nm:
            score = 50
        else:
            continue
        score += 15 if n["visible"] and n["enabled"] else 0
        if score > best_score:
            best, best_score = n["el"], score
    return best


def find_text_on_screen(text: str, region: dict | None = None) -> tuple[float, float] | None:
    """Screen position of `text` (a word or a few words), found by OCR."""
    q = text.lower().split()
    words = ocr_words(region)
    for i, w in enumerate(words):
        span = words[i:i + len(q)]
        if len(span) == len(q) and all(s["line"] == w["line"] for s in span) and \
                all(qq in s["text"].lower() for qq, s in zip(q, span)):
            return (sum(s["x"] for s in span) / len(span), sum(s["y"] for s in span) / len(span))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 3. INTERACT
# ─────────────────────────────────────────────────────────────────────────────

def can_control(win) -> tuple[bool, str]:
    """False when Windows will block our input: a blocked app, or a window
    running as Administrator while we are not (UIPI)."""
    proc_name = ""
    try:
        import psutil
        proc_name = psutil.Process(win.process_id()).name()
    except Exception:
        pass
    if safety and safety.blocked_app(proc_name, win.window_text()):
        return False, f"'{proc_name or win.window_text()}' is on Habibi's blocked list"
    if _is_elevated(win.process_id()) and not _is_elevated(None):
        return False, ("this window runs as Administrator; start Habibi with "
                       "'Run as administrator' to control it")
    return True, "ok"


def bring_to_front(win) -> None:
    """Front a window reliably (plain SetForegroundWindow is often refused)."""
    u32 = ctypes.windll.user32
    hwnd = win.handle
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 9)                                    # SW_RESTORE
    fg = u32.GetForegroundWindow()
    a = u32.GetWindowThreadProcessId(fg, None)
    b = ctypes.windll.kernel32.GetCurrentThreadId()
    u32.AttachThreadInput(b, a, True)
    u32.SetForegroundWindow(hwnd)
    u32.AttachThreadInput(b, a, False)
    time.sleep(0.3)


def click(el) -> str:
    """Click an element: UI Automation 'Invoke' first (no mouse needed, works
    even when partly covered), then a real mouse click at its centre."""
    check_stop()
    try:
        el.invoke()
        return "invoked"
    except Exception:
        pass
    r = el.rectangle()
    click_xy(r.mid_point().x, r.mid_point().y)
    return f"clicked at {r.mid_point().x},{r.mid_point().y}"


def click_xy(x: float, y: float, double: bool = False) -> None:
    import pyautogui
    check_stop()
    pyautogui.FAILSAFE = True                    # mouse in a corner = abort
    pyautogui.moveTo(x, y, duration=0.15)
    check_stop()
    pyautogui.click(clicks=2 if double else 1, interval=0.08)


def type_into(el, text: str, clear: bool = False) -> None:
    """Type into a field: the Value pattern when the field supports it, real
    keystrokes otherwise (needed for rich editors such as Notepad on Win 11)."""
    check_stop()
    try:
        if clear:
            el.iface_value.SetValue(text)
            return
    except Exception:
        pass
    el.set_focus()
    safe = "".join("{" + c + "}" if c in "+^%~(){}[]" else c for c in text)
    el.type_keys(safe, with_spaces=True, with_newlines=True, pause=0.01)


def read_value(el) -> str:
    """What is inside a field (Value pattern → Text pattern → legacy)."""
    for get in (lambda: el.get_value(),
                lambda: el.iface_value.CurrentValue,
                lambda: el.iface_text.DocumentRange.GetText(-1),
                lambda: el.window_text() if el.element_info.control_type == "Document" else "",
                lambda: el.legacy_properties().get("Value", "")):
        try:
            v = get()
            if v:
                return str(v)
        except Exception:
            pass
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# 4. VERIFY
# ─────────────────────────────────────────────────────────────────────────────

def screen_changed(before: np.ndarray, after: np.ndarray, threshold: float = 10.0) -> bool:
    """True when any part of the screen visibly changed (ignores a blinking
    caret). Images are compared as an 18×32 grid of average brightness."""
    def grid(img):
        g = img[:, :, :3].mean(axis=2)
        h, w = g.shape
        return g[: h - h % 18, : w - w % 32].reshape(18, h // 18, 32, w // 32).mean(axis=(1, 3))
    return float(np.abs(grid(after) - grid(before)).max()) > threshold


# ─────────────────────────────────────────────────────────────────────────────
# Demo
# ─────────────────────────────────────────────────────────────────────────────

def demo() -> int:
    import subprocess
    ok_all = True

    def step(name, ok, info=""):
        nonlocal ok_all
        ok_all &= bool(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {info}", flush=True)

    print("1. CAPTURE")
    img, ox, oy = screenshot()
    step("screenshot", img.size > 0, f"{img.shape[1]}×{img.shape[0]} px at ({ox},{oy})")

    proc = subprocess.Popen(["notepad.exe"])
    try:
        win = find_window("Notepad")
        bring_to_front(win)
        allowed, why = can_control(win)
        step("allowed to control Notepad", allowed, why)
        nodes = element_tree(win)
        step("UI Automation tree", len(nodes) > 3, f"{len(nodes)} elements")
        print_tree(nodes, limit=25)

        print("\n2. LOCATE + 3. INTERACT")
        edit = find_element(win, control_type="Document") or find_element(win, control_type="Edit")
        step("found the text area", edit is not None, edit.element_info.control_type if edit else "")
        marker = "Habibi can see this 7342"
        before = screenshot()[0]
        type_into(edit, marker)
        time.sleep(0.6)

        print("\n4. VERIFY")
        value = read_value(edit)
        step("value read back through UI Automation", "7342" in value, repr(value[:60]))
        step("screen changed after typing", screen_changed(before, screenshot()[0]))
        try:
            pos = find_text_on_screen("7342")
            step("OCR found the typed text", pos is not None, f"at {pos}")
            if pos:
                click_xy(*pos, double=True)          # double-click selects the word
                step("clicked the text found by OCR", True)
        except Exception as e:
            step("OCR (needs winrt packages)", False, f"{type(e).__name__}: {e}")

        menu = find_element(win, "File", "MenuItem")
        if menu:
            step("click a menu by its name", True, click(menu))
            time.sleep(0.5)
            import pyautogui
            pyautogui.press("esc")
    finally:
        proc.kill()
        # Windows 11 Notepad runs in its own process; close it without saving
        subprocess.run(["taskkill", "/IM", "notepad.exe", "/F"], capture_output=True)
    print("\nAll steps passed." if ok_all else "\nSome steps failed.")
    return 0 if ok_all else 1


def _is_elevated(pid: int | None) -> bool:
    """Whether a process (or this one, when pid is None) runs as Administrator."""
    k32, adv = ctypes.windll.kernel32, ctypes.windll.advapi32
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.GetCurrentProcess() if pid is None else k32.OpenProcess(0x1000, False, pid)
    if not h:
        return True        # can't even open it: treat as elevated
    tok = wintypes.HANDLE()
    try:
        if not adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):
            return True
        elev, size = wintypes.DWORD(), wintypes.DWORD()
        adv.GetTokenInformation(tok, 20, ctypes.byref(elev), 4, ctypes.byref(size))  # TokenElevation
        return bool(elev.value)
    finally:
        if tok:
            k32.CloseHandle(tok)
        if pid is not None:
            k32.CloseHandle(h)


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("This demo needs Windows.")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true", help="print the front window's UI tree and exit")
    args = ap.parse_args()
    if args.dump:
        time.sleep(2)                       # time to click the window you want
        w = foreground_window()
        print(f"Front window: '{w.window_text()}'  can control: {can_control(w)}")
        print_tree(element_tree(w), limit=200)
        sys.exit(0)
    sys.exit(demo())
