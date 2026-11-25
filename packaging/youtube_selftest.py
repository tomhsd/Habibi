"""
Real Windows test of the youtube_player plugin (YouTube inside the media player):
plays a video and a playlist in the official embedded player, controls them
from the sidebar commands, and checks the RGB colours follow YouTube audio
when the video page is hidden.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import ui  # noqa: E402  (loads Qt WebEngine before the QApplication exists)
from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
from core.plugin_loader import discover_plugins  # noqa: E402

app = QApplication(sys.argv)
win = ui.MainWindow("")
win.resize(1400, 860)
win.show()
wrap = ui.HabibiUI.__new__(ui.HabibiUI)
wrap._win = win
results: list[tuple[str, bool, str]] = []
pri0 = ui.C.PRI


def check(name, ok, info=""):
    results.append((name, bool(ok), str(info)))
    print(("PASS " if ok else "FAIL ") + name + " — " + str(info)[:300], flush=True)


def wait(cond, secs):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.5)
    return False


reg = discover_plugins(ROOT / "plugins", set(), logger=print)
plug = lambda **p: reg.run("youtube_player", p, player=wrap)


def script():
    time.sleep(1)
    check("plugin loaded", reg.has("youtube_player"), [r.name for r in reg._all_records])
    reg.run_ui_setup(wrap)
    yt_mod = sys.modules.get("plugins.youtube_player")
    yt = getattr(yt_mod, "_yt", None)
    check("plugin attached to the media player (setup_ui)", yt is not None and win._media.src is yt, "")
    if yt is None:
        QTimer.singleShot(0, app.quit); return
    check("▶ YT button on the card", any(b.text() == "▶ YT" for b in win._media_card.findChildren(type(win._media_card.b_play))), "")
    check("link parsing", yt_mod.parse_link("https://youtu.be/M7lc1UVf-VE") == {"id": "M7lc1UVf-VE"}, "")
    r = plug(action="play", link="https://www.youtube.com/watch?v=M7lc1UVf-VE")   # YouTube's IFrame API demo video
    check("plugin play command", "Playing the YouTube video" in r, r)
    ok = wait(lambda: yt.playing() and yt.s.get("dur", 0) > 0, 40)
    check("video plays in Habibi", ok, yt.s)
    check("title known", bool(yt.title()), yt.title())
    check("video page shown", win._hud_cam_stack.currentIndex() == 2, win._hud_cam_stack.currentIndex())
    wrap.media("seek", seconds=30)
    ok = wait(lambda: yt.s.get("pos", 0) >= 29, 15)
    check("seek", ok, yt.s.get("pos"))
    r = wrap.media("volume", volume=30)
    check("volume", "30" in r, r)
    wrap.media("pause")
    ok = wait(lambda: yt.paused(), 10)
    check("pause", ok, wrap.media("status"))
    win.grab().save("youtube_selftest_video.png")

    r = plug(action="play", query="lofi hip hop", playlist=True)
    check("playlist search + play", "Playing the YouTube playlist" in r, r)
    if "playlist" in r:
        ok = wait(lambda: len(yt.s.get("list") or []) > 1 and yt.playing(), 40)
        check("playlist imported", ok, f"{len(yt.s.get('list') or [])} videos")
        i0 = yt.index()
        wrap.media("next")
        ok = wait(lambda: yt.index() == i0 + 1, 20)
        check("next in playlist", ok, f"{i0} → {yt.index()}")
        time.sleep(3)
        check("playlist titles listed", win._media_card.listw.count() > 1, win._media_card.listw.count())
        r = wrap.media("list")
        check("list command", "YouTube playlist" in r, r[:200])
        check("profile saved for sign-in", (ROOT / "config" / "youtube_profile").exists(), "")
        # hide the video → RGB follows the YouTube audio
        wait(lambda: yt.playing(), 15)
        wrap.run_in_ui(lambda: win._media_card.b_vid.setChecked(False))
        print("state before RGB check:", yt.s.get("state"), "video on screen:", win._media.video_on_screen)
        seen = set()
        end = time.time() + 4
        while time.time() < end:
            seen.add(ui.C.PRI); time.sleep(0.1)
        check("RGB with YouTube audio (video hidden)", len(seen) > 5, f"{len(seen)} colours")
        win.grab().save("youtube_selftest_rgb.png")
        wrap.media("stop")
        time.sleep(1.5)
        check("own colour back after stop", ui.C.PRI == pri0, ui.C.PRI)
    r = wrap.media("play", paths=[])
    QTimer.singleShot(0, app.quit)


threading.Thread(target=script, daemon=True).start()
QTimer.singleShot(240000, app.quit)
app.exec()
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} YouTube checks passed")
sys.exit(1 if failed or not results else 0)
