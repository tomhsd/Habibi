"""
Phone voice — talk to Habibi from the phone, in Online AND Offline mode.

What it adds to the phone remote (the Habibi phone app and the browser page):
  • 🎙 HOLD button: hold it, speak, let go. The PC turns the recording into
    words (Gemini when online, Whisper on the PC when offline), shows them on
    the phone, and runs them as a command. Habibi's answer appears on the phone.
  • A live caption line above the buttons: "Listening…" while you hold,
    "Interrupted — listening…" when you talk over Habibi, then your words as
    Habibi recognises them (live with the 🎤 VOICE button in Online mode).
  • Offline mode: the 🎤 VOICE live mic used to be ignored offline. Now the
    phone's live audio is cut into sentences on the PC, recognised with Whisper
    and run as commands.
  • Habibi's own voice is never taken as a command: a recognised sentence that
    is just Habibi's last answer (heard back through the phone mic) is dropped.

Plugin hooks used (core/plugin_loader.py): dashboard_setup(server, host) adds
the /api/phone-voice route and the offline live-mic listener; phone_script()
adds the phone-page part. Turn it off in ⚙ PLUGINS like any other plugin.
"""
import asyncio
import difflib
import re
import sys
import threading
import time
from typing import Optional

PLUGIN = {
    "name": "phone_voice",
    "description": (
        "Reports whether talking to Habibi from the phone works right now (hold-to-talk "
        "and live phone mic) and which speech recogniser it uses. Call it when the user "
        "asks if phone voice / the phone mic works, or why Habibi doesn't hear the phone."
    ),
    "parameters": {"type": "OBJECT", "properties": {}, "required": []},
}

SAMPLE_RATE = 16000
MAX_SECONDS = 60
MAX_BYTES = SAMPLE_RATE * 2 * MAX_SECONDS
MIN_SECONDS = 0.35
GEMINI_MODELS = ("gemini-flash-latest", "gemini-flash-lite-latest")

_state = {
    "host": None,
    "server": None,
    "last_answer": "",      # Habibi's latest reply, to spot it coming back through the mic
    "last_answer_t": 0.0,
    "engine": "",           # recogniser used last: gemini | whisper
    "last_text": "",
    "last_error": "",
}
_whisper = None
_whisper_lock = threading.Lock()


# ── recognising speech ─────────────────────────────────────────────────────

def _online(host) -> bool:
    """Gemini Live is running (Online mode, not handed to the offline plugin)."""
    return (host is not None and getattr(host, "session", None) is not None
            and getattr(host, "_plugin_voice", None) is None)


def _wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _gemini_key() -> Optional[str]:
    try:
        from dashboard.server import _get_gemini_key
        return _get_gemini_key()
    except Exception:
        return None


def _transcribe_gemini(pcm: bytes) -> str:
    key = _gemini_key()
    if not key:
        raise RuntimeError("no Gemini key")
    from google import genai
    from google.genai import types as gtypes
    client = genai.Client(api_key=key)
    prompt = ("Write down exactly what the speaker says, in the language they speak "
              "(Bengali in Bengali script, English in English). Reply with the words only. "
              "If nobody speaks, reply with nothing.")
    err = None
    for model in GEMINI_MODELS:
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[gtypes.Part.from_bytes(data=_wav(pcm), mime_type="audio/wav"), prompt],
                config=gtypes.GenerateContentConfig(temperature=0),
            )
            return (resp.text or "").strip()
        except Exception as e:
            err = e
    raise RuntimeError(f"Gemini could not transcribe: {err}")


def _offline_ears():
    """The offline mode's Whisper, if it is already loaded (saves RAM)."""
    mod = sys.modules.get("plugins.offline_mode")
    a = getattr(mod, "_assistant", None) if mod else None
    ears = getattr(a, "ears", None)
    if ears is not None and getattr(ears, "_stt", None) is not None:
        return ears
    return None


def _offline_language() -> Optional[str]:
    try:
        from memory.config_manager import get_plugin_config
        lang = str((get_plugin_config("offline_mode") or {}).get("language") or "auto")
        return None if lang == "auto" else lang
    except Exception:
        return None


def _transcribe_whisper(pcm: bytes) -> str:
    import numpy as np
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    ears = _offline_ears()
    if ears is not None:
        return ears.transcribe(audio)
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            import importlib.util
            if importlib.util.find_spec("faster_whisper") is None:
                raise RuntimeError("Whisper (faster-whisper) is not installed on this PC")
            from core.stt import WhisperSTT
            _whisper = WhisperSTT("base", _offline_language())
        return _whisper.transcribe(audio).strip()


def transcribe(pcm: bytes, online: bool) -> tuple[str, str]:
    """PCM16 mono 16 kHz → (text, engine). Online prefers Gemini (best for
    Bengali), offline uses Whisper on this PC; each falls back to the other."""
    order = (("gemini", _transcribe_gemini), ("whisper", _transcribe_whisper))
    if not online:
        order = order[::-1]
    errors = []
    for name, fn in order:
        try:
            return _clean(fn(pcm)), name
        except Exception as e:
            errors.append(f"{name}: {e}")
    raise RuntimeError("; ".join(errors))


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text.strip('"').strip()


# ── Habibi's own voice ─────────────────────────────────────────────────────

def is_own_voice(text: str, last_answer: str) -> bool:
    """True when `text` is just Habibi's last answer heard back through the mic."""
    a = re.sub(r"[^\w\s]", " ", (text or "").lower(), flags=re.UNICODE).split()
    b = re.sub(r"[^\w\s]", " ", (last_answer or "").lower(), flags=re.UNICODE).split()
    if not a or not b:
        return False
    sa, sb = " ".join(a), " ".join(b)
    # Short replies ("yes", "open notepad") are real answers even if Habibi
    # used the same words in its question, so only longer echoes count.
    if len(a) >= 4 and sa in sb:
        return True
    if len(a) >= 3 and difflib.SequenceMatcher(None, sa, sb).ratio() >= 0.8:
        return True
    hits = sum(1 for w in a if w in set(b))                   # mostly Habibi's words
    return len(a) >= 5 and hits / len(a) >= 0.85


def _own_voice(text: str) -> bool:
    if time.time() - _state["last_answer_t"] > 30:
        return False
    return is_own_voice(text, _state["last_answer"])


def _watch_answers(server) -> None:
    """Remember what Habibi says (every 'habibi' log sent to the phone)."""
    orig = server.broadcast
    if getattr(orig, "_phone_voice", False):
        return

    async def broadcast(msg: dict) -> None:
        try:
            if msg.get("type") == "log" and msg.get("speaker") in ("habibi", "ai"):
                _state["last_answer"] = str(msg.get("text") or "")
                _state["last_answer_t"] = time.time()
        except Exception:
            pass
        await orig(msg)

    broadcast._phone_voice = True
    server.broadcast = broadcast


# ── one recorded sentence → command ────────────────────────────────────────

async def handle_speech(server, host, pcm: bytes, check_echo: bool = False) -> dict:
    """Recognise one recorded sentence and run it like a typed phone command.
    check_echo: drop it if it is only Habibi's own last answer (live mic)."""
    if len(pcm) < int(SAMPLE_RATE * 2 * MIN_SECONDS):
        return {"ok": False, "error": "Too short — hold the button while you speak."}
    online = _online(host)
    try:
        text, engine = await asyncio.to_thread(transcribe, pcm, online)
    except Exception as e:
        _state["last_error"] = str(e)
        return {"ok": False, "error": f"Couldn't recognise speech ({e})."}
    _state["engine"] = engine
    if not text or len(text) < 2:
        return {"ok": False, "error": "I didn't catch any words — try again."}
    if check_echo and _own_voice(text):
        return {"ok": False, "own_voice": True, "error": "That was my own voice — ignored."}
    _state["last_text"] = text
    _state["last_error"] = ""
    await server.broadcast({"type": "heard", "text": text, "final": True})
    await server.broadcast({"type": "log", "speaker": "user", "text": text,
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    await server.send_command(text)
    return {"ok": True, "text": text, "engine": engine}


# ── offline: the phone's live mic (🎤 VOICE) ───────────────────────────────

class Sentences:
    """Cuts a stream of PCM16 chunks into sentences with a self-calibrating
    loudness gate (same idea as the offline engine's, but for 16 kHz bytes)."""

    def __init__(self, block_ms: int = 32, end_ms: int = 700, max_s: float = 20.0):
        self.block = int(SAMPLE_RATE * block_ms / 1000) * 2
        self.end_blocks = max(1, end_ms // block_ms)
        self.max_bytes = int(max_s * SAMPLE_RATE * 2)
        self.floor = 150.0
        self.reset()

    def reset(self) -> None:
        self._buf = b""
        self._pre: list[bytes] = []
        self._cur: list[bytes] = []
        self._quiet = 0
        self._loud = 0

    @staticmethod
    def rms(b: bytes) -> float:
        import numpy as np
        a = np.frombuffer(b, dtype=np.int16).astype(np.float32)
        return float(np.sqrt(np.mean(a * a))) if a.size else 0.0

    def feed(self, data: bytes) -> list[bytes]:
        out = []
        self._buf += data
        while len(self._buf) >= self.block:
            blk, self._buf = self._buf[:self.block], self._buf[self.block:]
            lvl = self.rms(blk)
            if not self._cur:
                if lvl > max(self.floor * 3.0, 300.0):
                    self._cur = self._pre + [blk]
                    self._pre, self._quiet, self._loud = [], 0, 1
                else:
                    self.floor = 0.95 * self.floor + 0.05 * lvl
                    self._pre = (self._pre + [blk])[-6:]
                continue
            self._cur.append(blk)
            if lvl < max(self.floor * 2.0, 200.0):
                self._quiet += 1
            else:
                self._quiet = 0
                self._loud += 1
            if self._quiet >= self.end_blocks or sum(map(len, self._cur)) >= self.max_bytes:
                done, loud = b"".join(self._cur), self._loud
                self._cur, self._quiet, self._loud = [], 0, 0
                if loud >= 4:
                    out.append(done)
        return out


async def _offline_live_mic(server, host) -> None:
    """While the offline plugin runs the voice loop, nothing else reads the
    phone's live audio: turn it into commands here."""
    q = server._phone_audio_queue
    cut = Sentences()
    busy = False
    while True:
        try:
            if getattr(host, "_plugin_voice", None) is None:   # online: core relays it
                cut.reset()
                await asyncio.sleep(0.5)
                continue
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            data = chunk.get("data") if isinstance(chunk, dict) else chunk
            if not data:
                continue
            for sentence in cut.feed(bytes(data)):
                if busy:
                    continue
                busy = True
                try:
                    res = await handle_speech(server, host, sentence, check_echo=True)
                    if not res.get("ok") and not res.get("own_voice") and "catch" not in res.get("error", ""):
                        await server.broadcast({"type": "sys", "text": res.get("error", "")})
                finally:
                    busy = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[phone_voice] live mic: {e}")
            await asyncio.sleep(1)


# ── plugin hooks ───────────────────────────────────────────────────────────

def dashboard_setup(server, host) -> None:
    from fastapi import Request
    from fastapi.responses import JSONResponse

    _state["host"], _state["server"] = host, server
    _watch_answers(server)

    @server.app.post("/api/phone-voice")
    async def phone_voice(req: Request):
        if not server.is_authorized(req):
            return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
        pcm = await req.body()
        if len(pcm) > MAX_BYTES:
            return JSONResponse({"ok": False, "error": f"Too long — keep it under {MAX_SECONDS} s."},
                                status_code=413)
        if len(pcm) % 2:
            pcm = pcm[:-1]
        return JSONResponse(await handle_speech(server, host, pcm))

    try:
        asyncio.get_running_loop().create_task(_offline_live_mic(server, host))
    except RuntimeError:
        pass        # no loop (tests): the hold button still works


def phone_script() -> str:
    return _PHONE_JS


def run(parameters: dict, player=None, session_memory=None) -> str:
    host, server = _state["host"], _state["server"]
    if server is None:
        return "Phone voice isn't ready: the Remote Control server hasn't started."
    online = _online(host)
    import importlib.util
    whisper = importlib.util.find_spec("faster_whisper") is not None
    parts = ["Phone voice is on. Hold the 🎙 HOLD button on the phone and speak."]
    parts.append("I recognise it with Gemini while online" +
                 (", and with Whisper on this PC when offline." if whisper else
                  ". Whisper isn't installed, so it won't work offline."))
    if _state["last_text"]:
        parts.append(f"Last thing I heard from the phone: “{_state['last_text']}”.")
    if _state["last_error"]:
        parts.append(f"Last problem: {_state['last_error']}")
    if not online and not whisper:
        parts.append("Right now I'm offline without Whisper, so phone voice can't work.")
    return " ".join(parts)


# ── phone page part (loaded from /plugins.js) ──────────────────────────────

_PHONE_JS = r"""
(function () {
  const css = document.createElement('style');
  css.textContent = `
    #pv-hold { touch-action: none; user-select: none; -webkit-user-select: none; }
    #pv-hold.rec { background: color-mix(in srgb, var(--red) 22%, transparent); border-color: var(--red);
                   color: var(--red); animation: mic-pulse 1s ease-in-out infinite; }
    #pv-cap { min-height: 0; font-size: 13px; line-height: 1.35; padding: 0 2px; color: var(--white);
              opacity: 0; transition: opacity .2s; overflow-wrap: anywhere; }
    #pv-cap.show { opacity: 1; min-height: 18px; padding: 4px 2px 2px; }
    #pv-cap .st { color: var(--red); font-weight: 700; letter-spacing: 1px; font-size: 11px; margin-right: 6px; }
    #pv-cap .st.int { color: var(--acc); }
  `;
  document.head.appendChild(css);

  const mic = document.getElementById('mic-btn');
  if (!mic) return;
  const row = mic.parentElement, footer = row.parentElement;
  const cap = document.createElement('div');
  cap.id = 'pv-cap';
  footer.insertBefore(cap, footer.firstChild);
  const btn = document.createElement('button');
  btn.className = 'hbtn'; btn.id = 'pv-hold'; btn.type = 'button';
  btn.title = 'Hold to talk — let go to send';
  btn.textContent = '🎙 HOLD';
  mic.insertAdjacentElement('afterend', btn);

  let capTimer = 0;
  function caption(state, text, cls, keepMs) {
    cap.innerHTML = '';
    if (state) { const s = document.createElement('span'); s.className = 'st ' + (cls || ''); s.textContent = state; cap.appendChild(s); }
    if (text)  cap.appendChild(document.createTextNode(text));
    cap.classList.toggle('show', !!(state || text));
    clearTimeout(capTimer);
    if (keepMs) capTimer = setTimeout(() => cap.classList.remove('show'), keepMs);
  }
  window.habibiHooks = window.habibiHooks || { message: [] };
  window.habibiHooks.message.push(m => {
    if (m.type === 'voice_state' && m.state === 'interrupted') caption('INTERRUPTED — LISTENING…', '', 'int', 6000);
    if (m.type === 'heard' && m.text) caption(m.final ? 'YOU SAID' : 'HEARING', m.text, '', m.final ? 8000 : 10000);
    if (m.type === 'log' && (m.speaker === 'habibi' || m.speaker === 'ai')) setTimeout(() => cap.classList.remove('show'), 2500);
  });
  (window.habibiHooks.bargeIn = window.habibiHooks.bargeIn || []).push(
    () => caption('INTERRUPTED — LISTENING…', '', 'int', 6000));

  // ── hold to talk ──
  let rec = null;
  function stopTracks(r) { try { r.node.disconnect(); } catch (_) {} try { r.ctx.close(); } catch (_) {}
                           r.stream.getTracks().forEach(t => t.stop()); }
  async function start(ev) {
    ev.preventDefault();
    if (rec) return;
    try { btn.setPointerCapture(ev.pointerId); } catch (_) {}
    if (typeof doStop === 'function' && typeof _speakingNow === 'function' && _speakingNow()) doStop();
    if (!navigator.mediaDevices?.getUserMedia) {
      sys('This browser blocks the microphone here — open the remote over https or in the Habibi app.', true); return;
    }
    const r = { chunks: [], len: 0, up: false, t0: performance.now() };
    rec = r;
    // Live 🎤 VOICE would send the same words a second time: pause it meanwhile.
    r.live = (typeof _voiceWs !== 'undefined' && !!_voiceWs);
    if (r.live) _stopVoice();
    btn.classList.add('rec'); btn.textContent = '🎙 LISTENING';
    caption('LISTENING…', 'speak now, let go to send');
    try {
      r.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (e) {
      rec = null; idle();
      if (r.live && _voiceWant) _startVoice();
      caption('', ''); sys(e.name === 'NotAllowedError'
        ? 'Microphone not allowed — allow it for Habibi and try again.' : 'Mic error: ' + e.message, true);
      return;
    }
    try { r.ctx = new AudioContext({ sampleRate: 16000 }); } catch (_) { r.ctx = new AudioContext(); }
    const src = r.ctx.createMediaStreamSource(r.stream);
    r.node = r.ctx.createScriptProcessor(4096, 1, 1);
    r.node.onaudioprocess = e => {
      const d = e.inputBuffer.getChannelData(0);
      if (typeof lv === 'object') { lv.mic = Math.max(lv.mic, _rms(d)); lv.micWave = _down(d, 96); }
      const pcm = new Int16Array(_f32toPcm16(d, r.ctx.sampleRate));
      r.chunks.push(pcm); r.len += pcm.length;
      if (r.len > 16000 * 60) end();
    };
    src.connect(r.node); r.node.connect(r.ctx.destination);
    if (r.up) end();                       // released while the mic was opening
  }
  function idle() { btn.classList.remove('rec'); btn.textContent = '🎙 HOLD'; }
  async function end() {
    const r = rec;
    if (!r) return;
    r.up = true;
    if (!r.stream) return;                 // still opening — start() finishes it
    rec = null; idle(); stopTracks(r);
    if (r.live && typeof _voiceWant !== 'undefined' && _voiceWant) setTimeout(_startVoice, 300);
    if (r.len < 16000 * 0.35) { caption('', 'Too short — hold the button while you speak.', '', 3000); return; }
    const out = new Int16Array(r.len); let off = 0;
    for (const c of r.chunks) { out.set(c, off); off += c.length; }
    caption('RECOGNISING…', '');
    try {
      const res = await _authFetch('/api/phone-voice', {
        method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: out.buffer });
      const j = await res.json().catch(() => ({}));
      if (j.ok) caption('YOU SAID', j.text, '', 8000);
      else caption('', j.error || ('Error ' + res.status), '', 5000);
    } catch (e) {
      caption('', 'Could not reach Habibi: ' + e.message, '', 5000);
    }
  }
  btn.addEventListener('pointerdown', start);
  btn.addEventListener('pointerup', end);
  btn.addEventListener('pointercancel', end);
  btn.addEventListener('contextmenu', e => e.preventDefault());
})();
"""
