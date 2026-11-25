"""
core/voice_lock.py — "only obey my voice".

Habibi records a voiceprint of the owner once (⚙ → 🔒 VOICE LOCK → RECORD MY
VOICE). After that, every time someone starts speaking, the first ~1 second is
held back and compared with the voiceprint:

    • it's you      → the held second plus the rest of what you say goes to Habibi
    • someone else  → Habibi receives silence until they stop talking

This applies to the PC microphone and the phone microphone (both pass through
main._send_realtime). Typing is never blocked.

Engine: sherpa-onnx with the 3D-Speaker CAM++ zh+en speaker model (27 MB,
downloaded once to config/models/). On clean recordings it separated speakers
perfectly from 1 s of speech: owner ≥ 0.52, others ≤ 0.39 → threshold 0.45
(Normal) / 0.52 (Strict). The voiceprint (config/voiceprint.json) is a list of
numbers that describes your voice; it cannot be turned back into audio.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

MODEL_NAME = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
             "speaker-recongition-models/" + MODEL_NAME)
SR = 16000
THRESHOLDS = {"normal": 0.45, "strict": 0.52}
DECIDE_AFTER_S = 1.0          # speech needed before deciding
END_AFTER_SILENCE_S = 1.2     # an utterance ends after this much silence
MIN_ENROLL_SPEECH_S = 6.0


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


class VoiceLock:
    def __init__(self, log=print):
        self.log = log
        cfg_dir = _base_dir() / "config"
        self.model_path = cfg_dir / "models" / MODEL_NAME
        self.print_path = cfg_dir / "voiceprint.json"
        self._ex = None
        self._ex_lock = threading.Lock()
        self.cfg = {"enabled": False, "strictness": "normal", "embedding": None}
        try:
            self.cfg.update(json.loads(self.print_path.read_text(encoding="utf-8")))
        except Exception:
            pass
        self._reset()
        self.enrolling = False
        self._enroll_frames: list[np.ndarray] = []
        self._enroll_until = 0.0
        self._enroll_done = None
        self._last_block_log = 0.0

    # ── setup ────────────────────────────────────────────────────────────────

    @staticmethod
    def engine_installed() -> bool:
        try:
            import sherpa_onnx  # noqa: F401
            return True
        except Exception:
            return False

    @property
    def enrolled(self) -> bool:
        return bool(self.cfg.get("embedding"))

    @property
    def active(self) -> bool:
        return bool(self.cfg.get("enabled")) and self.enrolled and self._extractor() is not None

    def ensure_model(self) -> tuple[bool, str]:
        """Download the speaker model once (blocking — call from a worker thread)."""
        if not self.engine_installed():
            return False, "the voice engine (sherpa-onnx) is not installed"
        if self.model_path.exists() and self.model_path.stat().st_size > 1_000_000:
            return True, "ready"
        try:
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.model_path.with_suffix(".part")
            self.log("SYS: Voice lock — downloading the voice model (27 MB, one time)…")
            urllib.request.urlretrieve(MODEL_URL, tmp)
            tmp.replace(self.model_path)
            return True, "ready"
        except Exception as e:
            return False, f"could not download the voice model: {e}"

    def _extractor(self):
        if self._ex is not None:
            return self._ex
        if not self.model_path.exists() or not self.engine_installed():
            return None
        with self._ex_lock:
            if self._ex is None:
                try:
                    import sherpa_onnx as so
                    self._ex = so.SpeakerEmbeddingExtractor(
                        so.SpeakerEmbeddingExtractorConfig(model=str(self.model_path), num_threads=1))
                except Exception as e:
                    self.log(f"ERR: Voice lock engine failed to start — {e}")
                    return None
        return self._ex

    def _embed(self, f32: np.ndarray) -> np.ndarray:
        ex = self._extractor()
        s = ex.create_stream()
        s.accept_waveform(SR, f32)
        s.input_finished()
        e = np.asarray(ex.compute(s), dtype=np.float32)
        return e / (np.linalg.norm(e) + 1e-9)

    def score(self, f32: np.ndarray) -> float:
        ref = np.asarray(self.cfg["embedding"], dtype=np.float32)
        return float(self._embed(f32) @ ref)

    def save(self) -> None:
        self.print_path.parent.mkdir(parents=True, exist_ok=True)
        self.print_path.write_text(json.dumps(self.cfg), encoding="utf-8")

    def set_enabled(self, on: bool) -> None:
        self.cfg["enabled"] = bool(on)
        self._reset()
        self.save()

    def set_strictness(self, level: str) -> None:
        self.cfg["strictness"] = level if level in THRESHOLDS else "normal"
        self.save()

    def forget(self) -> None:
        self.cfg.update(enabled=False, embedding=None)
        self._reset()
        self.save()

    @property
    def threshold(self) -> float:
        return THRESHOLDS.get(self.cfg.get("strictness", "normal"), THRESHOLDS["normal"])

    # ── enrollment ───────────────────────────────────────────────────────────

    def start_enroll(self, seconds: float, on_done) -> None:
        """Collect the next `seconds` of microphone audio as the owner's voice.
        on_done(ok: bool, message: str) is called from a worker thread."""
        self._enroll_frames = []
        self._enroll_until = time.time() + seconds
        self._enroll_done = on_done
        self.enrolling = True

    def _finish_enroll(self) -> None:
        self.enrolling = False
        frames, done = self._enroll_frames, self._enroll_done
        self._enroll_frames, self._enroll_done = [], None
        try:
            ok, msg = self._build_voiceprint(frames)
        except Exception as e:
            ok, msg = False, f"voice lock error: {e}"
        if done:
            done(ok, msg)

    @staticmethod
    def _speech_only(frames: list[np.ndarray]) -> np.ndarray:
        if not frames:
            return np.zeros(0, dtype=np.float32)
        rms = np.array([np.sqrt(np.mean(f * f)) for f in frames])
        thr = max(0.006, float(np.percentile(rms, 30)) * 2.0)
        keep = [f for f, r in zip(frames, rms) if r > thr]
        return np.concatenate(keep) if keep else np.zeros(0, dtype=np.float32)

    def _build_voiceprint(self, frames: list[np.ndarray]) -> tuple[bool, str]:
        ok, why = self.ensure_model()
        if not ok:
            return False, why
        speech = self._speech_only(frames)
        secs = len(speech) / SR
        if secs < MIN_ENROLL_SPEECH_S:
            return False, (f"I only heard {secs:.0f} seconds of your voice — please read the "
                           f"lines aloud for the whole time and try again.")
        win, hop = int(1.5 * SR), int(0.75 * SR)
        embs = [self._embed(speech[i:i + win]) for i in range(0, len(speech) - win + 1, hop)]
        ref = np.mean(embs, axis=0)
        ref /= np.linalg.norm(ref) + 1e-9
        self_sim = float(np.min([e @ ref for e in embs]))
        self.cfg.update(embedding=[round(float(x), 6) for x in ref], enabled=True,
                        enrolled_at=int(time.time()), self_similarity=round(self_sim, 3))
        self._reset()
        self.save()
        return True, f"voiceprint saved from {secs:.0f} seconds of speech"

    # ── live gate ────────────────────────────────────────────────────────────

    def _reset(self) -> None:
        self._state = "idle"            # idle → collect → open | blocked → idle
        self._held: list[bytes] = []
        self._held_f: list[np.ndarray] = []
        self._speech_s = 0.0
        self._silence_s = 0.0
        self._noise = 0.003

    async def process(self, data: bytes, loop) -> list[bytes]:
        """Gate one chunk of 16 kHz PCM16 audio. Returns the chunks to send now
        (possibly none while deciding, or several when releasing held audio)."""
        pcm = np.frombuffer(data, dtype=np.int16)
        f = pcm.astype(np.float32) / 32768.0
        dur = len(pcm) / SR
        silent = bytes(len(data))

        if self.enrolling:
            self._enroll_frames.append(f)
            if time.time() >= self._enroll_until:
                loop.run_in_executor(None, self._finish_enroll)
            return [silent]               # enrollment speech is not sent to the AI

        if not self.active:
            return [data]

        rms = float(np.sqrt(np.mean(f * f))) if len(f) else 0.0
        speech = rms > max(0.012, self._noise * 3.0)
        if self._state == "idle" and not speech:
            self._noise = 0.95 * self._noise + 0.05 * rms

        if self._state == "idle":
            if not speech:
                return [silent]
            self._state, self._held, self._held_f = "collect", [data], [f]
            self._speech_s, self._silence_s = dur, 0.0
            return []

        if self._state == "collect":
            self._held.append(data)
            self._held_f.append(f)
            if speech:
                self._speech_s += dur
                self._silence_s = 0.0
            else:
                self._silence_s += dur
            enough = self._speech_s >= DECIDE_AFTER_S or (
                self._silence_s >= 0.5 and self._speech_s >= 0.5)
            if enough:
                audio = np.concatenate(self._held_f)
                try:
                    sim = await loop.run_in_executor(None, self.score, audio)
                except Exception:
                    sim = 1.0             # never lock the owner out on an engine error
                held, self._held, self._held_f = self._held, [], []
                if sim >= self.threshold:
                    self._state = "open"
                    return held
                self._state = "blocked"
                if time.time() - self._last_block_log > 20:
                    self._last_block_log = time.time()
                    self.log(f"SYS: Voice lock — ignored a voice that isn't yours "
                             f"(match {max(sim, 0) * 100:.0f}%).")
                return [bytes(len(c)) for c in held]
            if self._silence_s >= 0.8:     # just a short noise
                held = self._held
                self._reset_utterance()
                return [bytes(len(c)) for c in held]
            return []

        # open / blocked: follow the utterance until a pause
        self._silence_s = 0.0 if speech else self._silence_s + dur
        was_open = self._state == "open"
        if self._silence_s >= END_AFTER_SILENCE_S:
            self._reset_utterance()
        return [data] if was_open else [silent]

    def _reset_utterance(self) -> None:
        self._state, self._held, self._held_f = "idle", [], []
        self._speech_s = self._silence_s = 0.0
