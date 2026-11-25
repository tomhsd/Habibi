"""
actions/ui_control.py — precise control of Windows apps through UI Automation.

Instead of guessing pixel positions from a screenshot, this asks Windows for
the real buttons, menus, fields and lists of a window (the same information
screen readers use) and clicks / types into them by name. It is the most
accurate way to control normal desktop apps (Notepad, Settings, Explorer,
Office, most installers…). For things UI Automation can't see (games, some
web pages, images), use screen_text (OCR) instead.
"""
from __future__ import annotations

import time

from core import automation as A
from core import safety

_PREFERRED = ("Button", "MenuItem", "ListItem", "TabItem", "Hyperlink", "CheckBox",
              "RadioButton", "TreeItem", "ComboBox", "Edit", "Document", "SplitButton")
_MAX_NODES = 2500


def _desktop():
    from pywinauto import Desktop
    return Desktop(backend="uia")


def _windows():
    out = []
    for w in _desktop().windows(visible_only=True):
        try:
            title = w.window_text()
        except Exception:
            continue
        if title and title not in ("Program Manager",):
            out.append(w)
    return out


def _window(title: str = ""):
    """The window whose title contains `title`, or the window in front."""
    if title:
        t = title.lower()
        wins = _windows()
        for w in wins:
            if w.window_text().lower() == t:
                return w
        for w in wins:
            if t in w.window_text().lower():
                return w
        raise LookupError(f"No open window matches '{title}'.")
    import ctypes
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    return _desktop().window(handle=hwnd).wrapper_object()


def _bring_front(win) -> None:
    """Front the window without tapping Alt (Alt would open the app's menu bar)."""
    try:
        from actions.window_manager import focus_window
        focus_window(win.handle)
    except Exception:
        try:
            win.set_focus()
        except Exception:
            pass


def _check_allowed(win) -> None:
    try:
        import psutil
        proc = psutil.Process(win.process_id()).name()
    except Exception:
        proc = ""
    b = safety.blocked_app(proc, win.window_text())
    if b:
        raise PermissionError(f"'{b}' is on your blocked list.")


def _walk(win):
    """All elements of a window (breadth-first, capped so huge apps stay fast)."""
    out, queue, seen = [], [win], 0
    while queue and seen < _MAX_NODES:
        el = queue.pop(0)
        try:
            kids = el.children()
        except Exception:
            kids = []
        for k in kids:
            seen += 1
            out.append(k)
            queue.append(k)
    return out


def _info(el) -> tuple[str, str, str]:
    try:
        name = el.window_text() or el.element_info.name or ""
    except Exception:
        name = ""
    try:
        ctype = el.element_info.control_type or ""
    except Exception:
        ctype = ""
    try:
        aid = el.element_info.automation_id or ""
    except Exception:
        aid = ""
    return name.strip(), ctype, aid


def _visible(el) -> bool:
    try:
        return el.is_visible() and el.is_enabled()
    except Exception:
        return True


def _find(win, name: str, control_type: str = ""):
    q = name.strip().lower()
    want = control_type.strip().lower()
    best, best_score = None, -1
    for el in _walk(win):
        n, ct, aid = _info(el)
        if want and ct.lower() != want:
            continue
        nl, al = n.lower(), aid.lower()
        if nl == q or al == q:
            score = 100
        elif nl.startswith(q):
            score = 70
        elif q in nl:
            score = 50
        elif q and q in al:
            score = 40
        else:
            continue
        if ct in _PREFERRED:
            score += 10
        if _visible(el):
            score += 15
        if score > best_score:
            best, best_score = el, score
    return best


def _main_editable(win):
    """The text box the user most likely means: the focused editable element,
    otherwise the largest visible Edit/Document control of the window."""
    try:
        from pywinauto.uia_defines import IUIA
        focused = IUIA().get_focused_element()
        for el in _walk(win):
            try:
                if el.element_info.element == focused and _info(el)[1] in ("Edit", "Document"):
                    return el
            except Exception:
                continue
    except Exception:
        pass
    best, area = None, -1
    for el in _walk(win):
        if _info(el)[1] in ("Edit", "Document") and _visible(el):
            try:
                r = el.rectangle()
                a = r.width() * r.height()
            except Exception:
                a = 0
            if a > area:
                best, area = el, a
    return best


def _value(el) -> str:
    """The text inside a field (not its label)."""
    for getter in ("get_value",):
        try:
            v = getattr(el, getter)()
            if v:
                return str(v)
        except Exception:
            pass
    try:
        v = el.iface_value.CurrentValue
        if v:
            return str(v)
    except Exception:
        pass
    try:
        return el.legacy_properties().get("Value", "") or ""
    except Exception:
        return ""


def _escape_keys(text: str) -> str:
    out = []
    for ch in text:
        if ch in "+^%~(){}[]":
            out.append("{" + ch + "}")
        elif ch == "\n":
            out.append("{ENTER}")
        elif ch == "\t":
            out.append("{TAB}")
        else:
            out.append(ch)
    return "".join(out)


# ── actions ──────────────────────────────────────────────────────────────────

def _list_windows() -> str:
    lines = []
    for w in _windows()[:40]:
        try:
            import psutil
            proc = psutil.Process(w.process_id()).name()
        except Exception:
            proc = "?"
        lines.append(f"• {w.window_text()}  ({proc})")
    return "Open windows:\n" + "\n".join(lines) if lines else "No open windows found."


def _list_elements(win, query: str, control_type: str) -> str:
    q, want = query.lower(), control_type.lower()
    rows = []
    for el in _walk(win):
        n, ct, aid = _info(el)
        if not n and not aid:
            continue
        if want and ct.lower() != want:
            continue
        if q and q not in n.lower() and q not in aid.lower():
            continue
        if not _visible(el):
            continue
        rows.append(f"{ct} '{n}'" + (f" [id={aid}]" if aid and aid != n else ""))
        if len(rows) >= 70:
            rows.append("… (more — narrow it with query or control_type)")
            break
    head = f"Elements in '{win.window_text()}':\n"
    return head + ("\n".join(rows) if rows else "(nothing matched)")


def _click(win, name: str, control_type: str, double: bool, right: bool) -> str:
    el = _find(win, name, control_type)
    if el is None:
        return (f"No '{name}' found in '{win.window_text()}'. Use action list_elements to see "
                f"what is there, or screen_text click_text for things UI Automation can't see.")
    n, ct, _ = _info(el)
    kind = "destructive" if A.is_destructive(n) else "input"

    def _do() -> str:
        safety.check_stop()
        before = A.snapshot()
        try:
            _bring_front(win)
        except Exception:
            pass
        done = False
        if not double and not right:
            for attempt in ("invoke", "select", "toggle"):
                try:
                    getattr(el, attempt)()
                    done = True
                    break
                except Exception:
                    continue
        if not done:
            el.click_input(button="right" if right else "left", double=double)
        return f"Clicked {ct} '{n}'. " + A.describe_change(before)

    return safety.guarded(kind, f"Click '{n}'", f"in {win.window_text()}", _do)


def _type(win, text: str, field: str, clear: bool, enter: bool) -> str:
    def _do() -> str:
        safety.check_stop()
        before = A.snapshot()
        try:
            _bring_front(win)
        except Exception:
            pass
        target = _find(win, field, "") if field else _main_editable(win)
        if field and target is None:
            return f"No field '{field}' found in '{win.window_text()}'."
        if target is not None:
            try:
                target.set_focus()
            except Exception:
                pass
        written = False
        if target is not None and not enter:
            try:
                if clear:
                    target.set_edit_text(text)
                else:
                    target.set_edit_text((target.get_value() or "") + text)
                written = True
            except Exception:
                written = False
        if not written:
            from pywinauto.keyboard import send_keys
            if target is not None:
                try:
                    target.set_focus()
                except Exception:
                    try:
                        target.click_input()
                    except Exception:
                        pass
            if clear and target is not None:
                send_keys("^a{BACKSPACE}")
            for i in range(0, len(text), 40):          # in pieces so STOP can interrupt
                safety.check_stop()
                send_keys(_escape_keys(text[i:i + 40]), with_spaces=True, pause=0.01)
        if enter:
            from pywinauto.keyboard import send_keys
            send_keys("{ENTER}")
        where = f"'{field}'" if field else "the text box"
        if target is not None and text.strip():
            time.sleep(0.2)
            if text.strip()[:30] in _value(target):
                return f"Typed {len(text)} characters into {where}. ✓ Checked: the text is there."
        return f"Typed {len(text)} characters into {where}. " + A.describe_change(before)

    return safety.guarded("input", "Type text", text[:80], _do)


def _read(win, field: str) -> str:
    if field:
        el = _find(win, field, "")
        if el is None:
            return f"No '{field}' found."
        v = _value(el) or _info(el)[0]
        return f"{field}: {v}"[:3000] if v else f"'{field}' is empty."
    parts = []
    for el in _walk(win):
        n, ct, _ = _info(el)
        if ct in ("Edit", "Document"):
            v = _value(el)
            if v:
                parts.append(f"[{n or ct}] {v}")
                continue
        if ct in ("Text", "ListItem", "DataItem", "Hyperlink", "Button") and n:
            parts.append(n)
        if sum(len(p) for p in parts) > 3000:
            break
    return f"Text in '{win.window_text()}':\n" + "\n".join(parts)[:3000]


def ui_control(parameters: dict, player=None) -> str:
    err = A.windows_only()
    if err:
        return err
    action = str(parameters.get("action", "")).strip()
    title = str(parameters.get("window", "") or "")
    try:
        if action == "list_windows":
            return _list_windows()
        win = _window(title)
        _check_allowed(win)
        if action == "focus":
            before = A.snapshot()
            _bring_front(win)
            return f"Brought '{win.window_text()}' to the front. " + A.describe_change(before, 0.25)
        if action == "list_elements":
            return _list_elements(win, str(parameters.get("query", "")), str(parameters.get("control_type", "")))
        if action == "click":
            return _click(win, str(parameters.get("name", "")), str(parameters.get("control_type", "")),
                          bool(parameters.get("double", False)), bool(parameters.get("right", False)))
        if action == "type":
            return _type(win, str(parameters.get("text", "")), str(parameters.get("name", "")),
                         bool(parameters.get("clear", False)), bool(parameters.get("enter", False)))
        if action == "read":
            return _read(win, str(parameters.get("name", "")))
        return "Unknown action. Use list_windows, focus, list_elements, click, type or read."
    except (LookupError, PermissionError) as e:
        return str(e)
    except ImportError:
        return "UI Automation needs pywinauto (pip install pywinauto)."


TOOL = {
    "name": "ui_control",
    "description": (
        "MOST PRECISE way to control Windows desktop apps: finds real buttons, menus, fields and "
        "lists by their NAME through Windows UI Automation (no pixel guessing). Prefer this over "
        "computer_control clicks. Actions: list_windows; focus(window); list_elements(window, "
        "query, control_type) — see what can be clicked; click(name, window, control_type, double, "
        "right); type(text, name=field, window, clear, enter); read(window, name). `window` is part "
        "of the title (blank = window in front). Every click/type reports whether the screen "
        "changed — if it says nothing changed, try list_elements, then screen_text click_text."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list_windows | focus | list_elements | click | type | read"},
            "window": {"type": "STRING", "description": "Part of the window title; blank = front window"},
            "name": {"type": "STRING", "description": "Name of the button/menu/field, e.g. 'Save', 'File', 'Search'"},
            "control_type": {"type": "STRING", "description": "Optional: Button, MenuItem, Edit, ListItem, TabItem, CheckBox, Hyperlink…"},
            "query": {"type": "STRING", "description": "list_elements: only names containing this"},
            "text": {"type": "STRING", "description": "Text to type"},
            "clear": {"type": "BOOLEAN", "description": "type: replace existing text"},
            "enter": {"type": "BOOLEAN", "description": "type: press Enter afterwards"},
            "double": {"type": "BOOLEAN", "description": "click: double-click"},
            "right": {"type": "BOOLEAN", "description": "click: right-click"},
        },
        "required": ["action"],
    },
    "handler": ui_control,
}
