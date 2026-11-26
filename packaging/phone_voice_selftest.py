"""
Self-test for the phone_voice plugin (talk to Habibi from the phone).

Checks, without a phone or a microphone:
  1. the plugin loads and exposes the dashboard_setup / phone_script hooks;
  2. the phone server serves /plugins.js and the /api/phone-voice route
     (login required, too-short and too-long recordings refused, words shown on
     the phone and queued as a command);
  3. Habibi's own answer heard back through the mic is recognised as an echo,
     while short real replies ("yes", "open notepad") are not;
  4. the offline live-mic sentence cutter finds one sentence in speech-like audio;
  5. the phone page's talk-over check ignores Habibi's voice coming back from the
     phone speaker but still notices the user (needs node; skipped if missing);
  6. optional: the real phone page in headless Chromium with a fake microphone —
     the 🎙 HOLD button records, sends, and shows "YOU SAID" (needs playwright).

Run:  python packaging/phone_voice_selftest.py
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def tone(seconds: float, amp: float = 0.3, freq: float = 220.0, rate: int = 16000) -> bytes:
    n = int(seconds * rate)
    return b"".join(struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate)))
                    for i in range(n))


def silence(seconds: float, rate: int = 16000) -> bytes:
    return b"\x00\x00" * int(seconds * rate)


def main() -> int:
    print("phone_voice self-test")

    # 1. plugin loads with its hooks
    from core.plugin_loader import discover_plugins
    reg = discover_plugins(ROOT / "plugins", set(), logger=print)
    rec = reg._plugins.get("phone_voice")
    check("plugin loads", rec is not None and rec.valid, getattr(rec, "error", "missing"))
    check("dashboard_setup hook found", rec is not None and rec.dashboard_setup is not None)
    check("phone_script hook found", rec is not None and rec.phone_script is not None)
    pv = sys.modules["plugins.phone_voice"]

    # 2. server routes
    from fastapi.testclient import TestClient
    from dashboard.server import DashboardServer

    class Host:
        session = object()          # "online"
        _plugin_voice = None

    server = DashboardServer()
    sent: list[dict] = []
    orig_bc = server.broadcast

    async def capture(msg):
        sent.append(msg)
        await orig_bc(msg)
    server.broadcast = capture

    host = Host()
    reg.dashboard_setup(server, host)
    heard = {"pcm": None, "online": None}

    def fake_transcribe(pcm, online):
        heard["pcm"], heard["online"] = pcm, online
        return "open notepad", "gemini"
    pv.transcribe = fake_transcribe

    c = TestClient(server.app)
    js = c.get("/plugins.js")
    check("/plugins.js served", js.status_code == 200 and "pv-hold" in js.text)
    check("/plugins.js is JavaScript", "javascript" in js.headers.get("content-type", ""))

    r = c.post("/api/phone-voice", content=tone(1.0))
    check("login required", r.status_code == 401)

    server._tokens.add("tok123")
    auth = {"Authorization": "Bearer tok123", "Content-Type": "application/octet-stream"}
    r = c.post("/api/phone-voice", content=tone(0.1), headers=auth)
    check("too-short recording refused", r.status_code == 200 and not r.json()["ok"])

    r = c.post("/api/phone-voice", content=b"\x00" * (pv.MAX_BYTES + 2), headers=auth)
    check("too-long recording refused", r.status_code == 413)

    r = c.post("/api/phone-voice", content=tone(1.2), headers=auth)
    j = r.json()
    check("recording recognised", j.get("ok") and j.get("text") == "open notepad", str(j))
    check("online uses Gemini first", heard["online"] is True)
    q = server._command_queue
    check("words queued as a command", q.qsize() == 1 and q.get_nowait() == "open notepad")
    check("words shown on the phone",
          any(m.get("type") == "log" and m.get("speaker") == "user" and m.get("text") == "open notepad"
              for m in sent) and any(m.get("type") == "heard" for m in sent))

    host.session, host._plugin_voice = None, object()       # offline
    c.post("/api/phone-voice", content=tone(1.0), headers=auth)
    check("offline uses Whisper first", heard["online"] is False)

    # own voice: Habibi's last answer heard back is dropped (live mic only)
    asyncio.run(server.broadcast({"type": "log", "speaker": "habibi",
                                  "text": "Sure, I opened Notepad for you. Anything else you need today?"}))
    check("Habibi's answers are remembered", "Notepad for you" in pv._state["last_answer"])
    pv.transcribe = lambda pcm, online: ("I opened Notepad for you anything else", "whisper")
    res = asyncio.run(pv.handle_speech(server, host, tone(1.0), check_echo=True))
    check("own voice ignored on live mic", res.get("own_voice") is True, str(res))
    res = asyncio.run(pv.handle_speech(server, host, tone(1.0), check_echo=False))
    check("hold-to-talk never treated as echo", res.get("ok") is True, str(res))

    # 3. echo matcher
    ans = "Should I open Notepad for you now?"
    check("echo: long fragment of answer", pv.is_own_voice("should I open notepad for you", ans))
    check("echo: near-identical", pv.is_own_voice("should i open note pad for you now", ans))
    check("not echo: 'yes'", not pv.is_own_voice("yes", ans))
    check("not echo: 'open notepad'", not pv.is_own_voice("open notepad", ans))
    check("not echo: new request", not pv.is_own_voice("what's the weather in Riyadh today", ans))

    # 4. sentence cutter
    cut = pv.Sentences()
    audio = silence(1.0) + tone(1.2, 0.2) + silence(1.2) + tone(0.05, 0.2) + silence(1.0)
    found = []
    for i in range(0, len(audio), 2048):
        found += cut.feed(audio[i:i + 2048])
    check("live mic: one sentence found", len(found) == 1, f"found {len(found)}")
    if found:
        check("live mic: sentence covers the speech", 1.1 <= len(found[0]) / 32000 <= 2.4,
              f"{len(found[0]) / 32000:.2f}s")

    # 5. phone-page talk-over check (node)
    node = shutil.which("node")
    html = (ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")
    m = re.search(r"function _echoBargeStep\(.*?\n    }\n", html, re.S)
    check("talk-over check present in phone page", m is not None)
    if node and m:
        script = m.group(0) + r"""
function run(userAmp, spkOn) {
  const st = { gain: 1.0, ms: 0 }; let fired = -1; let seed = 7; const hist = [];
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  for (let i = 0; i < 160; i++) {                 // 160 × 32 ms ≈ 5 s of HABIBI talking
    const spk = spkOn ? 0.03 + 0.17 * rnd() : 0;  // what the phone speaker plays
    hist.push(spk); if (hist.length > 8) hist.shift();
    const late = hist[Math.max(0, hist.length - 4)];        // mic hears it ~100 ms later
    const echo = late * (1.4 + 0.4 * rnd());      // loud phone: mic hears it louder
    const user = i >= 80 ? userAmp : 0;           // user starts talking at 2.5 s
    const mic = Math.sqrt(echo * echo + user * user) + 0.005;
    if (_echoBargeStep(st, mic, Math.max(...hist), 32, i * 32)) { fired = i; break; }
  }
  return fired;
}
console.log(JSON.stringify({ echoOnly: run(0, true), user: run(0.6, true), pcOnly: run(0.3, false) }));
"""
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(script)
        out = subprocess.run([node, f.name], capture_output=True, text=True, encoding="utf-8", timeout=30)
        os.unlink(f.name)
        try:
            res = json.loads(out.stdout.strip())
        except Exception:
            res = {}
        check("phone speaker echo alone does NOT stop Habibi", res.get("echoOnly") == -1, out.stdout + out.stderr)
        check("user talking over Habibi DOES stop it", isinstance(res.get("user"), int) and res["user"] >= 80,
              str(res))
        check("talk-over with phone speaker off still works", isinstance(res.get("pcOnly"), int)
              and res["pcOnly"] >= 80, str(res))
    else:
        print("  SKIP  talk-over simulation (node not found)")

    # 5b. PC talk-over check (core/barge_in.py) with loud, delayed speakers
    pc_barge_test()

    # 6. real phone page in headless Chromium (optional)
    try:
        browser_test(server, pv)
    except ImportError:
        print("  SKIP  browser test (playwright not installed)")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): " + ", ".join(FAILS))
        return 1
    print("ALL PHONE VOICE CHECKS PASSED")
    return 0


def pc_barge_test() -> None:
    import random
    import numpy as np
    import core.barge_in as bi

    clock = {"t": 0.0}
    bi.time = type("T", (), {"monotonic": staticmethod(lambda: clock["t"])})

    def pcm(rms: float, n: int) -> bytes:
        a = np.random.default_rng(int(rms * 1e6) % 999).standard_normal(n) * rms
        return (np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes()

    def run(coupling: float, delay: float, user_amp: float, seconds: float = 6.0, seed: int = 3) -> float:
        b = bi.BargeIn()
        rnd = random.Random(seed)
        played = []                                   # (time, rms) Habibi plays
        nxt_out = 0.0
        t = 0.0
        while t < seconds:
            clock["t"] = t
            if t >= nxt_out:                          # speaker loop queues 200 ms batches
                r = 0.02 + 0.13 * rnd.random()
                b.note_output(pcm(r, 4800), 24000)
                played.append((t, r))
                nxt_out = t + 0.2
            heard = [r for (t0, r) in played if t0 <= t - delay < t0 + 0.2]
            echo = (heard[-1] if heard else 0.0) * coupling * (0.85 + 0.3 * rnd.random())
            user = user_amp if t >= 3.0 else 0.0
            mic = float(np.sqrt(echo ** 2 + user ** 2)) + 0.003
            if b.feed(pcm(mic, 1024)):
                return t
            t += 0.064
        return -1.0

    for coupling, delay in ((1.0, 0.05), (3.0, 0.2), (5.0, 0.4)):
        fired = run(coupling, delay, 0.0)
        check(f"PC: own voice ignored (echo x{coupling}, {int(delay * 1000)} ms late)", fired < 0,
              f"stopped itself at {fired:.2f}s")
    fired = run(1.0, 0.1, 0.4)
    check("PC: user talking over Habibi still stops it", fired >= 3.0, f"fired at {fired:.2f}s")
    fired = run(3.0, 0.2, 0.95)
    check("PC: user talking over loud speakers still stops it", fired >= 3.0, f"fired at {fired:.2f}s")
    bi.time = time


def loop_of(srv):
    return srv._phone_voice_loop


def browser_test(server, pv) -> None:
    from playwright.sync_api import sync_playwright
    import uvicorn

    pv.transcribe = lambda pcm, online: ("what time is it", "gemini")
    port = 18765
    cfg = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning", log_config=None)
    srv = uvicorn.Server(cfg)

    def serve():
        loop = asyncio.new_event_loop()
        srv._phone_voice_loop = loop
        loop.run_until_complete(srv.serve())
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    errors: list[str] = []
    try:
        with sync_playwright() as p:
            exe = os.environ.get("HABIBI_CHROMIUM") or None
            b = p.chromium.launch(executable_path=exe, args=["--use-fake-ui-for-media-stream",
                                        "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
            pg = b.new_page(viewport={"width": 390, "height": 760}, device_scale_factor=2)
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.add_init_script("sessionStorage.setItem('habibi_token','tok123');")
            pg.goto(f"http://127.0.0.1:{port}/")
            pg.wait_for_selector("#pv-hold", timeout=8000)
            check("HOLD button on the phone page", pg.is_visible("#pv-hold"))
            box = pg.locator("#pv-hold").bounding_box()
            pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            pg.mouse.down()
            pg.wait_for_timeout(500)
            check("caption says LISTENING while held", "LISTENING" in pg.inner_text("#pv-cap"))
            pg.wait_for_timeout(1000)
            pg.mouse.up()
            pg.wait_for_function("document.getElementById('pv-cap').innerText.includes('what time is it')",
                                 timeout=8000)
            check("caption shows the recognised words", "YOU SAID" in pg.inner_text("#pv-cap"))
            pg.screenshot(path="phone_voice_selftest.png")
            fut = asyncio.run_coroutine_threadsafe(
                server.broadcast({"type": "voice_state", "state": "interrupted"}), loop_of(srv))
            fut.result(timeout=5)
            pg.wait_for_timeout(500)
            check("caption shows INTERRUPTED (sent from the PC)", "INTERRUPTED" in pg.inner_text("#pv-cap"))
            fut = asyncio.run_coroutine_threadsafe(
                server.broadcast({"type": "heard", "text": "open youtube"}), loop_of(srv))
            fut.result(timeout=5)
            pg.wait_for_timeout(500)
            check("caption shows live words (sent from the PC)", "open youtube" in pg.inner_text("#pv-cap"))
            b.close()
    finally:
        srv.should_exit = True
        th.join(timeout=5)
    check("phone page has no script errors", not errors, "; ".join(errors))


if __name__ == "__main__":
    sys.exit(main())
