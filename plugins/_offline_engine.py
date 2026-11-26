"""
Offline engine for the offline_mode plugin (the leading "_" keeps the plugin
loader from treating this helper as a plugin).

    mic ──► Listener (energy VAD) ──► Whisper (faster-whisper) ──► Brain (Ollama + tools)
                                                                      │
                                     speaker ◄── Speaker (Piper → Windows voice) ◄┘

Every piece is optional and degrades on its own:
  • no faster-whisper → typed commands only
  • no Ollama / model → Habibi says it can't think offline yet
  • no Piper voice    → the built-in Windows voice (SAPI); elsewhere text only
Nothing here touches the internet.
"""
from __future__ import annotations

import asyncio
import json
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

SAMPLE_RATE = 16000
BLOCK = 1024                      # 64 ms at 16 kHz

# ── tools the local model is offered ───────────────────────────────────────
# Kept short on purpose: a 3B model picks well from about a dozen tools and
# badly from forty. Only tools that work without the internet.
OFFLINE_TOOLS = (
    "open_app", "computer_settings", "system_status", "system_tools",
    "file_controller", "find_files", "window_manager", "media_player",
    "reminder", "recall_memory", "save_memory", "clipboard_history",
    "macro", "screen_text", "undo", "offline_mode", "saved_info",
)
ONLINE_ONLY = ("web search, live news, weather, YouTube, flights, sending messages, "
               "coding help, reading or understanding the screen with AI")
DESC_LIMIT = 300
MAX_TOOL_ROUNDS = 3
HISTORY_TURNS = 8


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


PIPER_DIR = base_dir() / "models" / "piper"


# ── schema conversion: Gemini declarations → Ollama / OpenAI tools ──────────

def _lower_schema(node):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "type" and isinstance(v, str):
                out[k] = v.lower()
            elif k == "description" and isinstance(v, str):
                out[k] = v[:DESC_LIMIT]
            else:
                out[k] = _lower_schema(v)
        return out
    if isinstance(node, list):
        return [_lower_schema(x) for x in node]
    return node


def offline_tools(declarations: list[dict], allowed=OFFLINE_TOOLS) -> list[dict]:
    """Pick the offline-safe tools from Habibi's declarations, in Ollama's format."""
    by_name = {d.get("name"): d for d in declarations if isinstance(d, dict)}
    tools = []
    for name in allowed:
        d = by_name.get(name)
        if not d:
            continue
        params = _lower_schema(d.get("parameters") or {"type": "OBJECT", "properties": {}})
        params.setdefault("properties", {})
        tools.append({"type": "function", "function": {
            "name": name,
            "description": str(d.get("description") or "")[:DESC_LIMIT],
            "parameters": params,
        }})
    return tools


# ── listening ──────────────────────────────────────────────────────────────

class Utterances:
    """Turns a stream of int16 blocks into whole utterances with a simple,
    self-calibrating energy detector. Pure numpy, testable without a mic."""

    def __init__(self, start_ratio=3.0, end_ratio=2.0, min_start=300.0, min_end=200.0,
                 end_blocks=12, min_blocks=5, max_seconds=20.0, pre_blocks=4):
        self.floor = 150.0
        self.start_ratio, self.end_ratio = start_ratio, end_ratio
        self.min_start, self.min_end = min_start, min_end
        self.end_blocks, self.min_blocks = end_blocks, min_blocks
        self.max_blocks = int(max_seconds * SAMPLE_RATE / BLOCK)
        self.pre_blocks = pre_blocks
        self._pre: list[np.ndarray] = []
        self._cur: list[np.ndarray] = []
        self._quiet = 0
        self._loud = 0

    @staticmethod
    def rms(block: np.ndarray) -> float:
        b = block.astype(np.float32)
        return float(np.sqrt(np.mean(b * b))) if b.size else 0.0

    def reset(self) -> None:
        self._pre, self._cur, self._quiet, self._loud = [], [], 0, 0

    def feed(self, block: np.ndarray) -> Optional[np.ndarray]:
        """Feed one int16 block. Returns a finished utterance (float32 −1..1) or None."""
        block = np.asarray(block, dtype=np.int16).reshape(-1)
        level = self.rms(block)
        if not self._cur:
            if level > max(self.floor * self.start_ratio, self.min_start):
                self._cur = self._pre + [block]
                self._pre, self._quiet, self._loud = [], 0, 1
                return None
            self.floor = 0.95 * self.floor + 0.05 * level
            self._pre = (self._pre + [block])[-self.pre_blocks:]
            return None
        self._cur.append(block)
        if level < max(self.floor * self.end_ratio, self.min_end):
            self._quiet += 1
        else:
            self._quiet = 0
            self._loud += 1
        if self._quiet >= self.end_blocks or len(self._cur) >= self.max_blocks:
            done, loud = self._cur, self._loud
            self.reset()
            if loud < self.min_blocks:
                return None             # a click or a cough
            return np.concatenate(done).astype(np.float32) / 32768.0
        return None


class Mic:
    """Microphone stream → queue of int16 blocks (callback thread → consumer)."""

    def __init__(self):
        self.q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=400)
        self.stream = None
        self.on_level: Optional[Callable[[float], None]] = None
        self.gate: Optional[Callable[[np.ndarray], bool]] = None   # False → drop the block

    def open(self) -> None:
        import sounddevice as sd
        dev = None
        try:
            from memory.config_manager import get_input_device
            from core import audio_devices
            dev = audio_devices.resolve(get_input_device(), "input")
        except Exception:
            dev = None

        def cb(indata, frames, time_info, status):
            block = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
            if self.gate is not None and not self.gate(block):
                return
            try:
                self.q.put_nowait(block)
            except queue.Full:
                pass
            if self.on_level is not None:
                try:
                    self.on_level(min(1.0, Utterances.rms(block) / 2600.0))
                except Exception:
                    pass

        def _open(d):
            return sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                  blocksize=BLOCK, device=d, callback=cb)
        try:
            self.stream = _open(dev)
        except Exception:
            if dev is None:
                raise
            self.stream = _open(None)
        self.stream.start()

    def drain(self) -> None:
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        s, self.stream = self.stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass


class Ears:
    """Speech-to-text with faster-whisper (CPU, int8). Loads lazily."""

    def __init__(self, model: str = "base", language: str = "auto"):
        self.model_name = model or "base"
        self.language = language or "auto"
        self._stt = None
        self.error = ""

    @staticmethod
    def installed() -> bool:
        import importlib.util
        return importlib.util.find_spec("faster_whisper") is not None

    def load(self) -> bool:
        if self._stt is not None:
            return True
        if not self.installed():
            self.error = "faster-whisper is not installed"
            return False
        try:
            from core.stt import WhisperSTT
            self._stt = WhisperSTT(self.model_name, self.language)
            return True
        except Exception as e:
            self.error = str(e)
            return False

    def transcribe(self, audio: np.ndarray) -> str:
        if not self.load():
            return ""
        try:
            return self._stt.transcribe(audio).strip()
        except Exception as e:
            self.error = str(e)
            return ""


# ── speaking ───────────────────────────────────────────────────────────────

def find_piper_voice(folder: Path = PIPER_DIR) -> Optional[Path]:
    try:
        voices = sorted(p for p in folder.glob("*.onnx") if Path(str(p) + ".json").exists())
        return voices[0] if voices else None
    except Exception:
        return None


class Mouth:
    """Text-to-speech. Piper if a voice is downloaded, else the Windows voice."""

    def __init__(self):
        self._piper = None
        self._piper_tried = False
        self.engine = "none"
        self.stop_flag = threading.Event()

    def _load_piper(self):
        if self._piper_tried:
            return self._piper
        self._piper_tried = True
        voice = find_piper_voice()
        if voice is None:
            return None
        try:
            from piper.voice import PiperVoice
            self._piper = PiperVoice.load(str(voice))
        except Exception as e:
            print(f"[Offline] Piper unavailable: {e}")
            self._piper = None
        return self._piper

    def _piper_pcm(self, text: str) -> tuple[bytes, int]:
        v = self._piper
        rate = int(getattr(getattr(v, "config", None), "sample_rate", 22050) or 22050)
        if hasattr(v, "synthesize_stream_raw"):            # piper-tts 1.2
            return b"".join(v.synthesize_stream_raw(text)), rate
        chunks = list(v.synthesize(text))                   # piper-tts 1.3+
        if chunks and hasattr(chunks[0], "audio_int16_bytes"):
            rate = int(getattr(chunks[0], "sample_rate", rate) or rate)
            return b"".join(c.audio_int16_bytes for c in chunks), rate
        raise RuntimeError("unknown piper-tts version")

    def _play(self, pcm: bytes, rate: int) -> None:
        import sounddevice as sd
        dev = None
        try:
            from memory.config_manager import get_output_device
            from core import audio_devices
            dev = audio_devices.resolve(get_output_device(), "output")
        except Exception:
            dev = None
        audio = np.frombuffer(pcm, dtype=np.int16)
        sd.play(audio, rate, device=dev)
        while sd.get_stream().active:
            if self.stop_flag.is_set():
                sd.stop()
                break
            time.sleep(0.05)

    def _sapi(self, text: str) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            try:
                v = win32com.client.Dispatch("SAPI.SpVoice")
                v.Speak(text, 1)                 # async, so we can stop it
                while not v.WaitUntilDone(50):
                    if self.stop_flag.is_set():
                        v.Speak("", 3)           # purge
                        break
            finally:
                pythoncom.CoUninitialize()
            return True
        except Exception as e:
            print(f"[Offline] Windows voice unavailable: {e}")
            return False

    def say(self, text: str) -> str:
        """Speak (blocking). Returns the engine used: piper | windows | none."""
        self.stop_flag.clear()
        text = (text or "").strip()
        if not text:
            return "none"
        if self._load_piper() is not None:
            try:
                pcm, rate = self._piper_pcm(text)
                self._play(pcm, rate)
                self.engine = "piper"
                return self.engine
            except Exception as e:
                print(f"[Offline] Piper failed: {e}")
        if self._sapi(text):
            self.engine = "windows"
            return self.engine
        self.engine = "none"
        return self.engine

    def stop(self) -> None:
        self.stop_flag.set()


# ── thinking ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are {name}, a friendly voice assistant running OFFLINE on the user's "
    "Windows PC. Reply in the user's language, in one to three short spoken "
    "sentences, no lists or markdown. Use a tool when the user asks you to do "
    "something on the PC. These need the internet and are unavailable right now: "
    "{online_only}. If asked for one, say briefly that it needs the internet and "
    "you'll be able to do it when the connection is back. For news headlines or "
    "exchange rates use saved_info and say how old the saved data is."
)


class Brain:
    """A local model through Ollama's /api/chat, with tool calls."""

    def __init__(self, model: str, run_tool: Callable[[str, dict], str],
                 tools: list[dict], name: str = "Habibi",
                 post: Optional[Callable] = None):
        self.model = model
        self.run_tool = run_tool
        self.tools = tools
        self.name = name
        self.history: list[dict] = []
        self._post = post            # for tests: post(url, json, timeout) -> dict
        self.error = ""

    def _url(self) -> str:
        try:
            from core.llm_client import get_llm_settings
            return get_llm_settings()[0]
        except Exception:
            return "http://localhost:11434"

    def available(self) -> bool:
        if self._post is not None:
            return True
        try:
            import requests
            from core.llm_client import ensure_ollama_running
            if not ensure_ollama_running(timeout=10):
                self.error = "Ollama isn't running"
                return False
            tags = requests.get(f"{self._url()}/api/tags", timeout=3).json()
            names = {m.get("name", "") for m in tags.get("models", [])}
            want = self.model if ":" in self.model else self.model + ":latest"
            if want not in names and self.model not in names:
                self.error = f"model '{self.model}' isn't downloaded (ollama pull {self.model})"
                return False
            return True
        except Exception as e:
            self.error = f"Ollama isn't reachable: {e}"
            return False

    def _chat(self, messages: list[dict]) -> dict:
        payload = {
            "model": self.model, "messages": messages, "stream": False,
            "keep_alive": "30m", "options": {"num_predict": 220, "temperature": 0.4},
        }
        if self.tools:
            payload["tools"] = self.tools
        if self._post is not None:
            return self._post(f"{self._url()}/api/chat", payload, 120)
        import requests
        r = requests.post(f"{self._url()}/api/chat", json=payload, timeout=180)
        r.raise_for_status()
        return r.json()

    def reply(self, text: str) -> str:
        sys_msg = {"role": "system", "content": SYSTEM_PROMPT.format(
            name=self.name, online_only=ONLINE_ONLY)}
        turn = [{"role": "user", "content": text}]
        for _ in range(MAX_TOOL_ROUNDS + 1):
            data = self._chat([sys_msg] + self.history + turn)
            msg = data.get("message") or {}
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = (msg.get("content") or "").strip()
                self.history = (self.history + turn + [
                    {"role": "assistant", "content": answer}])[-HISTORY_TURNS * 2:]
                return answer
            turn.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
            for c in calls:
                fn = c.get("function") or {}
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                allowed = {t["function"]["name"] for t in self.tools}
                if name not in allowed:
                    result = f"'{name}' isn't available offline."
                else:
                    try:
                        result = self.run_tool(name, args)
                    except Exception as e:
                        result = f"{name} failed: {e}"
                turn.append({"role": "tool", "content": str(result)[:1500], "tool_name": name})
        return "Done."


# ── the loop ───────────────────────────────────────────────────────────────

class OfflineAssistant:
    """Glue: mic → ears → brain → mouth, plus typed commands. One turn at a time."""

    def __init__(self, host, settings: dict, log: Callable[[str], None]):
        self.host = host
        self.ui = host.ui
        self.log = log
        name = getattr(host, "_asst_name", "HABIBI") or "HABIBI"
        self.name = name
        self.ears = Ears(settings.get("stt_model") or "base", settings.get("language") or "auto")
        self.mouth = Mouth()
        self.mic: Optional[Mic] = None
        self.turn_lock = threading.Lock()
        self.speaking = False
        try:
            decls = host.tool_declarations()
        except Exception:
            decls = []
        self.brain = Brain(settings.get("model") or "qwen2.5:3b", self._run_tool_sync,
                           offline_tools(decls), name=name.title())
        self.brain_ok: Optional[bool] = None
        self.quick: Optional[Callable[[str], Optional[str]]] = None   # mode commands, no model

    # tools run on Habibi's event loop, exactly like a Gemini tool call
    def _run_tool_sync(self, name: str, args: dict) -> str:
        loop = getattr(self.host, "_loop", None)
        fut = asyncio.run_coroutine_threadsafe(self.host.run_tool(name, args), loop)
        return fut.result(timeout=120)

    def _set_speaking(self, on: bool) -> None:
        self.speaking = on
        try:
            self.host.set_speaking(on)
        except Exception:
            pass

    def _state(self, s: str) -> None:
        try:
            self.ui.set_state(s)
        except Exception:
            pass

    def status(self) -> dict:
        return {
            "listening": self.mic is not None,
            "stt": "whisper " + self.ears.model_name if Ears.installed() else "not installed",
            "brain": self.brain.model if self.brain_ok else ("not ready: " + self.brain.error),
            "voice": "piper" if find_piper_voice() else ("windows" if sys.platform == "win32" else "none"),
        }

    def handle_text(self, text: str, spoken: bool = False) -> str:
        """One full turn (blocking; called from a worker thread)."""
        with self.turn_lock:
            self._state("THINKING")
            quick = self.quick(text) if self.quick else None
            if quick:
                answer = quick
            else:
                if not self.brain_ok:
                    self.brain_ok = self.brain.available()
                if not self.brain_ok:
                    answer = ("I'm offline and my local brain isn't ready yet: "
                              f"{self.brain.error}.")
                else:
                    try:
                        answer = self.brain.reply(text) or "Okay."
                    except Exception as e:
                        self.brain_ok = None
                        answer = f"My local brain had a problem: {e}"
            self.log(f"{self.name}: {answer}")
            if self.mic is not None:
                self.mic.drain()
            self._set_speaking(True)
            try:
                self.mouth.say(answer)
            finally:
                self._set_speaking(False)
                if self.mic is not None:
                    self.mic.drain()      # don't hear ourselves
            return answer

    def _gate(self, block) -> bool:
        """Mic gate: muted, speaking, phone active, or asleep (wake word)."""
        host = self.host
        if getattr(self.ui, "muted", False) or self.speaking or self.turn_lock.locked():
            return False
        if getattr(host, "_wake_enabled", False) and not getattr(host, "_awake", True):
            det = getattr(host, "_wake_detector", None)
            if det is not None:
                try:
                    det.feed(block.reshape(-1, 1))
                except Exception:
                    pass
            return False
        return True

    def _voice_ok(self, audio: np.ndarray) -> bool:
        vl = getattr(self.host, "_voice_lock", None)
        try:
            if vl is not None and vl.active:
                return vl.score(audio) >= vl.threshold
        except Exception:
            pass
        return True

    async def run(self, keep_going: Callable[[], bool]) -> None:
        """Listen until keep_going() is False. Typed commands work throughout."""
        loop = asyncio.get_running_loop()
        if getattr(self.host, "_wake_enabled", False):
            try:
                self.host._ensure_wake_detector()     # wake word keeps working offline
            except Exception:
                pass
        if await loop.run_in_executor(None, self.ears.load):
            try:
                self.mic = Mic()
                self.mic.gate = self._gate
                self.mic.on_level = getattr(self.ui, "set_audio_level", None)
                self.mic.open()
                self.log("SYS: Offline voice ready — I'm listening "
                         f"(Whisper {self.ears.model_name}, voice: {self.status()['voice']}).")
            except Exception as e:
                self.mic = None
                self.log(f"SYS: Microphone unavailable offline: {e}")
        else:
            self.log(f"SYS: Offline listening unavailable ({self.ears.error}). "
                     "Typed commands still work.")
        vad = Utterances()
        try:
            while keep_going():
                if self.mic is None:
                    await asyncio.sleep(0.5)
                    continue
                if not self.speaking and not self.turn_lock.locked():
                    self._state("LISTENING")
                try:
                    block = await loop.run_in_executor(None, self.mic.q.get, True, 0.5)
                except queue.Empty:
                    continue
                except Exception:
                    await asyncio.sleep(0.1)
                    continue
                audio = vad.feed(block)
                if audio is None:
                    continue
                if not self._voice_ok(audio):
                    continue
                self._state("THINKING")
                text = await loop.run_in_executor(None, self.ears.transcribe, audio)
                if not text or len(text) < 2:
                    continue
                try:
                    self.host._last_user_speech = time.monotonic()
                except Exception:
                    pass
                self.log(f"You: {text}")
                await loop.run_in_executor(None, self.handle_text, text, True)
                vad.reset()
        finally:
            self.mouth.stop()
            if self.mic is not None:
                self.mic.close()
                self.mic = None
