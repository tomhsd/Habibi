"""
"Hey Habibi" wake word.

The built-in wake word engine only ships a "Hey Jarvis" model. This plugin lets
Habibi also wake on "Hey Habibi" (or just "Habibi") without training a model:
while Habibi sleeps, short bits of speech are written down on this PC with the
same Whisper speech recognition offline mode uses, and if they contain "Habibi"
Habibi wakes up. Nothing leaves the PC. "Hey Jarvis" keeps working too.

Needs faster-whisper (in both Windows installers and the Python 3.12 copy). Uses the
core sleep_listener hook: the core hands this plugin the mic audio heard while
asleep, and the plugin calls host.wake().
"""
from __future__ import annotations

import queue
import re
import sys
import threading
import time
from pathlib import Path

PLUGIN = {
    "name": "wake_habibi",
    "description": (
        "Status of the 'Hey Habibi' wake word (wakes Habibi from sleep when the user "
        "says 'Hey Habibi'). Use when the user asks whether Habibi can hear its name."
    ),
    "parameters": {"type": "OBJECT", "properties": {}, "required": []},
}

PLUGIN_SETTINGS = {
    "namespace": "wake_habibi",
    "title": "HEY HABIBI WAKE WORD",
    "fields": [
        {"key": "model", "label": "Speech recognition (bigger = better, slower)",
         "type": "choice", "options": ["tiny", "base", "small"], "default": "base"},
    ],
}

SAMPLE_RATE = 16000
MIN_SECONDS, MAX_SECONDS = 0.3, 6.0     # the wake phrase is short; skip long talk

# "habibi" as Whisper may spell it: habibi, habeebi, habibie, hbibi, ha bibi …
_LATIN = re.compile(r"h+[ae]*\s*b+[iey]+b+[iey]+")
_OTHER = ("حبيبي", "হাবিবি", "हबीबी", "हबीबि")


def heard_habibi(text: str) -> bool:
    """True if a transcript contains the wake name. Pure, for tests."""
    if not text:
        return False
    low = text.lower()
    if any(w in text for w in _OTHER):
        return True
    letters = re.sub(r"[^a-z ]+", "", low)
    joined = letters.replace(" ", "")
    return bool(_LATIN.search(letters) or _LATIN.search(joined))


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _load_sibling(stem: str):
    """Import a helper module next to this file (works in the .exe too)."""
    import importlib.util
    name = f"plugins.{stem}"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _model_name() -> str:
    try:
        from memory.config_manager import get_plugin_config
        m = str(get_plugin_config("wake_habibi").get("model") or "base")
        return m if m in ("tiny", "base", "small") else "base"
    except Exception:
        return "base"


class Spotter:
    """Finds 'Habibi' in mic audio heard while asleep. Runs in its own thread."""

    def __init__(self, host, log=print):
        self.host = host
        self.log = log
        self.q: queue.Queue = queue.Queue(maxsize=400)
        self.heard = 0
        self.last_text = ""
        self._thread = threading.Thread(target=self._loop, name="HeyHabibi", daemon=True)
        self._thread.start()

    def feed(self, frame) -> None:
        try:
            data = frame[:, 0] if getattr(frame, "ndim", 1) > 1 else frame
            self.q.put_nowait(data.copy())
        except queue.Full:
            pass
        except Exception:
            pass

    def _asleep(self) -> bool:
        h = self.host
        return bool(getattr(h, "_wake_enabled", False) and not getattr(h, "_awake", True))

    def _loop(self) -> None:
        eng = _load_sibling("_offline_engine")
        ears = eng.Ears(_model_name(), "en")
        if not ears.load():
            self.log(f"SYS: 'Hey Habibi' wake word is off — {ears.error or 'speech recognition missing'}.")
            return
        self.log("SYS: Wake word: also listening for 'Hey Habibi'.")
        vad = eng.Utterances(max_seconds=MAX_SECONDS + 1)
        while True:
            block = self.q.get()
            if not self._asleep():
                vad.reset()
                continue
            audio = vad.feed(block)
            if audio is None:
                continue
            secs = len(audio) / SAMPLE_RATE
            if not (MIN_SECONDS <= secs <= MAX_SECONDS):
                continue
            t0 = time.monotonic()
            text = ears.transcribe(audio)
            self.last_text = text
            print(f"[HeyHabibi] heard {text!r} in {time.monotonic() - t0:.1f}s")
            if text:
                self.log(f"SYS: (asleep) heard: {text}")
            if heard_habibi(text) and self._asleep():
                self.heard += 1
                self._drain()
                vad.reset()
                try:
                    self.host.wake(reason="Hey Habibi")
                except Exception as e:
                    self.log(f"SYS: 'Hey Habibi' couldn't wake Habibi: {e}")

    def _drain(self) -> None:
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass


_spotter: Spotter | None = None


def _say_habibi() -> None:
    """Make the core's 'Say … to wake me' lines name Hey Habibi."""
    try:
        import core.wake_word as ww
        ww.WAKE_PHRASE = "Hey Habibi"
    except Exception:
        pass


def sleep_listener(host):
    """Core hook: return the feed for mic audio heard while asleep."""
    global _spotter
    if not _can_listen():
        try:
            host.ui.write_log("SYS: 'Hey Habibi' needs the offline speech recognition "
                              "(Python 3.12 copy or the Windows installer). 'Hey Jarvis' still works.")
        except Exception:
            pass
        return None
    if _spotter is None:
        def log(t):
            try:
                host.ui.write_log(t)
            except Exception:
                print(t)
        _spotter = Spotter(host, log)
    return _spotter.feed


def _enabled() -> bool:
    try:
        from memory.config_manager import get_plugin_enabled
        return bool(get_plugin_enabled(PLUGIN["name"]))
    except Exception:
        return True


def _can_listen() -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except Exception:
        return False


if _enabled() and _can_listen():
    _say_habibi()


def run(parameters: dict, player=None, session_memory=None) -> str:
    if _spotter is None:
        return ("The 'Hey Habibi' wake word starts when Habibi goes to sleep with the "
                "wake word switched on in settings.")
    return (f"'Hey Habibi' wake word is on. Woke Habibi {_spotter.heard} time(s). "
            f"Last heard: {_spotter.last_text or 'nothing yet'}.")
