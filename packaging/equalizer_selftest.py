"""
Test of the equalizer plugin (plugins/equalizer.py).

Part 1 (no window): the sound maths — each band/shelf changes the right
frequencies by the right amount, beat makes kick hits stronger without
boosting the steady bass, balance/width, the limiter stops clipping, settings
can change mid-song without clicks, and it's fast enough. Voice commands.
Part 2 (real Habibi window, offscreen): 🎚 EQ button + panel on the media card,
a generated song plays through the equalizer, the plain output is silenced
while the EQ plays, and flat/off hands the sound back. YouTube: the Web Audio
script is installed in the YouTube player and is valid JavaScript.

  python packaging/equalizer_selftest.py          (both parts)
  python packaging/equalizer_selftest.py --dsp    (part 1 only)
"""
from __future__ import annotations

import os
import shutil
import subprocess
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

results: list[tuple[str, bool, str]] = []


def check(name, ok, info=""):
    results.append((name, bool(ok), str(info)))
    print(("PASS " if ok else "FAIL ") + name + " — " + str(info)[:300], flush=True)


def info(name, text):
    print("INFO " + name + " — " + str(text)[:300], flush=True)


# ══════════════════════════════════════════════════════════════════════════════
#  Part 1: maths + voice
# ══════════════════════════════════════════════════════════════════════════════
import importlib.util  # noqa: E402


def load(stem):
    name = f"plugins.{stem}"
    spec = importlib.util.spec_from_file_location(name, ROOT / "plugins" / f"{stem}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


D = load("_eq_dsp")
RATE = 48000


def tone(f, sec=1.0, amp=0.1, rate=RATE):
    t = np.arange(int(rate * sec)) / rate
    x = amp * np.sin(2 * np.pi * f * t)
    return np.stack([x, x], 1).astype(np.float32)


def run_blocks(p, x, block=1024, rate=RATE):
    return np.concatenate([p.process(x[i:i + block], rate) for i in range(0, len(x), block)])


def gain_db(settings, f, rate=RATE):
    p = D.Processor(D.normalise(settings))
    x = tone(f, 1.0, rate=rate)
    y = run_blocks(p, x, rate=rate)
    a = np.sqrt(np.mean(y[rate // 2:, 0] ** 2))
    b = np.sqrt(np.mean(x[rate // 2:, 0] ** 2))
    return 20 * np.log10(a / b)


def part1():
    s = {"bands": [0, 0, 0, 0, 0, 6, 0, 0, 0, 0]}
    g1k, g100, g8k = gain_db(s, 1000), gain_db(s, 100), gain_db(s, 8000)
    check("1 kHz band +6 dB boosts 1 kHz by 6 dB only", abs(g1k - 6) < 0.3 and abs(g100) < 0.5 and abs(g8k) < 0.5,
          f"1k {g1k:+.2f}, 100 Hz {g100:+.2f}, 8k {g8k:+.2f}")
    for i, f in enumerate(D.FREQS[:-1]):
        b = [0] * 10; b[i] = -9
        g = gain_db({"bands": b}, f)
        if abs(g + 9) > 0.6:
            check(f"band {D.BAND_LABELS[i]} cut", False, f"{g:+.2f} dB"); break
    else:
        check("every band cuts its own frequency (-9 dB)", True, "31 Hz … 8 kHz")
    g = gain_db({"bands": [0] * 9 + [8]}, 16000, rate=48000)
    check("16 kHz band", abs(g - 8) < 1.0, f"{g:+.2f} dB")
    gb, gm = gain_db({"bass": 10, "limiter": False}, 40), gain_db({"bass": 10}, 3000)
    check("BASS +10 dB lifts the lows, not the mids", gb > 8.5 and abs(gm) < 0.5, f"40 Hz {gb:+.2f}, 3 kHz {gm:+.2f}")
    gt = gain_db({"treble": -8}, 14000)
    check("TREBLE -8 dB", -8.5 < gt < -6.5, f"14 kHz {gt:+.2f}")
    gp = gain_db({"preamp": -6}, 1000)
    check("PREAMP -6 dB", abs(gp + 6) < 0.2, f"{gp:+.2f}")
    gl = gain_db({"loudness": True, "limiter": False}, 40)
    check("LOUDNESS lifts the lows", gl > 4, f"40 Hz {gl:+.2f}")
    g441 = gain_db({"bands": [0, 0, 0, 0, 0, 6, 0, 0, 0, 0]}, 1000, rate=44100)
    check("works at 44.1 kHz too", abs(g441 - 6) < 0.3, f"{g441:+.2f}")

    # beat: kick hits on top of steady bass
    n = RATE * 4
    x = 0.05 * np.sin(2 * np.pi * 60 * np.arange(n) / RATE)
    for b in np.arange(0, 4, 0.5):
        i, m = int(b * RATE), int(0.15 * RATE)
        x[i:i + m] += 0.35 * np.sin(2 * np.pi * 60 * np.arange(m) / RATE) * np.exp(-np.arange(m) / (0.05 * RATE))
    X = np.stack([x, x], 1).astype(np.float32)
    out = {}
    for beat in (0, 100):
        y = run_blocks(D.Processor({"beat": beat, "limiter": False}), X)
        hit = np.abs(y[2 * RATE: 2 * RATE + 2400, 0]).max()
        body = np.sqrt(np.mean(y[int(2.3 * RATE): int(2.45 * RATE), 0] ** 2))
        out[beat] = (hit, body)
    hit_up = 20 * np.log10(out[100][0] / out[0][0])
    body_up = 20 * np.log10(out[100][1] / out[0][1])
    check("BEAT 100% punches up kick hits much more than steady bass", hit_up > 4 and hit_up - body_up > 2.5,
          f"hits {hit_up:+.1f} dB, steady bass {body_up:+.1f} dB")

    p = D.Processor({"balance": -100})
    y = p.process(tone(440), RATE)
    check("BALANCE full left silences the right", np.abs(y[:, 1]).max() < 1e-4 and np.abs(y[:, 0]).max() > 0.09, "")
    x = tone(440); x[:, 1] = 0
    y = D.Processor({"width": 0}).process(x, RATE)
    check("WIDTH 0 = mono", np.allclose(y[:, 0], y[:, 1], atol=1e-4), "")
    y = D.Processor({"width": 200}).process(np.stack([x[:, 0], 0.5 * x[:, 0]], 1), RATE)
    check("WIDTH 200 widens", np.abs(y[:, 0]).max() > np.abs(y[:, 1]).max() * 3, "")
    mono = D.Processor({"bands": [3] * 10}).process(tone(440)[:, :1], RATE)
    check("mono audio works", mono.shape[1] == 1 and np.abs(mono).max() > 0.1, mono.shape)

    p = D.Processor({"bands": [12] * 10, "preamp": 12, "bass": 15})
    y = run_blocks(p, tone(440, 1, 0.5), block=1000)
    check("LIMITER keeps huge boosts below clipping", np.abs(y).max() <= 0.971, f"peak {np.abs(y).max():.3f}")

    p = D.Processor({"bands": [0, 0, 0, 0, 0, 6, 0, 0, 0, 0]})
    x = tone(440, 1, 0.2); outs = []
    for k, i in enumerate(range(0, len(x), 1024)):
        if k == 20:
            p.set({"bands": [0, 0, 0, 0, 0, -6, 0, 0, 0, 0]})
        outs.append(p.process(x[i:i + 1024], RATE))
    y = np.concatenate(outs)
    step = np.abs(np.diff(y[:, 0])).max()
    check("changing settings mid-song has no click", step < 2 * np.pi * 440 / RATE * 0.2 * 2.2, f"max step {step:.4f}")
    for blk in (1, 17, 333, 4096, 9000):
        y = D.Processor({"bands": [2] * 10}).process(tone(440, blk / RATE + 1e-9)[:blk], RATE)
        if y.shape != (blk, 2):
            check("odd block sizes", False, f"{blk}: {y.shape}"); break
    else:
        check("odd block sizes (1 … 9000 samples)", True, "")

    p = D.Processor({"bands": [3, 2, 1, 0, -1, 0, 1, 2, 3, 4], "beat": 60, "width": 130, "bass": 4})
    blk = tone(440, 0.02)
    t0 = time.perf_counter()
    for _ in range(300):
        p.process(blk, RATE)
    ms = (time.perf_counter() - t0) / 300 * 1000
    check("fast enough (20 ms of audio)", ms < 6, f"{ms:.2f} ms per block")

    # voice
    EQ = load("equalizer")
    tmp = Path(tempfile.mkdtemp()) / "equalizer.json"
    ctl = EQ.Equalizer(None, path=tmp)
    cmd = lambda **p: EQ.command(ctl, p)  # noqa: E731
    r = cmd(action="preset", preset="bass and beat")
    check("voice: 'bass and beat preset'", ctl.settings["preset"] == "Bass & Beat" and ctl.settings["beat"] == 60, r)
    r = cmd(action="preset", preset="hip hop")
    check("voice: preset aliases", ctl.settings["preset"] == "Hip-Hop", r)
    r = cmd(action="bass")
    check("voice: 'more bass' (no number)", ctl.settings["bass"] == 3 and ctl.settings["preset"] == "Custom", r)
    r = cmd(action="bass", value=2, relative=True)
    check("voice: bass relative", ctl.settings["bass"] == 5, r)
    r = cmd(action="band", band="60 hz", gain=4)
    check("voice: '60 hertz +4'", ctl.settings["bands"][1] == 4, r)
    r = cmd(action="band", band="1k", gain=-3)
    check("voice: '1k -3'", ctl.settings["bands"][5] == -3, r)
    r = cmd(action="band", band="3", gain=2)
    check("voice: band number 3", ctl.settings["bands"][2] == 2, r)
    r = cmd(action="beat", value=80)
    check("voice: beat 80", ctl.settings["beat"] == 80, r)
    r = cmd(action="balance", value=-30)
    check("voice: balance left", ctl.settings["balance"] == -30 and "left" in r, r)
    r = cmd(action="width", value=500)
    check("voice: width clamps to 200", ctl.settings["width"] == 200, r)
    r = cmd(action="save_preset", name="car")
    check("voice: save my preset", "car" in ctl.custom and ctl.settings["preset"] == "car", r)
    r = cmd(action="preset", preset="flat")
    r2 = cmd(action="preset", preset="car")
    check("voice: load my preset", ctl.settings["bands"][1] == 4 and ctl.settings["balance"] == -30, r2)
    r = cmd(action="off")
    check("voice: off", not ctl.settings["enabled"] and not ctl.proc.active(), r)
    r = cmd(action="on")
    check("voice: on", ctl.settings["enabled"] and ctl.proc.active(), r)
    r = cmd(action="bands", bands="1,2,3,4,5,6,7,8,9,10")
    check("voice: all 10 bands", ctl.settings["bands"] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], r)
    r = cmd(action="status")
    check("voice: status", "Equalizer on" in r and "Bass" in r, r)
    ctl.save_now()
    ctl2 = EQ.Equalizer(None, path=tmp)
    check("settings and presets are saved", ctl2.settings["bands"][9] == 10 and "car" in ctl2.custom, "")
    r = cmd(action="delete_preset", name="car")
    check("voice: delete my preset", "car" not in ctl.custom, r)
    r = cmd(action="reset")
    check("voice: reset", D.is_neutral(ctl.settings) and not ctl.proc.active(), r)
    r = cmd(action="preset", preset="banana")
    check("voice: unknown preset is explained", "no preset" in r, r)

    node = shutil.which("node")
    if node:
        f = Path(tempfile.mkdtemp()) / "eq.js"
        f.write_text(D.WEB_JS + "\n" + D.web_forward_js(D.normalise({"bass": 4})), encoding="utf-8")
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        check("YouTube Web Audio script is valid JavaScript", r.returncode == 0, r.stderr[:300])
    else:
        info("JavaScript syntax", "node not found, skipped")


# ══════════════════════════════════════════════════════════════════════════════
#  Part 2: in the real window
# ══════════════════════════════════════════════════════════════════════════════

def part2():
    d = Path(tempfile.mkdtemp())
    sr = 44100
    t = np.arange(sr * 8) / sr
    x = 0.25 * np.sin(2 * np.pi * 220 * t) + 0.1 * np.sin(2 * np.pi * 3000 * t)
    for b in np.arange(0, 8, 0.5):
        i, n = int(b * sr), int(0.12 * sr)
        x[i:i + n] += 0.6 * np.sin(2 * np.pi * 55 * np.arange(n) / sr) * np.exp(-np.arange(n) / (0.04 * sr))
    with wave.open(str(d / "eq test song.wav"), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.repeat(np.clip(x, -1, 1)[:, None], 2, 1) * 32767).astype("<i2").tobytes())

    import ui  # noqa: E402
    from PyQt6.QtCore import QTimer
    from PyQt6.QtMultimedia import QMediaDevices
    from PyQt6.QtWidgets import QApplication
    from core.plugin_loader import discover_plugins

    app = QApplication(sys.argv)
    win = ui.MainWindow("")
    win.resize(1400, 1000)
    win.show()
    wrap = ui.HabibiUI.__new__(ui.HabibiUI)
    wrap._win = win
    reg = discover_plugins(ROOT / "plugins", set(), logger=print)
    has_out = bool(QMediaDevices.audioOutputs())
    info("audio outputs", [o.description() for o in QMediaDevices.audioOutputs()] or "none on this machine")

    def wait(cond, secs):
        end = time.time() + secs
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.2)
        return False

    def script():
        time.sleep(1)
        check("plugin loaded", reg.has("equalizer"), [r.name for r in reg._all_records if not r.valid])
        reg.run_ui_setup(wrap)
        eqm = sys.modules.get("plugins.equalizer")
        ctl = getattr(eqm, "_ctl", None)
        e = win._media
        check("equalizer attached to the player", ctl is not None and e.processor is ctl.proc, "")
        if ctl is None:
            QTimer.singleShot(0, app.quit); return
        card = win._media_card
        check("🎚 EQ button on the media card", eqm._btn is not None and eqm._btn.text() == "🎚 EQ", "")
        wrap.run_in_ui(lambda: eqm._btn.setChecked(True))
        check("panel opens in the card", wrap.run_in_ui(lambda: ctl.panel.isVisible()), "")
        wrap.run_in_ui(lambda: ctl.reset())
        # slider → settings
        wrap.run_in_ui(lambda: ctl.panel.band_sl[0].setValue(60))
        check("band slider changes the sound settings", ctl.settings["bands"][0] == 6.0, ctl.settings["bands"])
        wrap.run_in_ui(lambda: ctl.panel.knobs["beat"][0].setValue(50))
        check("BEAT slider", ctl.settings["beat"] == 50, ctl.settings["beat"])
        wrap.run_in_ui(lambda: ctl.reset())
        check("flat = bypassed", e.processing_status() == "off" and abs(e.audio.volume() - 0.7) < 0.01,
              f"{e.processing_status()}, plain volume {e.audio.volume():.2f}")

        r = wrap.media("play", paths=[str(d)])
        check("song plays", "Now playing" in r, r)
        wait(lambda: e.playing(), 5)
        r = reg.run("equalizer", {"action": "preset", "preset": "bass & beat"}, player=wrap)
        check("plugin command through the loader", "Bass & Beat" in r, r)
        calls = [0]
        orig = ctl.proc.process

        def counting(frames, rate):
            calls[0] += 1
            return orig(frames, rate)
        ctl.proc.process = counting
        time.sleep(2.5)
        st = e.processing_status()
        info("sound path", f"{st}; processor calls {calls[0]}; buffer output "
             f"{'yes' if getattr(e, '_abo', None) is not None else 'no (Qt < 6.8)'}")
        if getattr(e, "_abo", None) is None:
            info("local-file EQ", "this Qt gives no decoded audio, so files play without EQ")
        elif not has_out:
            info("local-file EQ", "no audio device here, so the EQ output can't open")
            check("audio reaches the equalizer", calls[0] >= 1, f"{calls[0]} blocks")
            check("no audio device: falls back to the plain player", st.startswith("failed")
                  and e.audio.volume() > 0.5 and e._sink is None, f"{st}, volume {e.audio.volume():.2f}")
        else:
            check("audio goes through the equalizer", calls[0] > 10 and st == "on", f"{st}, {calls[0]} blocks")
            check("plain output silenced while EQ plays", e.audio.volume() == 0.0, e.audio.volume())
            check("EQ output running", e._sink is not None, str(e._sink.state()) if e._sink else "none")
        wrap.run_in_ui(lambda: win.grab().save("equalizer_selftest.png"))
        r = reg.run("equalizer", {"action": "off"}, player=wrap)
        time.sleep(0.3)
        check("EQ off hands the sound back to the plain player",
              e.processing_status() != "on" and e._sink is None and e.audio.volume() > 0.5,
              f"{e.processing_status()}, volume {e.audio.volume():.2f}")
        reg.run("equalizer", {"action": "on"}, player=wrap)
        wrap.media("stop")

        yt = sys.modules.get("plugins.youtube_player")
        src = getattr(yt, "_yt", None) if yt else None
        if src is None:
            info("YouTube", "youtube_player not attached (WebEngine missing?)")
        else:
            time.sleep(0.5)
            names = wrap.run_in_ui(lambda: [s.name() for s in src.profile.scripts().toList()])
            check("Web Audio EQ installed in the YouTube player", "habibi-equalizer" in names, names)
        st = reg.run("equalizer", {"action": "status"}, player=wrap)
        info("status", st)
        QTimer.singleShot(0, app.quit)

    threading.Thread(target=script, daemon=True).start()
    QTimer.singleShot(60000, app.quit)
    app.exec()
    # leave no test settings behind
    try:
        (ROOT / "config" / "equalizer.json").unlink()
    except Exception:
        pass


if __name__ == "__main__":
    part1()
    if "--dsp" not in sys.argv:
        had = (ROOT / "config" / "equalizer.json").exists()
        if had:
            info("existing settings", "config/equalizer.json exists; part 2 would overwrite it, skipped")
        else:
            part2()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} equalizer checks passed")
    sys.exit(1 if failed or not results else 0)
