"""
core/target.py — which window Habibi acts on, and making sure keystrokes land there.

Why this exists (bugs it fixes):
  • "The window in front" was usually Habibi's own HUD: you click or type in
    Habibi to give the command, so GetForegroundWindow() returned Habibi and
    text went into the wrong place. Now Habibi remembers the last window YOU
    used that isn't Habibi, and uses that.
  • Title matching took the first window whose title contained the word, in
    arbitrary order, including invisible "cloaked" Store-app windows, tool
    windows and Habibi itself. Now windows are filtered like Alt+Tab does and
    ranked: exact title > app name (chrome → chrome.exe) > title start >
    whole word > anywhere; ties go to the window used most recently.
  • Keys were sent even when bringing the window to the front had failed, so
    they went to whatever had focus. Now focus is verified first, and checked
    again while typing; if you switch windows mid-way Habibi stops typing.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

_OWN_PID = os.getpid()
_history: list[int] = []           # most recent first, user windows only
_hist_lock = threading.Lock()
_tracker_started = False

GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
WS_EX_NOACTIVATE = 0x08000000
DWMWA_CLOAKED = 14


def _u32():
    return ctypes.windll.user32


# ── basic window facts ───────────────────────────────────────────────────────

def title_of(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    _u32().GetWindowTextW(hwnd, buf, 512)
    return buf.value


def pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    _u32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_of(hwnd: int) -> str:
    try:
        import psutil
        return psutil.Process(pid_of(hwnd)).name()
    except Exception:
        return ""


def root_of(hwnd: int) -> int:
    return _u32().GetAncestor(hwnd, GA_ROOT) or hwnd if hwnd else 0


def is_own(hwnd: int) -> bool:
    """A window that belongs to Habibi itself (HUD, panels, dialogs)."""
    return bool(hwnd) and pid_of(hwnd) == _OWN_PID


def _cloaked(hwnd: int) -> bool:
    try:
        val = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
        return val.value != 0
    except Exception:
        return False


def is_user_window(hwnd: int) -> bool:
    """A real app window the way Alt+Tab sees it (not Habibi, not hidden)."""
    u = _u32()
    if not hwnd or not u.IsWindow(hwnd) or not u.IsWindowVisible(hwnd):
        return False
    if is_own(hwnd) or _cloaked(hwnd):
        return False
    ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if ex & WS_EX_TOOLWINDOW or ex & WS_EX_NOACTIVATE:
        return False
    t = title_of(hwnd)
    if not t or t in ("Program Manager", "Windows Input Experience"):
        return False
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    if not u.IsIconic(hwnd) and (r.right - r.left < 40 or r.bottom - r.top < 30):
        return False
    return True


def user_windows() -> list[int]:
    """Real app windows, front-most first (Z-order)."""
    out: list[int] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _):
        if is_user_window(hwnd):
            out.append(hwnd)
        return True
    _u32().EnumWindows(proto(_cb), 0)
    return out


# ── remembering the window the user was in ───────────────────────────────────

def _note_foreground() -> None:
    hwnd = root_of(_u32().GetForegroundWindow())
    if hwnd and is_user_window(hwnd):
        with _hist_lock:
            if not _history or _history[0] != hwnd:
                if hwnd in _history:
                    _history.remove(hwnd)
                _history.insert(0, hwnd)
                del _history[20:]


def start_tracker() -> None:
    """Call once at startup: remembers which windows you use, so 'the window
    in front' still means your app after you click on Habibi."""
    global _tracker_started
    if _tracker_started or sys.platform != "win32":
        return
    _tracker_started = True

    def _loop():
        while True:
            try:
                _note_foreground()
            except Exception:
                pass
            time.sleep(0.25)
    threading.Thread(target=_loop, daemon=True, name="habibi-window-tracker").start()


def recent_rank(hwnd: int) -> int:
    with _hist_lock:
        return _history.index(hwnd) if hwnd in _history else 999


def last_user_window() -> int:
    """The window you were using: the front window unless it's Habibi,
    otherwise the most recent real window you used."""
    if sys.platform != "win32":
        return 0
    _note_foreground()
    fg = root_of(_u32().GetForegroundWindow())
    if fg and is_user_window(fg):
        return fg
    with _hist_lock:
        hist = list(_history)
    for h in hist:
        if is_user_window(h):
            return h
    wins = user_windows()
    return wins[0] if wins else 0


# ── finding a window by what the user said ───────────────────────────────────

def _score(query: str, title: str, proc: str) -> int:
    q, t = query.lower().strip(), title.lower()
    stem = proc.lower().rsplit(".", 1)[0]
    if t == q:
        return 100
    if stem and (stem == q or stem == q.replace(" ", "")):
        return 90
    if t.startswith(q):
        return 80
    import re
    if re.search(r"(^|[\s\-–—|:(])" + re.escape(q) + r"($|[\s\-–—|:)])", t):
        return 70
    if stem and q in stem:
        return 60
    if q in t:
        return 50
    return 0


# What people call an app → its program name (when the title doesn't say it).
_ALIASES = {
    "word": "winword", "ms word": "winword", "microsoft word": "winword",
    "powerpoint": "powerpnt", "power point": "powerpnt", "ppt": "powerpnt",
    "excel": "excel", "ms excel": "excel", "outlook": "outlook",
    "vs code": "code", "vscode": "code", "visual studio code": "code",
    "chrome": "chrome", "google chrome": "chrome", "edge": "msedge",
    "microsoft edge": "msedge", "firefox": "firefox", "brave": "brave", "opera": "opera",
    "file explorer": "explorer", "explorer": "explorer", "files": "explorer",
    "my computer": "explorer", "this pc": "explorer",
    "cmd": "cmd", "command prompt": "cmd", "terminal": "windowsterminal",
    "windows terminal": "windowsterminal", "powershell": "powershell",
    "settings": "systemsettings", "task manager": "taskmgr", "paint": "mspaint",
    "notepad": "notepad", "notepad++": "notepad++", "whatsapp": "whatsapp",
    "telegram": "telegram", "discord": "discord", "spotify": "spotify",
    "vlc": "vlc", "zoom": "zoom", "teams": "ms-teams", "anydesk": "anydesk",
    "calculator": "calculatorapp", "photoshop": "photoshop",
}
_FILLER = {"the", "app", "application", "window", "program", "software", "my", "open", "in", "on"}


def _variants(query: str) -> list[str]:
    """The ways to look for what the user said: as said, without filler words
    ('the notepad app' → 'notepad'), its program name, and single words."""
    q = query.lower().strip().strip("'\"")
    words = [w for w in q.replace("_", " ").split() if w not in _FILLER]
    core_q = " ".join(words) or q
    out = [q, core_q]
    if core_q in _ALIASES:
        out.append(_ALIASES[core_q])
    out += [w for w in words if len(w) > 2]
    seen, res = set(), []
    for v in out:
        if v and v not in seen:
            seen.add(v); res.append(v)
    return res


def find(query: str) -> int:
    """Best window for `query` (part of its title or the app name). Blank
    means the window you were using. Raises LookupError when nothing fits."""
    if not query or not query.strip():
        h = last_user_window()
        if not h:
            raise LookupError("I can't tell which window you mean. Name the app or window.")
        return h
    best, best_key = 0, None
    variants = _variants(query)
    for z, h in enumerate(user_windows()):
        t, p = title_of(h), process_of(h)
        # earlier variants are what the user said more exactly: they count more
        s = max((_score(v, t, p) - 5 * i for i, v in enumerate(variants) if _score(v, t, p)), default=0)
        if s <= 0:
            continue
        key = (s, -recent_rank(h), -z)
        if best_key is None or key > best_key:
            best, best_key = h, key
    if not best:
        raise LookupError(f"No open window matches '{query}'.")
    return best


def describe(hwnd: int) -> str:
    p = process_of(hwnd)
    return f"'{title_of(hwnd)}'" + (f" ({p})" if p else "")


# ── focus that is checked, not hoped for ─────────────────────────────────────

def is_front(hwnd: int) -> bool:
    return root_of(_u32().GetForegroundWindow()) == root_of(hwnd)


def bring_front(hwnd: int, tries: int = 3) -> bool:
    """Bring a window to the front and confirm it really is. Never taps Alt
    (that opens the app's menu bar and turns typed letters into shortcuts)."""
    u, k32 = _u32(), ctypes.windll.kernel32
    for attempt in range(tries):
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)                       # SW_RESTORE
        if is_front(hwnd):
            return True
        fg = u.GetForegroundWindow()
        me = k32.GetCurrentThreadId()
        fg_tid = u.GetWindowThreadProcessId(fg, None)
        attached = bool(fg_tid and fg_tid != me and u.AttachThreadInput(fg_tid, me, True))
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(fg_tid, me, False)
        time.sleep(0.12 + 0.1 * attempt)
        if is_front(hwnd):
            return True
        u.keybd_event(0x10, 0, 0, 0)                    # harmless Shift unlocks SetForegroundWindow
        u.keybd_event(0x10, 0, 2, 0)
        u.SetForegroundWindow(hwnd)
        time.sleep(0.1)
    return is_front(hwnd)


class FocusLost(RuntimeError):
    pass


def require_front(hwnd: int) -> None:
    """Call before every burst of keystrokes. Stops typing into the wrong app."""
    if not is_front(hwnd):
        now = root_of(_u32().GetForegroundWindow())
        raise FocusLost(f"Stopped typing: {describe(hwnd)} is no longer in front "
                        f"(now {describe(now) if now else 'nothing'}).")
