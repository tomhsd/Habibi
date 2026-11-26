"""
plugins/ui_control.py — precise control of Windows apps through UI Automation.

Instead of guessing pixel positions from a screenshot, this asks Windows for
the real buttons, menus, fields and lists of a window (the same information
screen readers use) and clicks / types into them by name. It is the most
accurate way to control normal desktop apps (Notepad, Settings, Explorer,
Office, most installers…). For things UI Automation can't see (games, some
web pages, images), use screen_text (OCR) instead.
"""
from __future__ import annotations

import re
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
    """The window the user means: best title/app match, or (blank) the window
    they were using before talking to Habibi — never Habibi's own HUD."""
    from core import target
    hwnd = target.find(title)
    return _desktop().window(handle=hwnd).wrapper_object()


def _bring_front(win) -> bool:
    """Front the window and confirm it really is in front."""
    from core import target
    try:
        return target.bring_front(win.handle)
    except Exception:
        try:
            win.set_focus()
        except Exception:
            pass
        return False


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


def _hints(el) -> str:
    """Other words an element is known by: tooltip/help text, placeholder,
    accessible description (icon buttons and search boxes often have no name)."""
    out = []
    try:
        out.append(el.element_info.element.CurrentHelpText or "")
    except Exception:
        pass
    try:
        lp = el.legacy_properties()
        out += [lp.get("Description", "") or "", lp.get("Help", "") or ""]
    except Exception:
        pass
    try:
        out.append(el.element_info.element.CurrentItemStatus or "")
    except Exception:
        pass
    return " ".join(x for x in out if x).strip().lower()


def _candidates(win, control_type: str = ""):
    """Elements to search. For one control type ask UI Automation directly
    (fast, reaches deep into browser/Electron pages); else walk the tree."""
    if control_type:
        try:
            return win.descendants(control_type=control_type)
        except Exception:
            pass
    return _walk(win)


def _find(win, name: str, control_type: str = ""):
    q = name.strip().lower()
    want = control_type.strip().lower()
    best, best_score = None, -1
    for el in _candidates(win, control_type):
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
        elif q and len(q) > 2 and q in _hints(el):
            score = 45
        else:
            continue
        if ct in _PREFERRED:
            score += 10
        if _visible(el):
            score += 15
        if score > best_score:
            best, best_score = el, score
    return best


_EDITABLE = ("Edit", "Document", "ComboBox")


def _find_field(win, name: str):
    """The text box called `name`. Prefers editable controls; if the name is
    only on a label, takes the text box right next to (or under) that label."""
    for ct in _EDITABLE:
        el = _find(win, name, ct)
        if el is not None:
            return el
    el = _find(win, name, "")
    if el is None or _info(el)[1] in _EDITABLE:
        return el
    try:
        lr = el.rectangle()
    except Exception:
        return el
    best, dist = None, 10 ** 9
    for cand in _walk(win):
        if _info(cand)[1] not in ("Edit", "ComboBox") or not _visible(cand):
            continue
        try:
            r = cand.rectangle()
        except Exception:
            continue
        right_of = abs(r.top - lr.top) < 25 and r.left >= lr.left
        below = r.top >= lr.top and abs(r.left - lr.left) < 60
        if not (right_of or below):
            continue
        d = abs(r.left - lr.right) + abs(r.top - lr.top)
        if d < dist and d < 400:
            best, dist = cand, d
    return best or el


def _has_focus(el) -> bool:
    """True when the keyboard focus is in this element (or inside it)."""
    try:
        from pywinauto.uia_defines import IUIA
        f = IUIA().get_focused_element()
        r = el.rectangle()
        fr = f.CurrentBoundingRectangle
        if IUIA().iuia.CompareElements(f, el.element_info.element):
            return True
        return r.left <= fr.left and r.top <= fr.top and fr.right <= r.right and fr.bottom <= r.bottom \
            and (fr.right - fr.left) > 0
    except Exception:
        return True       # can't tell: don't block


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
    cands = []
    for ct in ("Edit", "Document"):
        try:
            cands += win.descendants(control_type=ct)
        except Exception:
            pass
    for el in cands or _walk(win):
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
    try:        # documents (Notepad, Word…): the text pattern keeps line breaks
        if _info(el)[1] == "Document":
            v = el.iface_text.DocumentRange.GetText(-1)
            if v:
                return str(v)
    except Exception:
        pass
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
    from core import target
    lines = [f"• {target.title_of(h)}  ({target.process_of(h) or '?'})" for h in target.user_windows()[:40]]
    return "Open windows (front-most first):\n" + "\n".join(lines) if lines else "No open windows found."


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
        pt = _vision_point(win, f"the '{name}' {control_type or 'button or control'}")
        if pt is None:
            return (f"No '{name}' found in '{win.window_text()}'. Use action list_elements to see "
                    f"what is there, or screen_text click_text for things UI Automation can't see.")

        def _do_pt() -> str:
            safety.check_stop()
            if not _bring_front(win):
                return f"Didn't click: '{win.window_text()}' wouldn't come to the front."
            before = A.snapshot()
            import pyautogui
            pyautogui.click(pt[0], pt[1], clicks=2 if double else 1, button="right" if right else "left")
            return f"Clicked '{name}' (found by looking at the window) at {pt}. " + A.describe_change(before)

        return safety.guarded("destructive" if A.is_destructive(name) else "input",
                              f"Click '{name}'", f"in {win.window_text()}", _do_pt)
    n, ct, _ = _info(el)
    kind = "destructive" if A.is_destructive(n) else "input"

    def _do() -> str:
        safety.check_stop()
        before = A.snapshot()
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
            if not _bring_front(win):
                return (f"Didn't click: '{win.window_text()}' wouldn't come to the front, "
                        f"so the click could land on another window.")
            el.click_input(button="right" if right else "left", double=double)
        return f"Clicked {ct} '{n}'. " + A.describe_change(before)

    return safety.guarded(kind, f"Click '{n}'", f"in {win.window_text()}", _do)


def _vision_point(win, what: str):
    """Last resort when UI Automation can't see it: look at a picture of the
    window (Gemini, 0-1000 box) and return screen x,y."""
    try:
        import importlib
        cc = importlib.import_module("actions.computer_control")
        return cc._screen_find(what, win.window_text())
    except Exception:
        return None


def _focus_editable(win) -> None:
    """If the keyboard focus isn't in a text box of this window, put it in the main one."""
    try:
        from pywinauto.uia_defines import IUIA
        f = IUIA().get_focused_element()
        if f.CurrentControlType in (50004, 50030):      # Edit, Document
            return
    except Exception:
        pass
    el = _main_editable(win)
    if el is None:
        return
    try:
        el.set_focus()
    except Exception:
        pass
    if not _has_focus(el):
        try:
            el.click_input()
            time.sleep(0.1)
            from pywinauto.keyboard import send_keys
            send_keys("^{END}")
        except Exception:
            pass


def _press(win, keys: str) -> str:
    """Press keys (Enter, Tab, Esc, ctrl+s…) in THIS window, checked in front."""
    from core import target
    from pywinauto.keyboard import send_keys
    names = {"enter": "{ENTER}", "return": "{ENTER}", "tab": "{TAB}", "esc": "{ESC}",
             "escape": "{ESC}", "backspace": "{BACKSPACE}", "delete": "{DELETE}", "space": " ",
             "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
             "home": "{HOME}", "end": "{END}", "pageup": "{PGUP}", "pagedown": "{PGDN}"}
    mods = {"ctrl": "^", "control": "^", "shift": "+", "alt": "%"}
    k = (keys or "enter").strip().lower()
    parts = [x.strip() for x in k.split("+") if x.strip()]
    seq = "".join(mods.get(x, "") for x in parts[:-1])
    last = parts[-1] if parts else "enter"
    seq += names.get(last, ("{" + last.upper() + "}") if re.fullmatch(r"f\d{1,2}", last) else last)
    if not _bring_front(win):
        return f"Didn't press {keys}: '{win.window_text()}' wouldn't come to the front."
    if last in ("enter", "return", "backspace", "delete", "space", "tab") and not seq.startswith(("^", "%")):
        _focus_editable(win)       # the key belongs in the text box, not a menu or tab strip
    before = A.snapshot()
    try:
        target.require_front(win.handle)
        send_keys(seq)
    except target.FocusLost as e:
        return str(e)
    return f"Pressed {keys} in '{win.window_text()}'. " + A.describe_change(before)


def _type(win, text: str, field: str, clear: bool, enter: bool) -> str:
    from core import target

    def _do() -> str:
        safety.check_stop()
        before = A.snapshot()
        hwnd = win.handle
        target_el = _find_field(win, field) if field else _main_editable(win)
        if target_el is None:
            # UI Automation can't see a box (canvas apps, some web pages): find it by
            # looking, click into it, then type real keys into that window only.
            pt = _vision_point(win, f"the '{field}' text input box" if field else "the main text input box")
            if pt is None:
                return (f"No {'field ' + repr(field) if field else 'text box'} found in "
                        f"'{win.window_text()}'. Nothing was typed.")
            if not _bring_front(win):
                return f"Nothing typed: '{win.window_text()}' wouldn't come to the front."
            import pyautogui
            pyautogui.click(pt[0], pt[1])
            time.sleep(0.2)
            from pywinauto.keyboard import send_keys
            try:
                target.require_front(hwnd)
                if clear:
                    send_keys("^a{BACKSPACE}")
                for i in range(0, len(text), 40):
                    safety.check_stop()
                    target.require_front(hwnd)
                    send_keys(_escape_keys(text[i:i + 40]), with_spaces=True, pause=0.01, vk_packet=True)
                if enter:
                    target.require_front(hwnd)
                    send_keys("{ENTER}")
            except target.FocusLost as e:
                return str(e)
            return (f"Typed {len(text)} characters into the box I found by looking at "
                    f"'{win.window_text()}'" + (" and pressed Enter. " if enter else ". ") + A.describe_change(before))
        where = f"'{field}'" if field else "the text box"

        # 1. Fill the field directly through UI Automation (needs no focus, can't go astray)
        if target_el is not None and not enter:
            try:
                target_el.set_edit_text(text if clear else (target_el.get_value() or "") + text)
                time.sleep(0.15)
                if not text.strip() or text.strip()[:30] in _value(target_el):
                    # put the cursor after the text, so a following Enter or more typing
                    # continues there (setting the value leaves it at the start)
                    try:
                        if target.is_front(hwnd):
                            target_el.set_focus()
                            if not _has_focus(target_el):
                                target_el.click_input()
                                time.sleep(0.1)
                            from pywinauto.keyboard import send_keys as _sk
                            target.require_front(hwnd)
                            _sk("^{END}")
                    except Exception:
                        pass
                    return (f"Typed {len(text)} characters into {where} of "
                            f"'{win.window_text()}'. ✓ Checked: the text is there.")
            except Exception:
                pass

        # 2. Real keystrokes: only once the right window AND field have focus
        if not _bring_front(win):
            return (f"Nothing typed: I couldn't bring '{win.window_text()}' to the front, so the "
                    f"keys could have gone somewhere else. Click that window once and ask again.")
        if target_el is not None:
            try:
                target_el.set_focus()
            except Exception:
                pass
            if not _has_focus(target_el):
                try:
                    target_el.click_input()
                    time.sleep(0.1)
                except Exception:
                    pass
            if not _has_focus(target_el):
                return (f"Nothing typed: the cursor didn't go into {where}. "
                        f"Use list_elements to pick the exact field name.")
        from pywinauto.keyboard import send_keys
        try:
            target.require_front(hwnd)
            if clear and target_el is not None:
                send_keys("^a{BACKSPACE}")
            for i in range(0, len(text), 40):          # in pieces so STOP / a window switch can interrupt
                safety.check_stop()
                target.require_front(hwnd)
                send_keys(_escape_keys(text[i:i + 40]), with_spaces=True, pause=0.01, vk_packet=True)
            if enter:
                target.require_front(hwnd)
                send_keys("{ENTER}")
        except target.FocusLost as e:
            return str(e)
        if target_el is not None and text.strip():
            time.sleep(0.2)
            if not enter and text.strip()[:30] in _value(target_el):
                return (f"Typed {len(text)} characters into {where} of "
                        f"'{win.window_text()}'. ✓ Checked: the text is there.")
        return (f"Typed {len(text)} characters into {where} of '{win.window_text()}'"
                + (" and pressed Enter. " if enter else ". ") + A.describe_change(before))

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
            if not _bring_front(win):
                return f"Couldn't bring '{win.window_text()}' to the front."
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
        if action in ("press", "key", "enter"):
            keys = str(parameters.get("keys", "") or parameters.get("key", "") or "enter")
            return safety.guarded("input", f"Press {keys}", f"in {win.window_text()}", lambda: _press(win, keys))
        return "Unknown action. Use list_windows, focus, list_elements, click, type, read or press."
    except (LookupError, PermissionError) as e:
        return str(e)
    except ImportError:
        return "UI Automation needs pywinauto (pip install pywinauto)."


PLUGIN = {
    "name": "ui_control",
    "description": (
        "MOST PRECISE way to control Windows desktop apps: finds real buttons, menus, fields and "
        "lists by their NAME through Windows UI Automation (no pixel guessing). Prefer this over "
        "computer_control clicks. Actions: list_windows; focus(window); list_elements(window, "
        "query, control_type) — see what can be clicked; click(name, window, control_type, double, "
        "right); type(text, name=field, window, clear, enter) — set enter=true whenever the user says "
        "send/submit/press Enter; press(keys, window) — Enter, Tab, Esc, ctrl+s… in that app; "
        "read(window, name). `window` is the app name or part of the title (blank = the app you were using). "
        "If a button or field has no name, it is found by looking at the window. Every click/type reports whether the screen "
        "changed — if it says nothing changed, try list_elements, then screen_text click_text."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list_windows | focus | list_elements | click | type | read | press"},
            "window": {"type": "STRING", "description": "Part of the window title; blank = front window"},
            "name": {"type": "STRING", "description": "Name of the button/menu/field, e.g. 'Save', 'File', 'Search'"},
            "control_type": {"type": "STRING", "description": "Optional: Button, MenuItem, Edit, ListItem, TabItem, CheckBox, Hyperlink…"},
            "query": {"type": "STRING", "description": "list_elements: only names containing this"},
            "text": {"type": "STRING", "description": "Text to type"},
            "clear": {"type": "BOOLEAN", "description": "type: replace existing text"},
            "enter": {"type": "BOOLEAN", "description": "type: press Enter afterwards (send/submit)"},
            "keys": {"type": "STRING", "description": "press: key or combo, e.g. 'enter', 'tab', 'ctrl+s'"},
            "double": {"type": "BOOLEAN", "description": "click: double-click"},
            "right": {"type": "BOOLEAN", "description": "click: right-click"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Plugin entry point (switch this tool off in ⚙ PLUGINS)."""
    return ui_control(parameters or {}, player=player)
