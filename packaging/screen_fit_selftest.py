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
win = ui.MainWindow("")
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
    for name, sa in (("left rail", left_sa), ("right rail", win._right_panel)):
        need = sa.widget().minimumSizeHint().width()
        room = sa.width() - 5          # minus the thin scrollbar
        check(f"{tag}: {name} cards not clipped", need <= room, f"cards {need}px, rail {room}px")
    inp = win._input.mapTo(win, win._input.rect().bottomRight())
    check(f"{tag}: chat box on screen", inp.x() <= W and inp.y() <= H, f"{inp.x()},{inp.y()}")
    c = win._center_split
    check(f"{tag}: reactor has room", c.width() >= 200 and c.height() >= 200, f"{c.width()}x{c.height()}")
    if W >= 1536:
        gap = W - win._left_panel.width() - win._right_panel.width() - c.width()
        check(f"{tag}: no empty gaps", gap <= 60, f"{gap}px of margins")
    win.grab().save(str(ROOT / f"screen_fit_selftest_{tag}.png"))

print("\nRESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
