"""
Real test of the screen_draw plugin (draw on the screen).

On Windows it uses the real mouse and keyboard (pyautogui), so it checks what a
person would do: open the layer by voice and by Ctrl+Alt+D, draw, change tool /
colour / size, erase with the right button, undo / redo / clear, whiteboard,
click-through (the mouse reaches the window underneath), save, Esc, and the
emergency stop. Elsewhere it sends the same input as Qt events.

Writes draw_selftest.png (the screen with the drawing on it).
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
WIN = sys.platform == "win32"
if not WIN:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, QPoint, QPointF, Qt, QTimer, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QColor, QGuiApplication, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402
from core.plugin_loader import discover_plugins  # noqa: E402

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)   # the drawing layer is a tool window
results: list[tuple[str, bool, str]] = []


def check(name, ok, info=""):
    results.append((name, bool(ok), str(info)))
    print(("PASS " if ok else "FAIL ") + name + " — " + str(info)[:300], flush=True)


class FakePlayer(QObject):
    _sig = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self._sig.connect(self._do)
        self.logs = []

    def _do(self, job):
        fn, holder, ev = job
        try:
            holder["r"] = fn()
        except Exception as e:
            holder["e"] = e
        ev.set()

    def run_in_ui(self, fn, timeout=15.0):
        if threading.current_thread() is threading.main_thread():
            return fn()
        holder, ev = {}, threading.Event()
        self._sig.emit((fn, holder, ev))
        if not ev.wait(timeout):
            raise TimeoutError("UI didn't answer")
        if "e" in holder:
            raise holder["e"]
        return holder.get("r")

    def write_log(self, m):
        self.logs.append(m)


player = FakePlayer()
ui = player.run_in_ui
reg = discover_plugins(ROOT / "plugins", set(), logger=print)
plug = lambda **p: reg.run("screen_draw", p, player=player)
mod = sys.modules.get("plugins.screen_draw")

if WIN:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.02


def wait(cond, secs=5.0):
    end = time.time() + secs
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def ctl():
    return mod._ctl


def to_global(x, y) -> QPoint:
    g = ctl().canvas.geometry()
    return QPoint(g.x() + x, g.y() + y)


def drag(points, button="left"):
    """Drag through canvas-local points with the real mouse (Windows) or Qt events."""
    if WIN:
        # pyautogui works in physical pixels
        dpr = ui(lambda: ctl().canvas.devicePixelRatioF())
        gp = [ui(lambda p=p: to_global(*p)) for p in points]
        phys = [(int(q.x() * dpr), int(q.y() * dpr)) for q in gp]
        pyautogui.moveTo(*phys[0])
        time.sleep(0.1)
        pyautogui.mouseDown(button=button)
        for x, y in phys[1:]:
            pyautogui.moveTo(x, y, duration=0.12)
        pyautogui.mouseUp(button=button)
        time.sleep(0.3)
        return
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QMouseEvent
    btn = Qt.MouseButton.LeftButton if button == "left" else Qt.MouseButton.RightButton

    def send():
        c = ctl().canvas
        def ev(t, p, b, bs):
            lp = QPointF(*p)
            QApplication.sendEvent(c, QMouseEvent(t, lp, QPointF(c.mapToGlobal(lp.toPoint())), b, bs,
                                                  Qt.KeyboardModifier.NoModifier))
        ev(QEvent.Type.MouseButtonPress, points[0], btn, btn)
        a = points[0]
        for b in points[1:]:
            for i in range(1, 11):
                ev(QEvent.Type.MouseMove, (a[0] + (b[0] - a[0]) * i / 10, a[1] + (b[1] - a[1]) * i / 10),
                   Qt.MouseButton.NoButton, btn)
            a = b
        ev(QEvent.Type.MouseButtonRelease, points[-1], btn, Qt.MouseButton.NoButton)
    ui(send)


def key(k, mods=Qt.KeyboardModifier.NoModifier, win_keys=None):
    if WIN and win_keys:
        pyautogui.hotkey(*win_keys)
        time.sleep(0.3)
        return
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QKeyEvent
    ui(lambda: QApplication.sendEvent(ctl().canvas, QKeyEvent(QEvent.Type.KeyPress, k, mods)))


def ink(x, y) -> QColor:
    def f():
        c = ctl().canvas
        c._ensure_layer()
        d = c.devicePixelRatioF()
        return QColor(c.layer.pixelColor(int(x * d), int(y * d)))
    return ui(f)


def strokes():
    return ui(lambda: ctl().canvas.ink_strokes())


finished = {"ok": False}


def script():
    time.sleep(0.8)
    try:
        _script()
        finished["ok"] = True
    except Exception as e:
        import traceback
        traceback.print_exc()
        check("test ran to the end", False, e)
    QTimer.singleShot(0, app.quit)


def _script():
    check("plugin loaded", reg.has("screen_draw"), [r.name + (":" + r.error if r.error else "") for r in reg._all_records])
    reg.run_ui_setup(player)
    check("setup_ui made the controller", ctl() is not None)

    # colour / tool words
    pc = mod.parse_colour
    check("colour words", pc("red") == "#ff0000" and pc("লাল") == "#ff0000" and pc("#00f") == "#0000ff"
          and pc("Dark Blue") == "#1133aa" and pc("ff8800") == "#ff8800" and pc("nonsense-colour") is None,
          [pc("red"), pc("লাল"), pc("#00f"), pc("Dark Blue"), pc("nonsense-colour")])
    check("tool words", mod.parse_tool("circle") == "ellipse" and mod.parse_tool("rubber") == "eraser"
          and mod.parse_tool("box") == "rectangle" and mod.parse_tool("spaceship") is None)

    # 1. voice: start drawing
    r = plug(action="start")
    ok = wait(lambda: ui(lambda: ctl().is_open()))
    geo = ui(lambda: (ctl().canvas.geometry(), ctl().screen.geometry(), ctl().bar.isVisible(),
                      bool(ctl().canvas.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)))
    check("'start drawing' opens the layer over the whole screen", ok and geo[0] == geo[1] and geo[2] and geo[3], (r, geo))
    time.sleep(0.6)
    if WIN:
        import ctypes
        u = ctypes.windll.user32
        hwnd = int(ui(lambda: ctl().canvas.winId()))
        ex = u.GetWindowLongW(hwnd, -20)
        check("layer is a topmost window (Windows)", bool(ex & 0x8), hex(ex))
        bar_h = int(ui(lambda: ctl().bar.winId()))
        check("toolbar is owned by the layer, so it stays above it", u.GetWindow(bar_h, 4) == hwnd,
              (u.GetWindow(bar_h, 4), hwnd))

    # 2. draw a red pen stroke
    plug(color="red", size=8, tool="pen")
    drag([(200, 300), (400, 320), (600, 300)])
    c = ink(400, 320)
    check("pen draws red ink where the mouse went", strokes() == 1 and c.alpha() > 200 and c.red() > 200 and c.green() < 80,
          (strokes(), c.name(), c.alpha()))
    check("no ink away from the stroke", ink(400, 600).alpha() == 0)

    # 3. blue box with the rectangle tool
    plug(tool="box", color="blue", size=6)
    drag([(300, 450), (500, 600)])
    c = ink(400, 450)
    check("box tool draws a blue rectangle edge", strokes() == 2 and c.blue() > 200 and c.red() < 60 and ink(400, 525).alpha() == 0,
          (strokes(), c.name(), ink(400, 525).alpha()))

    # 4. arrow + highlighter + ellipse + line just draw something
    for t, pts, probe in (("arrow", [(600, 150), (760, 200)], (680, 175)),
                          ("highlighter", [(600, 380), (760, 380)], (680, 380)),
                          ("line", [(600, 500), (760, 500)], (680, 500))):
        plug(tool=t, color="green", size=6)
        drag(pts)
        px = ink(*probe)
        check(f"{t} draws", px.alpha() > 40, (px.name(), px.alpha()))
    check("highlighter is see-through", 40 < ink(680, 380).alpha() < 200, ink(680, 380).alpha())

    # 5. right mouse button erases
    plug(tool="pen", color="red", size=8)
    n = strokes()
    drag([(400, 280), (400, 360)], button="right")
    check("right mouse button erases the ink under it", ink(400, 320).alpha() == 0 and ink(250, 305).alpha() > 200,
          (ink(400, 320).alpha(), ink(250, 305).alpha()))

    # 6. undo / redo
    r = plug(action="undo")
    check("undo brings the erased ink back", ink(400, 320).alpha() > 200 and strokes() == n, (r, strokes()))
    plug(action="redo")
    check("redo erases it again", ink(400, 320).alpha() == 0)
    key(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier, ("ctrl", "z"))
    check("Ctrl+Z undoes", wait(lambda: ink(400, 320).alpha() > 200, 2))

    # 7. brush size keys
    s0 = ui(lambda: ctl().size)
    key(Qt.Key.Key_BracketRight, win_keys=("]",))
    check("] makes the brush bigger", wait(lambda: ui(lambda: ctl().size) > s0, 2), (s0, ui(lambda: ctl().size)))
    key(Qt.Key.Key_E, win_keys=("e",))
    check("E picks the eraser", wait(lambda: ui(lambda: ctl().tool) == "eraser", 2))
    key(Qt.Key.Key_P, win_keys=("p",))
    check("P picks the pen", wait(lambda: ui(lambda: ctl().tool) == "pen", 2))

    # 8. save (screen + ink) — goes to a temp folder in the test
    out = Path(tempfile.mkdtemp())
    path = ui(lambda: ctl().save("screen", out))
    img = QImage(path)
    d = ui(lambda: ctl().canvas.devicePixelRatioF())
    px = img.pixelColor(int(250 * d), int(305 * d)) if not img.isNull() else QColor()
    corner = img.pixelColor(int(5 * d), int(img.height() - 5)) if not img.isNull() else QColor()
    check("save writes a PNG with the drawing on the screen picture",
          Path(path).exists() and px.red() > 200 and px.green() < 80 and (corner.alpha() == 255 or not WIN),
          (path, img.size(), px.name(), corner.alpha()))
    check("still open after saving", ui(lambda: ctl().is_open()))
    clip = ui(lambda: QApplication.clipboard().image())
    check("saved picture is on the clipboard", not clip.isNull(), clip.size())
    ink_only = QImage(ui(lambda: ctl().save("ink", out)))
    check("ink-only save keeps the background see-through",
          ink_only.pixelColor(int(5 * d), int(ink_only.height() - 5)).alpha() == 0)
    r = plug(action="save")
    check("voice save goes to Pictures\\Habibi Drawings", "Habibi Drawings" in r and Path(ui(lambda: ctl().last_saved)).exists(), r)
    try:
        Path(ui(lambda: ctl().last_saved)).unlink()
    except Exception:
        pass

    if WIN:
        try:
            shot = QGuiApplication.primaryScreen().grabWindow(0)
            shot.save(str(ROOT / "draw_selftest.png"))
        except Exception:
            pass

    # 9. whiteboard
    r = plug(board="white")
    wb = ui(lambda: ctl().render_image("screen"))
    p0 = wb.pixelColor(int(5 * d), int(wb.height() - 5))
    check("whiteboard gives a white page", p0.name() == "#ffffff" and ui(lambda: ctl().board) == "white", (r, p0.name()))
    if WIN:
        time.sleep(0.4)
        shot = ui(lambda: ctl().screen.grabWindow(0)).toImage()
        sp = shot.pixelColor(shot.width() // 2, shot.height() - 20)
        check("whiteboard is white on the real screen", sp.lightness() > 240, sp.name())
        try:
            shot.save(str(ROOT / "draw_whiteboard_selftest.png"))
        except Exception:
            pass
    plug(board="black")
    check("blackboard", ui(lambda: ctl().board) == "black")
    plug(board="off")

    # 10. click-through: the mouse reaches the window underneath
    if WIN:
        clicks = {"n": 0}

        def make_target():
            w = QLabel("click target")
            w.setWindowFlags(Qt.WindowType.FramelessWindowHint)
            sg = ctl().screen.geometry()
            w.setGeometry(sg.x() + 50, sg.y() + sg.height() - 250, 200, 150)
            w.mousePressEvent = lambda e: clicks.__setitem__("n", clicks["n"] + 1)
            w.show()
            ctl().canvas.raise_()
            ctl().bar.raise_()
            return w
        tgt = ui(make_target)
        time.sleep(0.6)
        tg = ui(lambda: tgt.geometry().center())
        dpr = ui(lambda: ctl().canvas.devicePixelRatioF())
        pyautogui.click(int(tg.x() * dpr), int(tg.y() * dpr))
        time.sleep(0.5)
        blocked = clicks["n"] == 0
        plug(click_through=True)
        time.sleep(0.8)
        import ctypes
        ex = ctypes.windll.user32.GetWindowLongW(int(ui(lambda: ctl().canvas.winId())), -20)
        pyautogui.click(int(tg.x() * dpr), int(tg.y() * dpr))
        time.sleep(0.6)
        check("drawing mode catches the mouse; click-through lets it reach the app underneath",
              blocked and clicks["n"] >= 1 and bool(ex & 0x20), (blocked, clicks["n"], hex(ex)))
        check("ink stays visible in click-through", ui(lambda: ctl().is_open()) and ink(250, 305).alpha() > 200)
        check("toolbar still shows in click-through", ui(lambda: ctl().bar.isVisible()))
        plug(click_through=False)
        time.sleep(0.5)
        n0 = strokes()
        drag([(200, 700), (400, 720)])
        check("drawing works again after click-through", strokes() == n0 + 1, (n0, strokes()))
        ui(lambda: tgt.close())
    else:
        plug(click_through=True)
        check("click-through sets the pass-through flag",
              bool(ui(lambda: ctl().canvas.windowFlags()) & Qt.WindowType.WindowTransparentForInput))
        plug(click_through=False)

    # 11. clear + undo
    n = strokes()
    r = plug(action="clear")
    check("clear wipes the drawing", strokes() == 0 and ink(250, 305).alpha() == 0, r)
    plug(action="undo")
    check("undo after clear brings it all back", strokes() == n and ink(250, 305).alpha() > 200)

    # 12. Esc closes, start again keeps the sketch
    ui(lambda: (ctl().canvas.activateWindow(), ctl().canvas.setFocus()))
    time.sleep(0.3)
    key(Qt.Key.Key_Escape, win_keys=("esc",))
    check("Esc closes the drawing layer", wait(lambda: not ui(lambda: ctl().is_open()), 3))
    plug(action="start")
    check("start again keeps the sketch", wait(lambda: ui(lambda: ctl().is_open()), 3) and strokes() == n)

    # 13. global hotkey Ctrl+Alt+D
    if WIN:
        pyautogui.hotkey("ctrl", "alt", "d")
        a = wait(lambda: not ui(lambda: ctl().is_open()), 3)
        pyautogui.hotkey("ctrl", "alt", "d")
        b = wait(lambda: ui(lambda: ctl().is_open()), 3)
        check("Ctrl+Alt+D closes and opens it from anywhere", a and b, (a, b))

    # 14. emergency stop closes it
    from core import safety
    safety.emergency_stop()
    check("emergency stop closes the drawing layer", wait(lambda: not ui(lambda: ctl().is_open()), 3))
    time.sleep(4.3)
    r = plug(action="status")
    check("status answers", "closed" in r, r)

    # 15. turned off in ⚙ PLUGINS → no tool, run refuses
    from memory import config_manager as cm
    try:
        cm.save_plugin_enabled("screen_draw", False)
        off_ok = "disabled" in plug(action="start") and not any(
            d["name"] == "screen_draw" for d in reg.get_tool_declarations())
    finally:
        cm.save_plugin_enabled("screen_draw", True)
    check("switching the plugin off in ⚙ PLUGINS removes it", off_ok)


threading.Thread(target=script, daemon=True).start()
QTimer.singleShot(240_000, app.quit)
app.exec()
if not finished["ok"]:
    check("test ran to the end", False, "stopped early")
fails = [r for r in results if not r[1]]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails or not results else 0)
