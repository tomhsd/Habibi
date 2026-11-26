"""Screen-fit selftest: the Habibi window must fit the monitor it opens on and
keep its rails, cards and footer on screen from small laptops to big monitors.

Runs the real MainWindow offscreen at common Windows sizes (in logical pixels,
i.e. after Windows scaling) and checks that nothing is pushed off screen.
Saves screen_fit_selftest_<W>x<H>.png for each size."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import ui  # noqa: E402
from PyQt6.QtWidgets import QApplication, QScrollArea  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail="") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILS.append(name)


# Logical sizes = screen free area at a given resolution + Windows scaling.
SIZES = [
    (780, 520),     # smallest supported
    (1024, 600),    # 1280x720 @125%
    (1093, 582),    # 1366x768 @125%
    (1280, 680),    # 1920x1080 @150%
    (1366, 728),    # 1366x768 @100%
    (1536, 816),    # 1920x1080 @125%
    (1920, 1040),   # 1920x1080 @100%
    (2560, 1400),   # 2560x1440 @100%
]

app = QApplication(sys.argv)
if sys.platform == "win32":
    # Qt's offscreen platform on Windows has no system fonts and falls back to
    # one ~1.7x wider than Consolas; load the fonts a real Windows desktop uses.
    from PyQt6.QtGui import QFontDatabase
    for fn in ("consola.ttf", "consolab.ttf", "segoeui.ttf", "segoeuib.ttf",
               "seguiemj.ttf", "seguisym.ttf", "cour.ttf", "courbd.ttf"):
        fp = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / fn
        if fp.exists():
            QFontDatabase.addApplicationFont(str(fp))
win = ui.MainWindow("")
from PyQt6.QtGui import QFontInfo, QFontMetrics  # noqa: E402
_f = ui._mono(10, True)
print(f"font: {QFontInfo(_f).family()}  dpi={app.primaryScreen().logicalDotsPerInch()}  "
      f"'MMMMMMMMMM' bold10={QFontMetrics(_f).horizontalAdvance('MMMMMMMMMM')}px")


def widest(w, depth=0, out=None, lim=140):
    """The widgets that set a rail's minimum width (printed when it clips)."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QWidget
    out = [] if out is None else out
    for c in w.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
        m = c.minimumSizeHint().width()
        if m >= lim and c.isVisibleTo(win):
            t = c.text() if hasattr(c, "text") and callable(c.text) else ""
            out.append(f"{'  ' * depth}{type(c).__name__} {m}px {str(t)[:30]!r}")
            widest(c, depth + 1, out, lim)
    return out
avail = app.primaryScreen().availableGeometry()
check("opens inside the screen's free area",
      win.width() <= avail.width() and win.height() <= avail.height(),
      f"window {win.width()}x{win.height()}, screen {avail.width()}x{avail.height()}")
check("opens maximized only when the normal size doesn't fit",
      win._start_maximized == (avail.width() < ui._DEFAULT_W + 20 or avail.height() < ui._DEFAULT_H + 20))
win.show()
if win._overlay:
    win._overlay.hide()
app.processEvents()

left_sa = win._left_panel.findChild(QScrollArea)
for W, H in SIZES:
    win.resize(W, H)
    for _ in range(3):
        app.processEvents()
    tag = f"{W}x{H}"
    mh = win.minimumSizeHint()
    check(f"{tag}: layout fits the window", mh.width() <= W and mh.height() <= H,
          f"needs {mh.width()}x{mh.height()}")
    if mh.width() > W and tag in ("780x520", "1280x680"):
        lay = win.centralWidget().layout()
        for i in range(lay.count()):
            it = lay.itemAt(i)
            print(f"      row {i}: min {it.minimumSize().width()}px")
        print("\n".join("      " + x for x in widest(lay.itemAt(0).widget(), lim=40)))
    for name, sa in (("left rail", left_sa), ("right rail", win._right_panel)):
        need = sa.widget().minimumSizeHint().width()
        room = sa.width() - 5          # minus the thin scrollbar
        check(f"{tag}: {name} cards not clipped", need <= room, f"cards {need}px, rail {room}px")
        if need > room and tag == "1093x582":
            print("\n".join("      " + x for x in widest(sa.widget())))
    inp = win._input.mapTo(win, win._input.rect().bottomRight())
    check(f"{tag}: chat box on screen", inp.x() <= W and inp.y() <= H, f"{inp.x()},{inp.y()}")
    c = win._center_split
    check(f"{tag}: reactor has room", c.width() >= 200 and c.height() >= 200, f"{c.width()}x{c.height()}")
    if W >= 1536:
        gap = W - win._left_panel.width() - win._right_panel.width() - c.width()
        check(f"{tag}: no empty gaps", gap <= 60, f"{gap}px of margins")
    win.grab().save(str(ROOT / f"screen_fit_selftest_{tag}.png"))

# A plugin adds a wide card after start-up (like Markets): the rail must grow.
from PyQt6.QtWidgets import QLabel  # noqa: E402
win.resize(1920, 1040)
app.processEvents()
wide = QLabel("X")
wide.setMinimumWidth(left_sa.width() + 60)
left_sa.widget().layout().insertWidget(1, wide)
for _ in range(5):
    app.processEvents()
need = left_sa.widget().minimumSizeHint().width()
check("rail grows when a plugin adds a wider card", need <= left_sa.width() - 5,
      f"cards {need}px, rail {left_sa.width() - 5}px")
check("footer status shows all states",
      win._status_strip.width() >= win._status_strip.needed_width(),
      f"{win._status_strip.width()} < {win._status_strip.needed_width()}")

print("\nRESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
