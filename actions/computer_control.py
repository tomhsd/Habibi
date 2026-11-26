#computer_control.py
from core import secure as _secure
import ctypes
import io
import json
import platform
import re
import string
import subprocess
import sys

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}
import time
import random
from pathlib import Path

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE    = 0.05
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE         = _base_dir()
_CONFIG_PATH  = _BASE / "config" / "api_keys.json"
_MEMORY_PATH  = _BASE / "memory" / "long_term.json"

def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _platform_os() -> str:
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux"
    )

def _get_os() -> str:
    return _load_config().get("os_system", _platform_os()).lower()


def _get_api_key() -> str:
    return _secure.reveal(_load_config().get("gemini_api_key", ""))

_SAFE_SCREENSHOT_ROOTS = (
    Path.home(),
)

def _safe_screenshot_path(requested: str | None) -> Path:
    fallback = Path.home() / "Desktop" / "habibi_screenshot.png"
    if not requested:
        return fallback
    try:
        p = Path(requested).expanduser().resolve()
        for root in _SAFE_SCREENSHOT_ROOTS:
            if p.is_relative_to(root.resolve()):
                p.parent.mkdir(parents=True, exist_ok=True)
                return p
    except Exception:
        pass
    return fallback

def _require_pyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed. Run: pip install pyautogui")

_FIRST_NAMES = [
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Drew", "Quinn",
    "Avery", "Blake", "Cameron", "Dakota", "Emerson", "Finley", "Harper",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Moore", "Taylor", "Anderson", "Thomas", "Jackson",
]
_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "proton.me", "mail.com"]


def _random_data(data_type: str) -> str:
    dt = data_type.lower().strip()

    if dt == "first_name":
        return random.choice(_FIRST_NAMES)

    if dt == "last_name":
        return random.choice(_LAST_NAMES)

    if dt == "name":
        return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"

    if dt == "email":
        first = random.choice(_FIRST_NAMES).lower()
        last  = random.choice(_LAST_NAMES).lower()
        num   = random.randint(10, 999)
        return f"{first}.{last}{num}@{random.choice(_DOMAINS)}"

    if dt == "username":
        return f"{random.choice(_FIRST_NAMES).lower()}{random.randint(100, 9999)}"

    if dt == "password":
        chars = string.ascii_letters + string.digits + "!@#$%"
        raw   = (
            random.choice(string.ascii_uppercase)
            + random.choice(string.digits)
            + random.choice("!@#$%")
            + "".join(random.choices(chars, k=9))
        )
        return "".join(random.sample(raw, len(raw)))

    if dt == "phone":
        return f"+1{random.randint(200,999)}{random.randint(1_000_000, 9_999_999)}"

    if dt == "birthday":
        y = random.randint(1980, 2000)
        m = random.randint(1, 12)
        d = random.randint(1, 28)
        return f"{m:02d}/{d:02d}/{y}"

    if dt == "address":
        num    = random.randint(100, 9999)
        street = random.choice(["Main St", "Oak Ave", "Park Blvd", "Elm St", "Cedar Ln"])
        return f"{num} {street}"

    if dt == "zip_code":
        return str(random.randint(10000, 99999))

    if dt == "city":
        return random.choice(["New York", "Los Angeles", "Chicago", "Houston", "Phoenix"])

    return f"random_{data_type}_{random.randint(1000, 9999)}"

def _user_profile() -> dict:
    """Read identity fields from long-term memory."""
    try:
        if _MEMORY_PATH.exists():
            data     = json.loads(_secure.read_protected_text(_MEMORY_PATH))
            identity = data.get("identity", {})
            return {k: v.get("value", "") for k, v in identity.items()}
    except Exception:
        pass
    return {}

def _keys_target() -> str:
    """Before sending keys: if Habibi's own window has the focus (you just
    talked to it), switch back to the app you were using, and confirm it."""
    if _get_os() != "windows":
        return ""
    from core import target
    fg = target.root_of(ctypes.windll.user32.GetForegroundWindow())
    if fg and not target.is_own(fg):
        return ""
    h = target.last_user_window()
    if not h:
        return "No app window to type into. Open or name the app first."
    if not target.bring_front(h):
        return (f"Nothing typed: I couldn't bring {target.describe(h)} to the front, "
                f"so the keys could have gone into Habibi. Click that window once and ask again.")
    return ""


def _write(text: str, interval: float = 0.03) -> None:
    """Type any text, including Bangla/Arabic/emoji (pyautogui.typewrite
    silently drops characters that aren't on the keyboard layout)."""
    if _get_os() == "windows":
        from pywinauto.keyboard import send_keys
        safe = "".join("{" + c + "}" if c in "+^%~(){}[]" else c for c in text)
        safe = safe.replace("\n", "{ENTER}").replace("\t", "{TAB}")
        send_keys(safe, with_spaces=True, with_newlines=True, pause=interval, vk_packet=True)
    else:
        pyautogui.typewrite(text, interval=interval)


def _type(text: str, interval: float = 0.03) -> str:
    _require_pyautogui()
    err = _keys_target()
    if err:
        return err
    time.sleep(0.15)
    _write(text, interval)
    return f"Typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _smart_type(text: str, clear_first: bool = True) -> str:
    _require_pyautogui()
    err = _keys_target()
    if err:
        return err
    if clear_first:
        _clear_field()
        time.sleep(0.1)

    if len(text) > 20 and _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        paste_key = "command" if _get_os() == "mac" else "ctrl"
        pyautogui.hotkey(paste_key, "v")
        return f"Smart-typed (clipboard): {text[:60]}{'…' if len(text) > 60 else ''}"

    _write(text, 0.04)
    return f"Smart-typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _click(x=None, y=None, button: str = "left", clicks: int = 1) -> str:
    _require_pyautogui()
    if x is not None and y is not None:
        pyautogui.click(x, y, button=button, clicks=clicks)
        return f"{'Double-c' if clicks == 2 else 'C'}licked ({x}, {y}) [{button}]"
    pyautogui.click(button=button, clicks=clicks)
    return f"Clicked at current position [{button}]"


def _hotkey(*keys) -> str:
    _require_pyautogui()
    err = _keys_target()
    if err:
        return err
    pyautogui.hotkey(*keys)
    return f"Hotkey: {'+'.join(keys)}"


def _press(key: str) -> str:
    _require_pyautogui()
    err = _keys_target()
    if err:
        return err
    pyautogui.press(key)
    return f"Pressed: {key}"


def _scroll(direction: str = "down", amount: int = 3) -> str:
    _require_pyautogui()
    vertical   = direction in ("up", "down")
    clicks     = amount if direction in ("up", "right") else -amount
    pyautogui.scroll(clicks) if vertical else pyautogui.hscroll(clicks)
    return f"Scrolled {direction} ×{amount}"


def _move(x: int, y: int, duration: float = 0.3) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x, y, duration=duration)
    return f"Mouse → ({x}, {y})"


def _drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x1, y1, duration=0.2)
    pyautogui.dragTo(x2, y2, duration=duration, button="left")
    return f"Dragged ({x1},{y1}) → ({x2},{y2})"


def _clipboard_get() -> str:
    if _PYPERCLIP:
        return pyperclip.paste()
    _hotkey("ctrl", "c")
    time.sleep(0.2)
    return "(copied — pyperclip unavailable for read)"


def _clipboard_paste(text: str) -> str:
    if _PYPERCLIP:
        _require_pyautogui()
        err = _keys_target()
        if err:
            return err
        pyperclip.copy(text)
        time.sleep(0.1)
        paste_key = "command" if _get_os() == "mac" else "ctrl"
        pyautogui.hotkey(paste_key, "v")
        return f"Pasted: {text[:60]}{'…' if len(text) > 60 else ''}"
    return "pyperclip not available"


def _screenshot(save_path: str | None = None) -> str:
    _require_pyautogui()
    path = _safe_screenshot_path(save_path)
    img  = pyautogui.screenshot()
    img.save(str(path))
    return f"Screenshot saved: {path}"


def _clear_field() -> str:
    _require_pyautogui()
    err = _keys_target()
    if err:
        return err
    select_key = "command" if _get_os() == "mac" else "ctrl"
    pyautogui.hotkey(select_key, "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    return "Field cleared"

_TARGETED = {"type", "smart_type", "click", "left_click", "double_click", "right_click",
             "hotkey", "press", "paste", "clear_field", "scroll", "screen_find", "screen_click"}


def _go_to_window(query: str) -> str:
    """Bring the named app to the front and confirm it. '' = done."""
    from core import target
    try:
        h = target.find(query)
    except LookupError as e:
        names = ", ".join(sorted({target.process_of(w).rsplit(".", 1)[0] or target.title_of(w)
                                  for w in target.user_windows()})[:12])
        return f"Nothing done: {e} Open windows: {names}."
    if not target.bring_front(h):
        return (f"Nothing done: I couldn't bring {target.describe(h)} to the front. "
                f"Click it once and ask again.")
    time.sleep(0.15)
    return ""


def _focus_window(title: str) -> str:
    os_name = _get_os()

    if os_name == "windows":
        try:
            from core import target
            h = target.find(title)
            if target.bring_front(h):
                return f"Focused window: {target.describe(h)}"
            return f"Couldn't bring {target.describe(h)} to the front. Click it once and try again."
        except LookupError as e:
            return str(e)
        except Exception:
            pass
        try:
            script = f'(New-Object -ComObject WScript.Shell).AppActivate("{title}")'
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=5, **_WIN_HIDE,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except Exception as e:
            return f"focus_window (Windows) failed: {e}"

    if os_name == "mac":
        script = (
            f'tell application "System Events" to '
            f'set frontmost of (first process whose name contains "{title}") to true'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=5,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except Exception as e:
            return f"focus_window (macOS) failed: {e}"

    if os_name == "linux":
        try:
            result = subprocess.run(
                ["wmctrl", "-a", title],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                time.sleep(0.3)
                return f"Focused window: {title}"
        except FileNotFoundError:
            pass
        try:
            result = subprocess.run(
                ["xdotool", "search", "--name", title, "windowactivate"],
                capture_output=True, timeout=5,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except FileNotFoundError:
            return "focus_window (Linux) requires wmctrl or xdotool"
        except Exception as e:
            return f"focus_window (Linux) failed: {e}"

    return f"focus_window: unknown OS '{os_name}'"

def _window_rect(hwnd: int):
    """(left, top, width, height) of a window, without the invisible resize border."""
    try:
        from ctypes import wintypes
        r = wintypes.RECT()
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:   # EXTENDED_FRAME_BOUNDS
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        return (r.left, r.top, w, h) if w > 20 and h > 20 else None
    except Exception:
        return None


def _uia_find(hwnd: int, description: str):
    """Exact position from the app's accessibility tree (no guessing)."""
    try:
        import importlib
        uic = importlib.import_module("plugins.ui_control")
        from pywinauto import Desktop
        win = Desktop(backend="uia").window(handle=hwnd)
        el = uic._find(win, description)
        if el is None:
            return None
        r = el.rectangle()
        if r.width() <= 0 or r.height() <= 0:
            return None
        return (r.left + r.right) // 2, (r.top + r.bottom) // 2
    except Exception:
        return None


def _screen_find(description: str, window: str = "") -> tuple[int, int] | None:
    """Where is `description` on screen? 1) the app's accessibility tree (exact),
    2) Gemini vision on a picture of just that app window, asked for a box in
    0–1000 units (models are poor at raw pixel numbers; scaled boxes map back
    exactly whatever the screen size or Windows scaling)."""
    _require_pyautogui()
    region = None
    if _get_os() == "windows":
        from core import target
        try:
            hwnd = target.find(window) if window else target.last_user_window()
        except LookupError:
            hwnd = 0
        if hwnd:
            target.bring_front(hwnd)
            time.sleep(0.25)
            pos = _uia_find(hwnd, description)
            if pos:
                return pos
            region = _window_rect(hwnd)
    if region is None:
        sw, sh = pyautogui.size()
        region = (0, 0, sw, sh)

    api_key = _get_api_key()
    if not api_key:
        print("[ComputerControl] ⚠️ No API key for screen_find")
        return None
    try:
        from google import genai
        from google.genai import types as gtypes
        left, top, rw, rh = region
        img = pyautogui.screenshot(region=(left, top, rw, rh))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        prompt = (
            f"Find the UI element: '{description}'.\n"
            "Reply with JSON only: {\"box_2d\": [ymin, xmin, ymax, xmax]} using coordinates "
            "normalised to 0-1000 over this image. If it is not visible reply {\"box_2d\": null}."
        )
        client = genai.Client(api_key=api_key)
        text = ""
        for model in ("gemini-flash-latest", "gemini-flash-lite-latest"):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=[gtypes.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"), prompt],
                    config=gtypes.GenerateContentConfig(temperature=0),
                )
                text = (resp.text or "").strip()
                break
            except Exception as e:
                print(f"[ComputerControl] screen_find with {model} failed: {e}")
        m = re.search(r"\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]", text)
        if not m:
            return None
        ymin, xmin, ymax, xmax = (min(1000.0, max(0.0, float(v))) for v in m.groups())
        x = left + (xmin + xmax) / 2000.0 * rw
        y = top + (ymin + ymax) / 2000.0 * rh
        return int(round(x)), int(round(y))
    except Exception as e:
        print(f"[ComputerControl] ⚠️ screen_find failed: {e}")
    return None


def computer_control(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Dispatch table for all computer control actions.

    parameters keys (all optional unless noted):
      action        : (required) one of the actions listed below
      text          : text to type or paste
      x, y          : screen coordinates
      button        : 'left' | 'right' (default: left)
      keys          : hotkey string, e.g. 'ctrl+c'
      key           : single key name, e.g. 'enter'
      direction     : 'up' | 'down' | 'left' | 'right'
      amount        : scroll amount (default: 3)
      seconds       : wait duration
      title         : window title fragment for focus_window
      description   : natural-language element description for screen_find/click
      type          : data type for random_data
      field         : memory field name for user_data
      clear_first   : bool, clear field before typing (default: true)
      path          : save path for screenshot (must be inside home dir)

    Actions:
      type          — type text at cursor
      smart_type    — clear field + type (clipboard-backed)
      click         — left click
      double_click  — double left click
      right_click   — right click
      move          — move mouse
      drag          — click-drag between two points
      hotkey        — key combination
      press         — single key
      scroll        — scroll the wheel
      copy          — read clipboard
      paste         — write + paste clipboard
      screenshot    — capture screen (safe path only)
      wait          — sleep N seconds
      clear_field   — select-all + delete
      focus_window  — bring window to foreground
      screen_find   — AI element finder (returns x,y)
      screen_click  — AI element finder + click
      random_data   — generate fake form data
      user_data     — pull real data from memory
    """
    params = parameters or {}
    action = params.get("action", "").lower().strip()

    # Name the app and the keys/clicks go THERE, never into whatever happens
    # to be in front (the main cause of typing into the wrong app).
    win_q = str(params.get("window") or "").strip()
    if win_q and action in _TARGETED and _get_os() == "windows":
        err = _go_to_window(win_q)
        if err:
            return err

    if not action:
        return "No action specified for computer_control."

    if player:
        player.write_log(f"[Computer] {action}")

    print(f"[ComputerControl] ▶ {action}  {params}")

    try:

        if action == "type":
            r = _type(params.get("text", ""))
            if params.get("enter") and r.startswith("Typed"):
                time.sleep(0.1)
                r += " | " + _press("enter")
            return r

        if action == "smart_type":
            return _smart_type(
                params.get("text", ""),
                clear_first=params.get("clear_first", True),
            )

        if action in ("click", "left_click"):
            return _click(params.get("x"), params.get("y"), "left", 1)

        if action == "double_click":
            return _click(params.get("x"), params.get("y"), "left", 2)

        if action == "right_click":
            return _click(params.get("x"), params.get("y"), "right", 1)

        if action == "move":
            return _move(int(params.get("x", 0)), int(params.get("y", 0)))

        if action == "drag":
            return _drag(
                int(params.get("x1", 0)), int(params.get("y1", 0)),
                int(params.get("x2", 0)), int(params.get("y2", 0)),
            )

        if action == "hotkey":
            raw  = params.get("keys", "")
            keys = [k.strip() for k in raw.split("+")] if isinstance(raw, str) else raw
            return _hotkey(*keys)

        if action == "press":
            return _press(params.get("key", "enter"))

        if action == "scroll":
            return _scroll(
                direction=params.get("direction", "down"),
                amount=int(params.get("amount", 3)),
            )

        if action == "copy":
            return _clipboard_get()

        if action == "paste":
            return _clipboard_paste(params.get("text", ""))

        if action == "screenshot":
            return _screenshot(params.get("path"))

        if action == "screen_find":
            coords = _screen_find(params.get("description", ""), win_q)
            return f"{coords[0]},{coords[1]}" if coords else "NOT_FOUND"

        if action == "screen_click":
            desc   = params.get("description", "")
            coords = _screen_find(desc, win_q)
            if coords:
                time.sleep(0.2)
                _click(x=coords[0], y=coords[1])
                return f"Clicked '{desc}' at {coords}"
            return f"Element not found on screen: '{desc}'"

        if action == "wait":
            secs = float(params.get("seconds", 1.0))
            secs = min(secs, 30.0)
            time.sleep(secs)
            return f"Waited {secs}s"

        if action == "clear_field":
            return _clear_field()

        if action == "focus_window":
            return _focus_window(params.get("title", ""))

        if action == "random_data":
            dt     = params.get("type", "name")
            result = _random_data(dt)
            print(f"[ComputerControl] 🎲 random {dt} → {result}")
            return result

        if action == "user_data":
            field   = params.get("field", "name")
            profile = _user_profile()
            value   = profile.get(field, "")
            if not value:
                value = _random_data(field)
                print(f"[ComputerControl] ⚠️ No '{field}' in memory, using random: {value}")
            return value

        return f"Unknown action: '{action}'"

    except Exception as e:
        print(f"[ComputerControl] ❌ {action}: {e}")
        return f"computer_control '{action}' failed: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "computer_control",
    "description": (
        "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements "
        "on screen. Whenever the user names an app ('write in Notepad', 'click Send in WhatsApp'), pass "
        "it as `window` so keys and clicks go to that app and nowhere else. For buttons/fields prefer "
        "screen_click with a short description (e.g. 'Send button') over raw x,y."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"
            },
            "text": {
                "type": "STRING",
                "description": "Text to type or paste"
            },
            "x": {
                "type": "INTEGER",
                "description": "X coordinate"
            },
            "y": {
                "type": "INTEGER",
                "description": "Y coordinate"
            },
            "keys": {
                "type": "STRING",
                "description": "Key combination e.g. 'ctrl+c'"
            },
            "key": {
                "type": "STRING",
                "description": "Single key e.g. 'enter'"
            },
            "direction": {
                "type": "STRING",
                "description": "up | down | left | right"
            },
            "amount": {
                "type": "INTEGER",
                "description": "Scroll amount (default: 3)"
            },
            "seconds": {
                "type": "NUMBER",
                "description": "Seconds to wait"
            },
            "title": {
                "type": "STRING",
                "description": "Window title for focus_window"
            },
            "window": {
                "type": "STRING",
                "description": "App or window to act in (e.g. 'notepad', 'chrome', 'word', part of the title). "
                               "Brought to the front first; nothing is typed if that fails."
            },
            "description": {
                "type": "STRING",
                "description": "Element description for screen_find/screen_click"
            },
            "type": {
                "type": "STRING",
                "description": "Data type for random_data"
            },
            "field": {
                "type": "STRING",
                "description": "Field for user_data: name|email|city"
            },
            "enter": {
                "type": "BOOLEAN",
                "description": "type: press Enter after the text (use when the user says send/submit/enter)"
            },
            "clear_first": {
                "type": "BOOLEAN",
                "description": "Clear field before typing (default: true)"
            },
            "path": {
                "type": "STRING",
                "description": "Save path for screenshot"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": computer_control,
}
