"""
Real Windows test of Habibi's built-in media player (ui_media.py).
Generates a short song, plays it in the real HUD (offscreen), and checks:
the AI command path, live bass/mid/treble analysis, RGB colour cycling,
pause/next/volume, and that your own colour comes back afterwards.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

d = Path(tempfile.mkdtemp())
sr = 44100
t = np.arange(sr * 8) / sr
x = 0.3 * np.sin(2 * np.pi * 220 * t) + 0.15 * np.sin(2 * np.pi * 3000 * t)
for b in np.arange(0, 8, 0.5):
    i, n = int(b * sr), int(0.12 * sr)
    x[i:i + n] += 0.8 * np.sin(2 * np.pi * 55 * np.arange(n) / sr) * np.exp(-np.arange(n) / (0.04 * sr))
for name in ("habibi test song A.wav", "habibi test song B.wav"):
    with wave.open(str(d / name), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.repeat(np.clip(x, -1, 1)[:, None], 2, 1) * 32767).astype("<i2").tobytes())

import ui  # noqa: E402  (before the QApplication, so Qt WebEngine can load)
from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

win = ui.MainWindow("")
win.resize(1400, 860)
win.show()
wrap = ui.HabibiUI.__new__(ui.HabibiUI)
wrap._win = win
pri0 = ui.C.PRI
results: list[tuple[str, bool, str]] = []
colours: set[str] = set()
bass_max = [0.0]


def check(name, ok, info=""):
    results.append((name, bool(ok), str(info)))
    print(("PASS " if ok else "FAIL ") + name + " — " + str(info)[:300], flush=True)


def sample():
    colours.add(ui.C.PRI)
    if win._media is not None:
        bass_max[0] = max(bass_max[0], win._media.levels[0])


tmr = QTimer(); tmr.timeout.connect(sample); tmr.start(50)


def script():
    time.sleep(0.5)
    check("media player built", win._media is not None and win._media_card is not None, "")
    r = wrap.media("play", paths=[str(d)])
    check("play a folder", "Now playing" in r, r)
    time.sleep(3)
    r = wrap.media("status")
    check("status says playing", r.startswith("Playing audio"), r)
    check("live analysis (bass seen)", bass_max[0] > 0.2, f"max bass {bass_max[0]:.2f}, "
          f"buffer output {'yes' if getattr(win._media, '_abo', None) is not None else 'no'}")
    check("RGB colours cycle with the music", len(colours) > 10, f"{len(colours)} colours in 3 s")
    r = wrap.media("volume", volume=35)
    check("volume", "35" in r, r)
    r = wrap.media("next")
    check("next track", "track 2 of 2" in r, r)
    time.sleep(0.6)
    r = wrap.media("pause")
    check("pause", r.startswith("Paused"), r)
    time.sleep(0.8)
    check("own colour back after pause", ui.C.PRI == pri0, f"{ui.C.PRI} vs {pri0}")
    win.grab().save("media_selftest.png")
    QTimer.singleShot(0, app.quit)


threading.Thread(target=script, daemon=True).start()
QTimer.singleShot(30000, app.quit)
app.exec()
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} media checks passed")
sys.exit(1 if failed or not results else 0)
