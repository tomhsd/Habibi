"""
Ambient light for the media player (plugin).

While a video plays on the video page (YouTube or a local file), the frame
around the video glows with the picture's colour, and Habibi's accent colour
follows it. Brightness follows the picture (dark scene = dimmer light) and
gently pulses with the beat of the audio. Your own colour comes back when the
video stops or is hidden.

Switch it with the 💡 button on the MEDIA PLAYER card, by voice ("ambient light
off"), or turn the whole plugin off in ⚙ PLUGINS.
"""
from __future__ import annotations

import colorsys
import time

PLUGIN = {
    "name": "ambient_light",
    "description": (
        "Ambient light while a video plays in Habibi's media player (YouTube or local): the "
        "frame around the video and the interface colour follow the picture's colour, dim with "
        "dark scenes and pulse softly with the music. action=on|off|status."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "on, off or status"},
        },
    },
}

_amb = None          # the running AmbientLight


def _cfg_enabled() -> bool:
    try:
        from memory.config_manager import get_plugin_config
        return bool(get_plugin_config("ambient_light").get("enabled", True))
    except Exception:
        return True


def _cfg_save(on: bool) -> None:
    try:
        from memory.config_manager import save_plugin_config
        save_plugin_config("ambient_light", {"enabled": bool(on)})
    except Exception:
        pass


def frame_colour(img) -> tuple[tuple[int, int, int], float] | None:
    """(colour, brightness 0..1) of a QImage. The colour favours the edges and
    saturated pixels, like the light a TV throws on the wall behind it."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage
    if img is None or img.isNull() or img.width() < 4 or img.height() < 4:
        return None
    small = img.convertToFormat(QImage.Format.Format_RGB32).scaled(
        32, 18, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    w, h = small.width(), small.height()
    tot_w = r_s = g_s = b_s = 0.0
    luma = 0.0
    for y in range(h):
        for x in range(w):
            c = small.pixelColor(x, y)
            r, g, b = c.red(), c.green(), c.blue()
            lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
            luma += lum
            mx, mn = max(r, g, b), min(r, g, b)
            sat = (mx - mn) / mx if mx else 0.0
            edge = 2.0 if (x < 5 or x >= w - 5 or y < 3 or y >= h - 3) else 1.0
            wgt = edge * (0.15 + sat) * (0.1 + lum)
            r_s += r * wgt; g_s += g * wgt; b_s += b * wgt; tot_w += wgt
    luma /= (w * h)
    if tot_w <= 0:
        return (0, 0, 0), luma
    return (int(r_s / tot_w), int(g_s / tot_w), int(b_s / tot_w)), luma


class AmbientLight:
    def __init__(self, player):
        from PyQt6.QtCore import QTimer
        self.player = player
        self.win = player._win
        self.e = player.media_engine
        self.enabled = _cfg_enabled()
        self.active = False
        self.col = (0.0, 0.0, 0.0)          # smoothed colour
        self.light = 0.0                    # smoothed brightness
        self._local_img = None
        self._local_t = 0.0
        self._dark_since = None
        self._thumb: dict[str, tuple] = {}
        self._last_full = 0.0
        self._sheet_palette = None
        self._page = None
        try:                                # local files: frames straight from the video sink
            sink = self.win._video.videoSink()
            sink.videoFrameChanged.connect(self._on_local_frame)
        except Exception:
            pass
        self.t = QTimer(self.win)
        self.t.timeout.connect(self._tick)
        self.t.start(90)

    # ── picture ─────────────────────────────────────────────────────────────
    def _on_local_frame(self, frame) -> None:
        now = time.time()
        if not self.active or now - self._local_t < 0.15:
            return
        self._local_t = now
        try:
            self._local_img = frame.toImage()
        except Exception:
            self._local_img = None

    def _sample(self):
        if self.e.is_external():
            view = getattr(self.e.src, "view", None)
            got = frame_colour(view.grab().toImage()) if view is not None else None
            # Some PCs hand back a black grab of hardware-decoded video: then use
            # the video's thumbnail colour so the light still matches the video.
            if got is None or got[1] < 0.015:
                if self._dark_since is None:
                    self._dark_since = time.time()
                if time.time() - self._dark_since > 2.5:
                    t = self._thumb_colour()
                    if t is not None:
                        return t
            else:
                self._dark_since = None
            return got
        return frame_colour(self._local_img)

    def _thumb_colour(self):
        vid = str((getattr(self.e.src, "s", {}) or {}).get("id") or "")
        if not vid:
            return None
        if vid not in self._thumb:
            self._thumb[vid] = None
            import threading

            def fetch():
                try:
                    import urllib.request
                    from PyQt6.QtGui import QImage
                    data = urllib.request.urlopen(
                        f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg", timeout=8).read()
                    img = QImage(); img.loadFromData(data)
                    self._thumb[vid] = frame_colour(img)
                except Exception:
                    pass
            threading.Thread(target=fetch, daemon=True).start()
        return self._thumb.get(vid)

    # ── light ───────────────────────────────────────────────────────────────
    def _video_page(self):
        if self._page is None:
            try:
                self._page = self.win._video_stack.parentWidget()
                self._orig_sheet = self._page.styleSheet()
                from PyQt6.QtCore import Qt
                self._page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
                if not self._page.objectName():
                    self._page.setObjectName("habibiVideoPage")
            except Exception:
                self._page = None
        return self._page

    def _set_frame(self, hexc: str | None, width: int) -> None:
        page = self._video_page()
        if page is None:
            return
        lay = page.layout()
        if lay is not None:
            lay.setContentsMargins(width, width, width, 0)
        if hexc is None:
            page.setStyleSheet(self._orig_sheet)
        else:                       # only the page itself, not the video or the buttons
            n = page.objectName()
            page.setStyleSheet(f"#{n} {{ background: {hexc}; }} #{n} QWidget {{ background: #000; }}")

    def _tick(self) -> None:
        import ui as U
        e = self.e
        want = (self.enabled and e.playing() and e.has_video()
                and bool(getattr(e, "video_on_screen", False)))
        if not want:
            if self.active:
                self.active = False
                self._set_frame(None, 0)
                try:
                    saved = (U._read_full_config().get("ui_color") or "").strip() or U.DEFAULT_UI_COLOR
                    if U.apply_ui_accent(saved) and self._sheet_palette is not None:
                        new = U.current_palette()
                        U.retheme_all_widgets(self._sheet_palette, new)
                except Exception:
                    pass
            return
        if not self.active:
            self.active = True
            self._sheet_palette = U.current_palette()
        now = time.time()
        got = None
        if now - getattr(self, "_last_sample", 0.0) > 0.25:   # grabbing the picture 4x a second is plenty
            self._last_sample = now
            got = self._sample()
        bass, _mid, _treble, overall = getattr(e, "levels", (0.0, 0.0, 0.0, 0.0))
        if got is not None:
            (r, g, b), luma = got
            k = 0.25                                         # smooth, no flicker
            self.col = tuple(c0 + (c1 - c0) * k for c0, c1 in zip(self.col, (r, g, b)))
            self.light += (luma - self.light) * k
        r, g, b = self.col
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        # dim with dark scenes, pulse softly on the beat
        pulse = min(1.0, 0.12 * overall + 0.22 * max(0.0, bass - 0.35))
        val = max(0.18, min(1.0, 0.35 + 0.65 * self.light + pulse))
        fr, fg, fb = colorsys.hsv_to_rgb(h, min(1.0, s * 1.15), val * 0.55)   # frame: softer glow
        self._set_frame("#{:02x}{:02x}{:02x}".format(int(fr * 255), int(fg * 255), int(fb * 255)),
                        8 + int(6 * pulse))
        if s > 0.12:                                          # grey scenes keep the current accent
            ar, ag, ab = colorsys.hsv_to_rgb(h, max(0.55, s), max(0.6, val))
            hexc = "#{:02x}{:02x}{:02x}".format(int(ar * 255), int(ag * 255), int(ab * 255))
            now = time.time()
            full = now - self._last_full > 0.6
            if U.apply_ui_accent(hexc) and full:
                self._last_full = now
                new = U.current_palette()
                U.retheme_all_widgets(self._sheet_palette, new)
                self._sheet_palette = new
        try:
            self.win.hud.set_audio_level(min(1.0, overall * 1.1 + bass * 0.4))
        except Exception:
            pass

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        _cfg_save(self.enabled)
        if getattr(self, "btn", None) is not None:
            self.btn.setChecked(self.enabled)


def setup_ui(player) -> None:
    """Runs once on the Qt thread: start the ambient light and add the 💡 button."""
    global _amb
    if _amb is not None or getattr(player, "media_engine", None) is None:
        return
    if getattr(player._win, "_video_stack", None) is None:
        return
    _amb = AmbientLight(player)
    card = getattr(player, "media_card", None)
    if card is not None:
        b = card.add_source_button("💡", "Ambient light: the room around the video follows its colour", lambda: None, 34)
        b.setCheckable(True)
        b.setChecked(_amb.enabled)
        b.toggled.connect(_amb.set_enabled)
        _amb.btn = b


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str((parameters or {}).get("action", "status") or "status").lower().strip()
    if _amb is None:
        if player is not None and hasattr(player, "run_in_ui"):
            try:
                player.run_in_ui(lambda: setup_ui(player))
            except Exception as e:
                return f"Ambient light couldn't start: {e}"
        if _amb is None:
            return "Ambient light needs Habibi's video player."
    if action in ("on", "enable", "start"):
        player.run_in_ui(lambda: _amb.set_enabled(True))
        return "Ambient light is on. It works while a video is showing."
    if action in ("off", "disable", "stop"):
        player.run_in_ui(lambda: _amb.set_enabled(False))
        return "Ambient light is off."
    return ("Ambient light is " + ("on" if _amb.enabled else "off")
            + (", glowing with the video now." if _amb.active else "."))
