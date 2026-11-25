"""
core/barge_in.py — talk over Habibi to stop it ("barge-in").

While Habibi speaks, the mic is closed to the AI so it can't hear its own voice
from the speakers. This detector still watches the mic level and compares it
with what Habibi is playing at that moment:

    expected echo  = coupling × (loudness Habibi is playing right now)
    you talking    = mic clearly louder than the expected echo for ~0.3 s

The coupling (how much of the speaker sound reaches the mic) is learned while
you are quiet, so it adapts to your speakers, volume and room. When you talk
over Habibi it stops at once and the start of what you said is passed on, so
nothing you said is lost.
"""
from __future__ import annotations

import time
from collections import deque

import numpy as np

TRIGGER_CHUNKS = 4          # ~0.26 s of 64 ms mic chunks
MIN_LEVEL = 0.02            # quieter than this is never treated as speech
OVER_ECHO = 2.2             # mic must be this many times louder than expected echo
HOLD_CHUNKS = 12            # ~0.8 s of your words kept and sent after stopping


class BargeIn:
    def __init__(self):
        self._out: deque = deque(maxlen=200)   # (start, end, rms) of played audio
        self._k = 1.0                          # learned speaker→mic coupling (starts cautious)
        self._hits = 0
        self._held: deque = deque(maxlen=HOLD_CHUNKS)
        self._cooldown_until = 0.0

    @staticmethod
    def _rms(pcm16: np.ndarray) -> float:
        if not len(pcm16):
            return 0.0
        f = pcm16.astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(f * f)))

    def note_output(self, data: bytes, sample_rate: int = 24000) -> None:
        """Record a chunk Habibi is about to play (called by the speaker loop)."""
        pcm = np.frombuffer(data, dtype=np.int16)
        now = time.monotonic()
        self._out.append((now, now + len(pcm) / sample_rate + 0.25, self._rms(pcm)))

    def _expected(self, now: float) -> float:
        vals = [r for (t0, t1, r) in self._out if t0 - 0.05 <= now <= t1]
        return max(vals) if vals else 0.0

    def feed(self, data: bytes) -> bool:
        """Feed one mic chunk while Habibi is speaking. True = the user is talking over it."""
        self._held.append(data)
        now = time.monotonic()
        rms = self._rms(np.frombuffer(data, dtype=np.int16))
        exp = self._expected(now)
        threshold = max(MIN_LEVEL, self._k * exp * OVER_ECHO)
        if rms > threshold:
            self._hits += 1
        else:
            self._hits = max(0, self._hits - 1)
            if exp > 0.01:             # quiet user → learn how much echo this setup has
                self._k = 0.96 * self._k + 0.04 * min(rms / exp, 2.0)
        if self._hits >= TRIGGER_CHUNKS and now >= self._cooldown_until:
            self._hits = 0
            self._cooldown_until = now + 1.5
            return True
        return False

    def take_held(self) -> list[bytes]:
        """The last ~0.8 s of mic audio (the start of what the user said)."""
        held = list(self._held)
        self._held.clear()
        return held

    def reset(self) -> None:
        self._hits = 0
        self._held.clear()
