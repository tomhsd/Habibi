"""
plugins/screen_text.py — read text on the screen and click it exactly (OCR).

Uses the OCR engine built into Windows 10/11 (Windows.Media.Ocr — the languages
you have installed in Windows, e.g. English, Arabic, Bangla if added), with
RapidOCR as a fallback. Each word comes back with its exact position, so
"click on Downloads" clicks the real word instead of a guessed spot.
Works for anything visible — web pages, games, images, apps that UI Automation
can't see.
"""
from __future__ import annotations

import asyncio
import re
import sys
import threading
import time

import numpy as np

from core import automation as A
from core import safety

_engine_lock = threading.Lock()


# ── capture ──────────────────────────────────────────────────────────────────

def _grab(area: str) -> tuple[np.ndarray, int, int]:
    """BGRA image + screen offset of the captured area ('screen' or 'window')."""
    import mss
    with mss.mss() as s:
        mon = s.monitors[0]                    # all monitors together
        region = {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}
        if area == "window":
            try:
                import ctypes
                from ctypes import wintypes
                r = wintypes.RECT()
                from core import target
                hwnd = target.last_user_window()      # your app, not Habibi's HUD
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
                if r.right - r.left > 50 and r.bottom - r.top > 50:
                    region = {"left": r.left, "top": r.top,
                              "width": r.right - r.left, "height": r.bottom - r.top}
            except Exception:
                pass
        img = np.asarray(s.grab(region))       # BGRA
    return img, region["left"], region["top"]


# ── OCR engines ──────────────────────────────────────────────────────────────

def _ocr_windows(img: np.ndarray) -> list[dict]:
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
    from winrt.windows.storage.streams import DataWriter

    scale = 2.0 if img.shape[1] <= 2400 else 1.0          # small UI text reads better enlarged
    if scale != 1.0:
        img = np.repeat(np.repeat(img, 2, axis=0), 2, axis=1)
    maxdim = int(OcrEngine.max_image_dimension)
    if max(img.shape[0], img.shape[1]) > maxdim:
        f = maxdim / max(img.shape[0], img.shape[1])
        ys = (np.arange(int(img.shape[0] * f)) / f).astype(int)
        xs = (np.arange(int(img.shape[1] * f)) / f).astype(int)
        img = img[ys][:, xs]
        scale *= f
    h, w = img.shape[:2]
    data = np.ascontiguousarray(img).tobytes()
    writer = DataWriter()
    writer.write_bytes(data)
    bmp = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                 w, h, BitmapAlphaMode.PREMULTIPLIED)
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("Windows OCR has no language installed")

    async def _run():
        return await engine.recognize_async(bmp)

    result = asyncio.run(_run())
    words = []
    for li, line in enumerate(result.lines):
        for word in line.words:
            r = word.bounding_rect
            words.append({"text": word.text, "line": li,
                          "x": r.x / scale, "y": r.y / scale,
                          "w": r.width / scale, "h": r.height / scale})
    return words


def _ocr_rapid(img: np.ndarray) -> list[dict]:
    from rapidocr_onnxruntime import RapidOCR
    global _rapid
    try:
        _rapid
    except NameError:
        _rapid = RapidOCR()
    res, _ = _rapid(np.ascontiguousarray(img[:, :, :3][:, :, ::-1]))
    words = []
    for li, (box, text, _conf) in enumerate(res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        parts = text.split()
        x0, x1 = min(xs), max(xs)
        step = (x1 - x0) / max(1, len(text))
        pos = 0
        for p in parts or [text]:
            i = text.find(p, pos)
            words.append({"text": p, "line": li, "x": x0 + i * step, "y": min(ys),
                          "w": len(p) * step, "h": max(ys) - min(ys)})
            pos = i + len(p)
    return words


_OCR_PKGS = ("winrt-runtime winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging "
             "winrt-Windows.Storage.Streams winrt-Windows.Foundation "
             "winrt-Windows.Foundation.Collections winrt-Windows.Globalization")


class OcrMissing(RuntimeError):
    def __init__(self, cause):
        super().__init__(
            "Reading the screen needs Windows OCR, which isn't installed "
            f"({cause}). Run this once, then restart Habibi: "
            f"\"{sys.executable}\" -m pip install {_OCR_PKGS}")


def ocr(area: str = "screen") -> tuple[list[dict], int, int]:
    safety.guard_foreground()
    img, ox, oy = _grab(area)
    with _engine_lock:
        try:
            words = _ocr_windows(img)
        except Exception as e_win:
            try:
                words = _ocr_rapid(img)
            except Exception:
                raise OcrMissing(e_win)
    return words, ox, oy


# ── matching ─────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def find_text(words: list[dict], query: str) -> list[dict]:
    """Every place `query` appears (consecutive words on one line)."""
    q = _norm(query).split(" ")
    if not q or q == [""]:
        return []
    hits = []
    by_line: dict[int, list[dict]] = {}
    for w in words:
        by_line.setdefault(w["line"], []).append(w)
    for line in by_line.values():
        toks = [_norm(w["text"]) for w in line]
        for i in range(len(line)):
            if len(q) == 1:
                ok = q[0] in toks[i]
            else:
                ok = i + len(q) <= len(line) and toks[i].endswith(q[0]) and \
                    all(toks[i + k] == q[k] for k in range(1, len(q) - 1)) and \
                    toks[i + len(q) - 1].startswith(q[-1])
            if ok:
                span = line[i:i + len(q)]
                x0 = min(w["x"] for w in span)
                y0 = min(w["y"] for w in span)
                x1 = max(w["x"] + w["w"] for w in span)
                y1 = max(w["y"] + w["h"] for w in span)
                exact = " ".join(toks[i:i + len(q)]) == " ".join(q)
                hits.append({"text": " ".join(w["text"] for w in span),
                             "x": (x0 + x1) / 2, "y": (y0 + y1) / 2, "exact": exact})
    if not hits:                      # OCR slips ('Sumbit', 'L0gin'): accept close matches
        import difflib
        want = " ".join(q)
        for line in by_line.values():
            toks = [_norm(w["text"]) for w in line]
            for i in range(len(line)):
                j = i + len(q)
                if j > len(line):
                    break
                got = " ".join(toks[i:j])
                if len(want) >= 4 and difflib.SequenceMatcher(None, got, want).ratio() >= 0.8:
                    span = line[i:j]
                    x0 = min(w["x"] for w in span); y0 = min(w["y"] for w in span)
                    x1 = max(w["x"] + w["w"] for w in span); y1 = max(w["y"] + w["h"] for w in span)
                    hits.append({"text": " ".join(w["text"] for w in span),
                                 "x": (x0 + x1) / 2, "y": (y0 + y1) / 2, "exact": False})
    hits.sort(key=lambda h: (not h["exact"], h["y"], h["x"]))
    return hits


def _lines_text(words: list[dict]) -> str:
    lines: dict[int, list[str]] = {}
    for w in words:
        lines.setdefault(w["line"], []).append(w["text"])
    return "\n".join(" ".join(v) for _, v in sorted(lines.items()))


# ── tool ─────────────────────────────────────────────────────────────────────

def screen_text(parameters: dict, player=None) -> str:
    err = A.windows_only()
    if err:
        return err
    action = str(parameters.get("action", "read"))
    # Default: just the app you're using. Reading the whole desktop (Habibi's HUD,
    # other windows) is what made it pick the wrong text in busy screens.
    area = "screen" if str(parameters.get("area", "") or "").lower() in ("screen", "all", "desktop") else "window"
    text = str(parameters.get("text", ""))
    try:
        if action == "read":
            words, _, _ = ocr(area)
            out = _lines_text(words)
            return ("Text on screen:\n" + out[:3500]) if out else "I can't read any text on the screen."
        if action in ("find", "click_text"):
            if not text:
                return "Tell me which text to look for."
            words, ox, oy = ocr(area)
            hits = find_text(words, text)
            if not hits:
                return (f"'{text}' is not visible on the screen right now. Use action read to see "
                        f"what is there.")
            if action == "find":
                return "Found:\n" + "\n".join(
                    f"{i + 1}. '{h['text']}' at ({int(h['x'] + ox)}, {int(h['y'] + oy)})"
                    for i, h in enumerate(hits[:15]))
            n = max(1, int(parameters.get("occurrence", 1) or 1))
            if n > len(hits):
                return f"'{text}' appears only {len(hits)} time(s)."
            h = hits[n - 1]
            x, y = int(h["x"] + ox + int(parameters.get("offset_x", 0) or 0)), \
                int(h["y"] + oy + int(parameters.get("offset_y", 0) or 0))
            kind = "destructive" if A.is_destructive(h["text"]) else "input"

            def _do() -> str:
                before = A.snapshot()
                A.click_at(x, y, "right" if parameters.get("right") else "left",
                           bool(parameters.get("double", False)))
                return f"Clicked '{h['text']}' at ({x}, {y}). " + A.describe_change(before)

            return safety.guarded(kind, f"Click '{h['text']}'", "on the screen", _do)
        if action == "wait_for":
            timeout = min(30.0, float(parameters.get("timeout", 10) or 10))
            end = time.time() + timeout
            while time.time() < end:
                safety.check_stop()
                words, ox, oy = ocr(area)
                hits = find_text(words, text)
                if hits:
                    return f"'{text}' appeared on the screen."
                time.sleep(0.8)
            return f"'{text}' did not appear within {int(timeout)} seconds."
        return "Unknown action. Use read, find, click_text or wait_for."
    except PermissionError as e:
        return str(e)


PLUGIN = {
    "name": "screen_text",
    "description": (
        "Read the text that is visible on the screen and click text EXACTLY where it is (OCR "
        "with real word positions). Use for web pages, games, images and anything ui_control "
        "can't find. Actions: read(area) — all text on screen or in the front window; "
        "find(text) — where a text appears; click_text(text, occurrence, double, right, "
        "offset_x, offset_y) — click it (offsets click next to the text, e.g. a box beside a "
        "label); wait_for(text, timeout) — wait until it appears. area: window (default) | screen."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "read | find | click_text | wait_for"},
            "text": {"type": "STRING", "description": "The visible text to find/click, e.g. 'Downloads'"},
            "area": {"type": "STRING", "description": "window (default: the app you are using) or screen (all monitors)"},
            "occurrence": {"type": "INTEGER", "description": "Which match to click if the text appears several times (1 = first, top-to-bottom)"},
            "double": {"type": "BOOLEAN", "description": "Double-click"},
            "right": {"type": "BOOLEAN", "description": "Right-click"},
            "offset_x": {"type": "INTEGER", "description": "Pixels to the right (+) or left (-) of the text"},
            "offset_y": {"type": "INTEGER", "description": "Pixels below (+) or above (-) the text"},
            "timeout": {"type": "NUMBER", "description": "wait_for: seconds (max 30)"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    """Plugin entry point (switch this tool off in ⚙ PLUGINS)."""
    try:
        return screen_text(parameters or {}, player=player)
    except OcrMissing as e:
        return str(e)
