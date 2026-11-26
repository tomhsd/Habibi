"""
Draw on the screen (plugin).

A see-through drawing layer goes over the whole screen, so you can paint,
sketch and mark up anything you see. A small toolbar floats on top:

  ✏ pen · 🖍 highlighter · ⌫ eraser · ╱ line · ▭ box · ◯ circle · ➚ arrow
  colours (+ 🎨 any colour) · brush size · ↶ undo · ↷ redo · 🗑 clear
  ⬜ whiteboard (white / black page for sketching) · 🖱 click-through
  (ink stays, mouse goes to your apps) · ⇆ next screen · 💾 save · ✕ close

Start it by voice ("start drawing", "draw in red", "open a whiteboard"), with
Ctrl+Alt+D anywhere in Windows, or turn the whole plugin off in ⚙ PLUGINS.
Pen tablets draw thinner or thicker with pressure. Right mouse button erases.

Keys while drawing: Esc close · Ctrl+Z undo · Ctrl+Y redo · Ctrl+S save ·
P pen · H highlighter · E eraser · L line · R box · O circle · A arrow ·
B whiteboard · [ ] brush size · 1–9 colours · Delete clear.

Saved pictures go to Pictures\\Habibi Drawings (and to the clipboard). Closing
keeps the drawing, so "start drawing" brings it back; "clear" wipes it.
Ctrl+Alt+Q (emergency stop) always closes the drawing layer.
"""
from __future__ import annotations

import math
import sys
import threading
import time
from pathlib import Path

PLUGIN = {
    "name": "screen_draw",
    "description": (
        "Draw, paint and sketch directly on top of the screen with a see-through drawing layer "
        "and a floating toolbar (pen, highlighter, eraser, line, box, circle, arrow, colours, "
        "brush size, undo, redo, clear, whiteboard, save). Use for 'start drawing', 'let me draw "
        "on the screen', 'open a whiteboard / sketch pad', 'draw in red', 'bigger brush', "
        "'undo that', 'clear the drawing', 'save my drawing', 'stop drawing'. "
        "action=start|stop|toggle|clear|undo|redo|save|set|status. Optional: tool "
        "(pen|highlighter|eraser|line|rectangle|ellipse|arrow), color (name or #hex), size "
        "(1-80 px), board (off|white|black for a blank sketch page), click_through (true = ink "
        "stays but the mouse works the apps underneath), save_mode (screen = drawing over the "
        "screen picture, ink = drawing only). Not for taking plain screenshots or screen recording."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start, stop, toggle, clear, undo, redo, save, set or status"},
            "tool": {"type": "STRING", "description": "pen, highlighter, eraser, line, rectangle, ellipse or arrow"},
            "color": {"type": "STRING", "description": "colour name (red, blue, …) or #rrggbb"},
            "size": {"type": "NUMBER", "description": "brush size in pixels, 1–80"},
            "board": {"type": "STRING", "description": "off (draw over the screen), white or black (blank page)"},
            "click_through": {"type": "BOOLEAN", "description": "true: the mouse goes to the apps under the drawing"},
            "save_mode": {"type": "STRING", "description": "screen (default) or ink"},
        },
        "required": [],
    },
}

HOTKEY = "Ctrl+Alt+D"
TOOLS = ("pen", "highlighter", "eraser", "line", "rectangle", "ellipse", "arrow")
_TOOL_ALIASES = {
    "pencil": "pen", "brush": "pen", "paint": "pen", "draw": "pen", "marker": "highlighter",
    "highlight": "highlighter", "rubber": "eraser", "erase": "eraser", "straight": "line",
    "rect": "rectangle", "box": "rectangle", "square": "rectangle", "circle": "ellipse",
    "oval": "ellipse", "round": "ellipse", "arrows": "arrow",
}
PALETTE = ["#ff3355", "#ff8800", "#ffdd00", "#22dd66", "#00d4ff",
           "#3366ff", "#bb44ff", "#ffffff", "#111111"]
_COLOUR_WORDS = {
    # English extras Qt doesn't know + Bengali
    "sky": "#55ccff", "skyblue": "#55ccff", "light blue": "#55ccff", "dark blue": "#1133aa",
    "dark green": "#117733", "light green": "#88ee88", "violet": "#8844ff",
    "লাল": "red", "নীল": "blue", "সবুজ": "green", "হলুদ": "yellow", "কালো": "black",
    "সাদা": "white", "কমলা": "orange", "বেগুনি": "purple", "গোলাপি": "pink", "ধূসর": "gray",
    "আকাশী": "#55ccff", "বাদামি": "brown",
}
MIN_SIZE, MAX_SIZE = 1, 80

_ctl = None              # the DrawController (lives on the Qt thread)
_hotkey_started = False


# ── small helpers (no Qt needed) ──────────────────────────────────────────────
def parse_colour(text):
    """'red', 'Dark Blue', 'লাল', '#f00', 'ff0000' → '#rrggbb', or None."""
    from PyQt6.QtGui import QColor
    t = str(text or "").strip().lower()
    if not t:
        return None
    t = _COLOUR_WORDS.get(t, t)
    if not t.startswith("#") and len(t) in (3, 6) and all(c in "0123456789abcdef" for c in t):
        t = "#" + t
    c = QColor(t)
    if not c.isValid():
        c = QColor(t.replace(" ", ""))
    return c.name() if c.isValid() else None


def parse_tool(text):
    t = str(text or "").strip().lower()
    t = _TOOL_ALIASES.get(t, t)
    return t if t in TOOLS else None


def drawings_folder() -> Path:
    home = Path.home()
    pics = home / "Pictures"
    if sys.platform == "win32":
        try:                                  # OneDrive-moved Pictures folder
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x27, None, 0, buf) == 0 and buf.value:
                pics = Path(buf.value)
        except Exception:
            pass
    if not pics.exists():
        pics = home
    return pics / "Habibi Drawings"


def _enabled() -> bool:
    try:
        from memory.config_manager import get_plugin_enabled
        return bool(get_plugin_enabled("screen_draw"))
    except Exception:
        return True


# ── the Qt side ───────────────────────────────────────────────────────────────
def _build_classes():
    from PyQt6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, Qt, pyqtSignal
    from PyQt6.QtGui import (QColor, QCursor, QGuiApplication, QImage, QKeySequence, QPainter,
                             QPainterPath, QPen, QPixmap, QPolygonF, QShortcut)
    from PyQt6.QtWidgets import (QApplication, QColorDialog, QFrame, QHBoxLayout, QLabel,
                                 QPushButton, QSlider, QToolButton, QVBoxLayout, QWidget)

    try:
        import ui as U
        TH = {"bg": U.C.PANEL, "card": U.C.CARD, "hi": U.C.CARD_HI, "border": U.C.BORDER_B,
              "pri": U.C.PRI, "text": U.C.TEXT, "dim": U.C.TEXT_DIM}
    except Exception:
        TH = {"bg": "#010d14", "card": "#02121c", "hi": "#04202e", "border": "#1a5c7a",
              "pri": "#00d4ff", "text": "#8ffcff", "dim": "#3a8a9a"}

    class Stroke:
        __slots__ = ("tool", "color", "size", "pts")

        def __init__(self, tool, color, size, pt=None):
            self.tool, self.color, self.size = tool, QColor(color), float(size)
            self.pts = [pt] if pt is not None else []      # [(QPointF, pressure 0..1)]

        def bounds(self) -> QRectF:
            if not self.pts:
                return QRectF()
            xs = [p.x() for p, _ in self.pts]
            ys = [p.y() for p, _ in self.pts]
            pad = self.size * 3 + 20
            return QRectF(min(xs) - pad, min(ys) - pad, max(xs) - min(xs) + 2 * pad,
                          max(ys) - min(ys) + 2 * pad)

    def smooth_path(pts) -> QPainterPath:
        path = QPainterPath(pts[0])
        if len(pts) == 1:
            path.lineTo(pts[0] + QPointF(0.01, 0.01))
            return path
        if len(pts) == 2:
            path.lineTo(pts[1])
            return path
        for i in range(1, len(pts) - 1):              # quadratic through midpoints = smooth ink
            mid = (pts[i] + pts[i + 1]) / 2
            path.quadTo(pts[i], mid)
        path.lineTo(pts[-1])
        return path

    def draw_stroke(p: QPainter, s: Stroke) -> None:
        if not s.pts:
            return
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pts = [q for q, _ in s.pts]
        col = QColor(s.color)
        w = s.size
        if s.tool == "eraser":
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            col = QColor(0, 0, 0, 255)
            w = s.size * 3
        elif s.tool == "highlighter":
            col.setAlpha(95)
            w = s.size * 3
        pen = QPen(col, w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if s.tool == "pen" and len(s.pts) > 1 and any(abs(pr - 1.0) > 0.02 for _, pr in s.pts):
            for (a, pa), (b, pb) in zip(s.pts, s.pts[1:]):     # pressure: width per segment
                pen.setWidthF(max(0.6, w * (0.25 + 0.75 * (pa + pb) / 2)))
                p.setPen(pen)
                p.drawLine(a, b)
        elif s.tool in ("pen", "highlighter", "eraser"):
            p.setPen(pen)
            p.drawPath(smooth_path(pts))
        else:
            a, b = pts[0], pts[-1]
            p.setPen(pen)
            if s.tool == "line":
                p.drawLine(a, b)
            elif s.tool == "rectangle":
                p.drawRect(QRectF(a, b).normalized())
            elif s.tool == "ellipse":
                p.drawEllipse(QRectF(a, b).normalized())
            elif s.tool == "arrow":
                p.drawLine(a, b)
                ang = math.atan2(b.y() - a.y(), b.x() - a.x())
                head = max(14.0, w * 4)
                l = QPointF(b.x() - head * math.cos(ang - 0.45), b.y() - head * math.sin(ang - 0.45))
                r = QPointF(b.x() - head * math.cos(ang + 0.45), b.y() - head * math.sin(ang + 0.45))
                p.setBrush(col)
                p.drawPolygon(QPolygonF([b, l, r]))
        p.restore()

    # ── the see-through canvas ───────────────────────────────────────────────
    class Canvas(QWidget):
        def __init__(self, ctl):
            super().__init__(None)
            self.ctl = ctl
            self.setWindowTitle("Habibi drawing")
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            self.setMouseTracking(True)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.click_through = False
            self._apply_flags()
            self.history: list = []        # Stroke, or the string "clear"
            self.redo_stack: list = []
            self.layer: QImage | None = None
            self.cur: Stroke | None = None
            self._live: QImage | None = None   # eraser preview
            self._tablet = False
            self.setCursor(Qt.CursorShape.CrossCursor)

        def _apply_flags(self):
            f = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.Tool)
            if self.click_through:
                f |= Qt.WindowType.WindowTransparentForInput
            self.setWindowFlags(f)

        def set_click_through(self, on: bool):
            on = bool(on)
            if on == self.click_through:
                return
            self.click_through = on
            vis = self.isVisible()
            self._apply_flags()
            if vis:
                self.show()

        # layer image at the screen's real pixel size, so ink is sharp
        def _ensure_layer(self):
            dpr = self.devicePixelRatioF()
            size = self.size() * dpr
            if self.layer is not None and self.layer.size() == size \
                    and abs(self.layer.devicePixelRatio() - dpr) < 0.01:
                return
            self.layer = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
            self.layer.setDevicePixelRatio(dpr)
            self.rerender()

        def rerender(self):
            if self.layer is None:
                return
            self.layer.fill(Qt.GlobalColor.transparent)
            p = QPainter(self.layer)
            for s in self.history:
                if s == "clear":
                    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
                    p.fillRect(QRectF(0, 0, self.width(), self.height()), Qt.GlobalColor.transparent)
                    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                else:
                    draw_stroke(p, s)
            p.end()
            self.update()

        def ink_strokes(self) -> int:
            n = 0
            for s in self.history:
                n = 0 if s == "clear" else n + 1
            return n

        # ── painting ─────────────────────────────────────────────────────
        def paintEvent(self, ev):
            self._ensure_layer()
            p = QPainter(self)
            board = self.ctl.board
            if board == "white":
                p.fillRect(self.rect(), QColor("#ffffff"))
            elif board == "black":
                p.fillRect(self.rect(), QColor("#101418"))
            else:
                # alpha 1: invisible, but Windows still sends the mouse to us
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
                p.fillRect(ev.rect(), QColor(0, 0, 0, 0 if self.click_through else 1))
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            if self.cur is not None and self.cur.tool == "eraser" and self._live is not None:
                p.drawImage(QPointF(0, 0), self._live)
            else:
                p.drawImage(QPointF(0, 0), self.layer)
                if self.cur is not None:
                    draw_stroke(p, self.cur)
            if not self.click_through and self.ctl.tool in ("pen", "highlighter", "eraser"):
                pos = self.mapFromGlobal(QCursor.pos())       # brush-size ring
                if self.rect().contains(pos):
                    r = self.ctl.size * (3 if self.ctl.tool != "pen" else 1) / 2 + 1
                    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                    p.setPen(QPen(QColor(128, 128, 128, 200), 1))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawEllipse(QPointF(pos), r, r)
            p.end()

        # ── input ────────────────────────────────────────────────────────
        def _begin(self, pos: QPointF, pressure: float, erase: bool):
            self._ensure_layer()
            tool = "eraser" if erase else self.ctl.tool
            self.cur = Stroke(tool, self.ctl.color, self.ctl.size, (pos, pressure))
            if tool == "eraser":
                self._live = self.layer.copy()
                self._erase_seg(pos, pos)
            self.update()

        def _erase_seg(self, a, b):
            p = QPainter(self._live)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.setPen(QPen(QColor(0, 0, 0), self.cur.size * 3, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            if a == b:
                p.drawPoint(a)
            else:
                p.drawLine(a, b)
            p.end()

        def _extend(self, pos: QPointF, pressure: float, shift: bool):
            s = self.cur
            if s is None:
                return
            if s.tool in ("line", "rectangle", "ellipse", "arrow"):
                a = s.pts[0][0]
                if shift:                       # Shift: straight / square / circle
                    dx, dy = pos.x() - a.x(), pos.y() - a.y()
                    if s.tool in ("rectangle", "ellipse"):
                        m = max(abs(dx), abs(dy))
                        pos = QPointF(a.x() + math.copysign(m, dx), a.y() + math.copysign(m, dy))
                    else:
                        ang = round(math.atan2(dy, dx) / (math.pi / 4)) * (math.pi / 4)
                        d = math.hypot(dx, dy)
                        pos = QPointF(a.x() + d * math.cos(ang), a.y() + d * math.sin(ang))
                old = s.bounds()
                s.pts = [s.pts[0], (pos, pressure)]
                self.update(old.united(s.bounds()).toAlignedRect())
                return
            last = s.pts[-1][0]
            if (pos - last).manhattanLength() < 1.2:
                return
            s.pts.append((pos, pressure))
            if s.tool == "eraser":
                self._erase_seg(last, pos)
            pad = s.size * 3 + 6
            self.update(QRectF(last, pos).normalized().adjusted(-pad, -pad, pad, pad).toAlignedRect())

        def _finish(self):
            s = self.cur
            if s is None:
                return
            self.cur = None
            self._live = None
            if s.pts:
                self.history.append(s)
                self.redo_stack.clear()
                p = QPainter(self.layer)
                draw_stroke(p, s)
                p.end()
            self.update()
            self.ctl.changed()

        def mousePressEvent(self, ev):
            if self._tablet:
                return
            if ev.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
                self._begin(ev.position(), 1.0, ev.button() == Qt.MouseButton.RightButton)

        def mouseMoveEvent(self, ev):
            if self._tablet:
                return
            if self.cur is not None:
                self._extend(ev.position(), 1.0,
                             bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            else:
                self._ring_update(ev.position())

        def mouseReleaseEvent(self, ev):
            if self._tablet:
                return
            self._finish()

        def _ring_update(self, pos):
            # repaint only around the brush ring, not the whole screen
            r = int(self.ctl.size * 3 / 2) + 4
            new = QRect(int(pos.x()) - r, int(pos.y()) - r, 2 * r, 2 * r)
            old = getattr(self, "_ring_rect", None)
            self._ring_rect = new
            self.update(new.united(old) if old is not None else new)

        def tabletEvent(self, ev):
            t = ev.type()
            pr = max(0.05, min(1.0, ev.pressure() or 1.0))
            erase = ev.pointerType().name.lower() == "eraser"
            if t == ev.Type.TabletPress:
                self._tablet = True
                self._begin(ev.position(), pr, erase)
            elif t == ev.Type.TabletMove and self.cur is not None:
                self._extend(ev.position(), pr,
                             bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            elif t == ev.Type.TabletRelease:
                self._finish()
                self._tablet = False
            ev.accept()

        def keyPressEvent(self, ev):
            k = ev.key()
            ctrl = bool(ev.modifiers() & Qt.KeyboardModifier.ControlModifier)
            shift = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            c = self.ctl
            if k == Qt.Key.Key_Escape:
                c.stop()
            elif ctrl and k == Qt.Key.Key_Z:
                c.redo() if shift else c.undo()
            elif ctrl and k == Qt.Key.Key_Y:
                c.redo()
            elif ctrl and k == Qt.Key.Key_S:
                c.save_and_tell()
            elif k == Qt.Key.Key_Delete:
                c.clear()
            elif k == Qt.Key.Key_BracketLeft:
                c.set_size(c.size - (1 if c.size <= 6 else 3))
            elif k == Qt.Key.Key_BracketRight:
                c.set_size(c.size + (1 if c.size < 6 else 3))
            elif not ctrl and Qt.Key.Key_1 <= k <= Qt.Key.Key_9:
                c.set_colour(PALETTE[k - Qt.Key.Key_1])
            elif not ctrl:
                tool = {Qt.Key.Key_P: "pen", Qt.Key.Key_H: "highlighter", Qt.Key.Key_E: "eraser",
                        Qt.Key.Key_L: "line", Qt.Key.Key_R: "rectangle", Qt.Key.Key_O: "ellipse",
                        Qt.Key.Key_A: "arrow"}.get(k)
                if tool:
                    c.set_tool(tool)
                elif k == Qt.Key.Key_B:
                    c.cycle_board()
                else:
                    super().keyPressEvent(ev)
            else:
                super().keyPressEvent(ev)

    # ── the floating toolbar ─────────────────────────────────────────────────
    class Toolbar(QFrame):
        def __init__(self, ctl, owner):
            # owned by the canvas → Windows always keeps it above the drawing
            super().__init__(owner, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                             | Qt.WindowType.WindowStaysOnTopHint)
            self.ctl = ctl
            self.setObjectName("drawBar")
            self.setStyleSheet(f"""
                #drawBar {{ background: {TH['bg']}; border: 1px solid {TH['border']}; border-radius: 10px; }}
                QToolButton {{ background: {TH['card']}; color: {TH['text']}; border: 1px solid transparent;
                               border-radius: 6px; font-size: 15px; min-width: 30px; min-height: 30px; }}
                QToolButton:hover {{ background: {TH['hi']}; }}
                QToolButton:checked {{ border: 1px solid {TH['pri']}; background: {TH['hi']}; }}
                QLabel {{ color: {TH['dim']}; font-size: 11px; }}
                QSlider::groove:horizontal {{ height: 4px; background: {TH['card']}; border-radius: 2px; }}
                QSlider::handle:horizontal {{ width: 12px; margin: -5px 0; border-radius: 6px; background: {TH['pri']}; }}
            """)
            self._drag = None
            col = QVBoxLayout(self)
            col.setContentsMargins(8, 4, 8, 6)
            col.setSpacing(4)
            grip = QLabel(f"✥  HABIBI DRAW  ·  {HOTKEY} · Esc closes")
            grip.setCursor(Qt.CursorShape.SizeAllCursor)
            grip.setToolTip("Drag here to move the toolbar")
            col.addWidget(grip)
            row1, row2 = QHBoxLayout(), QHBoxLayout()
            for r in (row1, row2):
                r.setSpacing(3)
                col.addLayout(r)

            self.tool_btns = {}
            for tool, icon, tip in (("pen", "✏", "Pen (P)"), ("highlighter", "🖍", "Highlighter (H)"),
                                    ("eraser", "⌫", "Eraser (E) — the right mouse button erases too"),
                                    ("line", "╱", "Straight line (L) — hold Shift for neat angles"),
                                    ("rectangle", "▭", "Box (R) — Shift makes a square"),
                                    ("ellipse", "◯", "Circle / oval (O) — Shift makes a circle"),
                                    ("arrow", "➚", "Arrow (A)")):
                b = self._btn(icon, tip, lambda _=False, t=tool: ctl.set_tool(t), row1, True)
                self.tool_btns[tool] = b
            row1.addSpacing(6)
            self.swatches = []
            for i, hexc in enumerate(PALETTE):
                b = QToolButton()
                b.setCheckable(True)
                b.setToolTip(f"Colour ({i + 1})")
                b.setFixedSize(22, 22)
                b.setStyleSheet(f"QToolButton {{ background: {hexc}; border: 1px solid #556; border-radius: 11px; min-width: 0; min-height: 0; }}"
                                f"QToolButton:checked {{ border: 3px solid {TH['pri']}; }}")
                b.clicked.connect(lambda _=False, c=hexc: ctl.set_colour(c))
                row1.addWidget(b)
                self.swatches.append((hexc, b))
            self._btn("🎨", "Any colour…", self._pick, row1)

            row2.addWidget(QLabel("Size"))
            self.slider = QSlider(Qt.Orientation.Horizontal)
            self.slider.setRange(MIN_SIZE, MAX_SIZE)
            self.slider.setFixedWidth(110)
            self.slider.setToolTip("Brush size  ( [  and  ] )")
            self.slider.valueChanged.connect(lambda v: ctl.set_size(v))
            row2.addWidget(self.slider)
            self.size_lbl = QLabel("")
            self.size_lbl.setFixedWidth(30)
            row2.addWidget(self.size_lbl)
            self._btn("↶", "Undo (Ctrl+Z)", ctl.undo, row2)
            self._btn("↷", "Redo (Ctrl+Y)", ctl.redo, row2)
            self._btn("🗑", "Clear everything (Delete) — Undo brings it back", ctl.clear, row2)
            self.board_btn = self._btn("⬜", "Whiteboard: white page → black page → off (B)", ctl.cycle_board, row2)
            self.through_btn = self._btn("🖱", "Click-through: ink stays, the mouse works your apps. Click again to draw.",
                                         lambda _=False: ctl.set_click_through(not ctl.click_through), row2, True)
            self._btn("⇆", "Move the drawing to the next screen", ctl.next_screen, row2)
            self._btn("💾", "Save picture (Ctrl+S) to Pictures\\Habibi Drawings + clipboard", ctl.save_and_tell, row2)
            self._btn("✕", "Close (Esc). Your drawing is kept until you clear it.", ctl.stop, row2)
            self.status = QLabel("")
            col.addWidget(self.status)
            self.adjustSize()

        def _btn(self, text, tip, fn, row, checkable=False):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda *_: fn())
            row.addWidget(b)
            return b

        def _pick(self):
            c = QColorDialog.getColor(QColor(self.ctl.color), self, "Drawing colour")
            if c.isValid():
                self.ctl.set_colour(c.name())

        def sync(self):
            c = self.ctl
            for t, b in self.tool_btns.items():
                b.setChecked(t == c.tool)
            for hexc, b in self.swatches:
                b.setChecked(hexc.lower() == c.color.lower())
            self.slider.blockSignals(True)
            self.slider.setValue(int(c.size))
            self.slider.blockSignals(False)
            self.size_lbl.setText(f"{int(c.size)}px")
            self.board_btn.setText({"white": "⬜", "black": "⬛"}.get(c.board, "▢"))
            self.through_btn.setChecked(c.click_through)

        def say(self, text):
            self.status.setText(text)

        def mousePressEvent(self, ev):
            if ev.button() == Qt.MouseButton.LeftButton:
                self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

        def mouseMoveEvent(self, ev):
            if self._drag is not None:
                self.move(ev.globalPosition().toPoint() - self._drag)

        def mouseReleaseEvent(self, ev):
            self._drag = None

    # ── controller: one per app, lives on the Qt thread ──────────────────────
    class DrawController(QObject):
        call = pyqtSignal(object)      # thread-safe: hotkey / emergency stop

        def __init__(self, player=None):
            super().__init__()
            self.player = player
            self.tool, self.color, self.size = "pen", PALETTE[0], 4.0
            self.board = "off"
            self.canvas = None
            self.bar = None
            self.screen = None
            self.last_saved = ""
            self.call.connect(lambda fn: fn())

        @property
        def click_through(self) -> bool:
            return bool(self.canvas and self.canvas.click_through)

        def _log(self, msg):
            try:
                if self.player is not None:
                    self.player.write_log(f"HABIBI: {msg}")
            except Exception:
                pass

        def _ensure(self):
            if self.canvas is None:
                self.canvas = Canvas(self)
                self.bar = Toolbar(self, self.canvas)

        def _place(self, screen=None):
            scr = screen or QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            self.screen = scr
            g = scr.geometry()
            self.canvas.setGeometry(g)
            self.bar.adjustSize()
            self.bar.move(g.x() + (g.width() - self.bar.width()) // 2, g.y() + 12)

        def is_open(self) -> bool:
            return bool(self.canvas and self.canvas.isVisible())

        def start(self):
            self._ensure()
            if not self.is_open():
                self._place()
                self.canvas.show()
                self.bar.show()
            self.canvas.raise_()
            self.bar.raise_()
            if not self.canvas.click_through:
                self.canvas.activateWindow()
                self.canvas.setFocus()
            self.bar.sync()
            self.changed()

        def stop(self):
            if self.canvas is not None:
                if self.canvas.cur is not None:
                    self.canvas._finish()
                self.bar.hide()
                self.canvas.hide()

        def toggle(self):
            self.stop() if self.is_open() else self.start()

        def next_screen(self):
            screens = QGuiApplication.screens()
            if self.canvas is None or len(screens) < 2:
                self.say("Only one screen.")
                return
            i = screens.index(self.screen) if self.screen in screens else 0
            self._place(screens[(i + 1) % len(screens)])
            self.canvas.layer = None          # new size → re-render on that screen
            self.canvas.update()

        # settings
        def set_tool(self, t):
            if t in TOOLS:
                self.tool = t
                if self.click_through:
                    self.set_click_through(False)
                self._sync()

        def set_colour(self, hexc):
            self.color = hexc
            if self.tool == "eraser":
                self.tool = "pen"
            self._sync()

        def set_size(self, v):
            self.size = float(max(MIN_SIZE, min(MAX_SIZE, round(float(v)))))
            self._sync()

        def set_board(self, b):
            self.board = b if b in ("white", "black") else "off"
            if self.board != "off" and self.click_through:
                self.set_click_through(False)
            if self.canvas:
                self.canvas.update()
            self._sync()

        def cycle_board(self):
            self.set_board({"off": "white", "white": "black"}.get(self.board, "off"))

        def set_click_through(self, on):
            if self.canvas is None:
                return
            if on and self.board != "off":
                self.board = "off"
            self.canvas.set_click_through(on)
            if self.is_open():
                self.bar.show()
                self.bar.raise_()
                if not on:
                    self.canvas.activateWindow()
                    self.canvas.setFocus()
            self.canvas.update()
            self._sync()
            self.say("Click-through: your mouse works the apps. Click 🖱 to draw again."
                     if on else "Drawing.")

        def _sync(self):
            if self.bar is not None:
                self.bar.sync()
                self.canvas.update()

        def say(self, text):
            if self.bar is not None:
                self.bar.say(text)

        def changed(self):
            if self.canvas is not None:
                n = self.canvas.ink_strokes()
                self.say(f"{n} stroke{'s' if n != 1 else ''}" + (f" · saved {Path(self.last_saved).name}"
                                                                 if self.last_saved else ""))

        # edits
        def undo(self) -> bool:
            c = self.canvas
            if c is None or not c.history:
                return False
            c.redo_stack.append(c.history.pop())
            c.rerender()
            self.changed()
            return True

        def redo(self) -> bool:
            c = self.canvas
            if c is None or not c.redo_stack:
                return False
            c.history.append(c.redo_stack.pop())
            c.rerender()
            self.changed()
            return True

        def clear(self):
            c = self.canvas
            if c is None or c.ink_strokes() == 0:
                return
            c.history.append("clear")
            c.redo_stack.clear()
            c.rerender()
            self.changed()

        # saving
        def render_image(self, mode: str = "screen") -> QImage:
            c = self.canvas
            c._ensure_layer()
            dpr = c.devicePixelRatioF()
            out = QImage(c.layer.size(), QImage.Format.Format_ARGB32_Premultiplied)
            out.setDevicePixelRatio(dpr)
            out.fill(Qt.GlobalColor.transparent)
            p = QPainter(out)
            if self.board == "white":
                p.fillRect(QRectF(0, 0, c.width(), c.height()), QColor("#ffffff"))
            elif self.board == "black":
                p.fillRect(QRectF(0, 0, c.width(), c.height()), QColor("#101418"))
            elif mode != "ink":
                shot = self._grab_under()
                if shot is not None and not shot.isNull():
                    p.drawPixmap(QRectF(0, 0, c.width(), c.height()), shot, QRectF(shot.rect()))
            p.drawImage(QPointF(0, 0), c.layer)
            p.end()
            return out

        def _grab_under(self):
            """Picture of the screen without our layer and toolbar on it."""
            was = self.is_open()
            if was:
                self.bar.hide()
                self.canvas.hide()
                QApplication.processEvents()
                time.sleep(0.25)              # let Windows repaint what was under us
                QApplication.processEvents()
            try:
                g = self.canvas.geometry()
                scr = self.screen or QGuiApplication.primaryScreen()
                sg = scr.geometry()
                return scr.grabWindow(0, g.x() - sg.x(), g.y() - sg.y(), g.width(), g.height())
            except Exception:
                return None
            finally:
                if was:
                    self.canvas.show()
                    self.bar.show()
                    self.bar.raise_()

        def save(self, mode: str = "screen", folder: Path | None = None) -> str:
            if self.canvas is None:
                raise RuntimeError("Nothing drawn yet.")
            img = self.render_image(mode)
            folder = Path(folder) if folder else drawings_folder()
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / time.strftime("drawing-%Y%m%d-%H%M%S.png")
            k = 1
            while path.exists():
                path = folder / time.strftime(f"drawing-%Y%m%d-%H%M%S-{k}.png")
                k += 1
            if not img.save(str(path), "PNG"):
                raise RuntimeError(f"couldn't write {path}")
            try:
                QApplication.clipboard().setImage(img)
            except Exception:
                pass
            self.last_saved = str(path)
            self.changed()
            self._log(f"Drawing saved: {path}")
            return str(path)

        def save_and_tell(self):
            try:
                path = self.save()
                self.say(f"Saved {Path(path).name} · copied to clipboard")
            except Exception as e:
                self.say(f"Couldn't save: {e}")

    return DrawController


_DrawController = None


def _controller(player=None):
    """Create the controller on first use. Must run on the Qt thread."""
    global _ctl, _DrawController
    if _ctl is None:
        if _DrawController is None:
            _DrawController = _build_classes()
        _ctl = _DrawController(player)
        try:
            from core import safety
            safety.on_stop(lambda: _ctl.call.emit(_ctl.stop))   # emergency stop closes the layer
        except Exception:
            pass
    elif player is not None and _ctl.player is None:
        _ctl.player = player
    return _ctl


def _start_hotkey(log=print) -> bool:
    """Ctrl+Alt+D anywhere in Windows opens / closes the drawing layer."""
    global _hotkey_started
    if _hotkey_started or sys.platform != "win32":
        return False
    _hotkey_started = True

    def loop():
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 0xD4A7, 0x1 | 0x2 | 0x4000, ord("D")):
            log(f"SYS: Drawing hotkey {HOTKEY} is used by another app — say 'start drawing' instead.")
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312 and _ctl is not None and _enabled():
                _ctl.call.emit(_ctl.toggle)

    threading.Thread(target=loop, name="DrawHotkey", daemon=True).start()
    return True


def setup_ui(player) -> None:
    """Runs once on the Qt thread: get the controller ready and the hotkey on."""
    _controller(player)

    def log(m):
        try:
            player.write_log(m)
        except Exception:
            print(m)
    _start_hotkey(log)


def _on_ui(player, fn):
    if threading.current_thread() is threading.main_thread() or player is None \
            or not hasattr(player, "run_in_ui"):
        return fn()
    return player.run_in_ui(fn)


def _truthy(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "").strip().lower()
    try:
        return _on_ui(player, lambda: _run(p, action, player))
    except Exception as e:
        return f"Sir, drawing failed: {e}"


def _run(p: dict, action: str, player) -> str:
    c = _controller(player)
    notes = []
    if p.get("tool") not in (None, ""):
        t = parse_tool(p.get("tool"))
        if t is None:
            notes.append(f"I don't have a '{p.get('tool')}' tool")
        else:
            c.set_tool(t)
            notes.append(f"tool {t}")
    if p.get("color") not in (None, ""):
        hexc = parse_colour(p.get("color"))
        if hexc is None:
            notes.append(f"I don't know the colour '{p.get('color')}'")
        else:
            c.set_colour(hexc)
            notes.append(f"colour {p.get('color')}")
    if p.get("size") not in (None, ""):
        try:
            c.set_size(float(p.get("size")))
            notes.append(f"brush {int(c.size)} px")
        except (TypeError, ValueError):
            notes.append("brush size must be a number")
    if p.get("board") not in (None, ""):
        b = str(p.get("board")).strip().lower()
        b = {"whiteboard": "white", "blackboard": "black", "none": "off", "screen": "off",
             "false": "off", "true": "white", "on": "white"}.get(b, b)
        c.set_board(b)
        notes.append("whiteboard " + c.board if c.board != "off" else "drawing over the screen")
    settings_given = bool(notes)
    if not action:
        action = "set" if settings_given else "start"
    if "click_through" in p and p.get("click_through") not in (None, ""):
        if action in ("start", "toggle") and not c.is_open():
            c.start()
        c.set_click_through(_truthy(p.get("click_through")))
        notes.append("click-through on" if c.click_through else "drawing mode")
    extra = (" (" + ", ".join(notes) + ")") if notes else ""

    if action in ("start", "open", "on", "show", "begin", "draw"):
        c.start()
        return (f"Drawing is on{extra}. Draw with the mouse or pen; the toolbar is at the top. "
                f"Esc or {HOTKEY} closes it.")
    if action in ("stop", "close", "off", "hide", "end", "exit"):
        c.stop()
        return "Drawing closed. Your sketch is kept — say 'start drawing' to bring it back."
    if action == "toggle":
        c.toggle()
        return "Drawing is on." if c.is_open() else "Drawing closed."
    if action in ("clear", "wipe", "erase_all", "reset"):
        c.clear()
        return "Cleared the drawing. Say 'undo' to bring it back."
    if action == "undo":
        return "Undone." if c.undo() else "Nothing to undo."
    if action == "redo":
        return "Redone." if c.redo() else "Nothing to redo."
    if action in ("save", "export", "screenshot"):
        if c.canvas is None:
            return "There's no drawing to save yet. Say 'start drawing' first."
        mode = "ink" if str(p.get("save_mode") or "").lower().startswith("ink") else "screen"
        path = c.save(mode)
        return f"Saved your drawing to {path} and copied it to the clipboard."
    if action in ("set", "change"):
        if not c.is_open():
            c.start()
        return ("Done" + extra + ".") if settings_given else "Tell me a tool, colour or size to change."
    # status
    if c.canvas is None:
        return f"Drawing is ready. Say 'start drawing' or press {HOTKEY}."
    return (f"Drawing is {'open' if c.is_open() else 'closed'}: {c.tool}, colour {c.color}, "
            f"{int(c.size)} px, {c.canvas.ink_strokes()} strokes"
            + (", whiteboard " + c.board if c.board != "off" else "")
            + (", click-through" if c.click_through else "") + ".")
