"""
plugins/_eq_dsp.py — the sound engine behind plugins/equalizer.py (helper, not a
plugin: the loader skips files that start with "_").

Settings (one dict, saved in config/equalizer.json):
  enabled    master on/off
  bands      10 gains in dB (-12..+12) at 31, 62, 125, 250, 500, 1k, 2k, 4k, 8k, 16k Hz
  preamp     dB (-12..+12) before everything else
  bass       dB (-12..+15) low shelf at 100 Hz
  treble     dB (-12..+12) high shelf at 8 kHz
  beat       0..100  punch: kick-drum hits (40-160 Hz) are made stronger than the
             rest of the bass (transient shaper), so beats hit harder without
             making the whole mix boomy
  balance    -100 (left) .. +100 (right)
  width      0 (mono) .. 100 (normal) .. 200 (extra wide stereo)
  loudness   classic loudness contour (more low and high end for quiet listening)
  limiter    stops clipping/distortion when you boost a lot

Two renderers share these settings and the same filter maths (RBJ biquads, the
same filters Web Audio uses):
  • Processor   numpy, for files played by Habibi's own player: all the
                frequency shaping is folded into one minimum-phase FIR (no added
                delay) applied with FFT overlap-add, then beat, stereo and limiter.
  • WEB_JS      Web Audio graph injected into the YouTube player page.
"""
from __future__ import annotations

import json
import math

import numpy as np

FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
BAND_LABELS = ["31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]
BAND_Q = 1.41
BASS_F, TREBLE_F = 100.0, 8000.0
LOUD_LOW = (80.0, 7.0)        # loudness contour: low shelf (Hz, dB)
LOUD_HIGH = (10000.0, 4.0)    # and high shelf
KICK_F, KICK_Q = 90.0, 0.8    # beat: the kick-drum band

LIMITS = {"preamp": (-12.0, 12.0), "bass": (-12.0, 15.0), "treble": (-12.0, 12.0),
          "beat": (0.0, 100.0), "balance": (-100.0, 100.0), "width": (0.0, 200.0),
          "band": (-12.0, 12.0)}

DEFAULTS = {
    "enabled": True,
    "bands": [0.0] * 10,
    "preamp": 0.0,
    "bass": 0.0,
    "treble": 0.0,
    "beat": 0.0,
    "balance": 0.0,
    "width": 100.0,
    "loudness": False,
    "limiter": True,
    "preset": "Flat",
}

# preset: bands + the tone settings it implies (balance, preamp, limiter stay yours)
PRESETS: dict[str, dict] = {
    "Flat":           {"bands": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]},
    "Bass Boost":     {"bands": [6, 5, 4, 2, 0, 0, 0, 0, 0, 0]},
    "Deep Bass":      {"bands": [9, 7, 4, 1, 0, 0, 0, 0, 0, 0], "beat": 20},
    "Bass & Beat":    {"bands": [6, 5, 3, 0, -1, 0, 1, 2, 3, 3], "beat": 60},
    "Party":          {"bands": [5, 4, 2, 0, -1, 0, 2, 3, 4, 4], "beat": 45, "width": 120},
    "Hip-Hop":        {"bands": [6, 5, 2, 1, -1, -1, 1, 0, 1, 2], "beat": 40},
    "EDM":            {"bands": [6, 5, 2, 0, 0, -2, -1, 1, 3, 4], "beat": 50},
    "Rock":           {"bands": [4, 3, 2, 0, -1, -1, 1, 2, 3, 4], "beat": 20},
    "Pop":            {"bands": [-1, 1, 3, 4, 3, 0, -1, -1, 1, 2]},
    "Bollywood":      {"bands": [4, 3, 1, 0, 1, 2, 2, 2, 2, 1], "beat": 20},
    "Jazz":           {"bands": [3, 2, 1, 2, -1, -1, 0, 1, 2, 3]},
    "Classical":      {"bands": [4, 3, 2, 1, -1, -1, 0, 2, 3, 4]},
    "Acoustic":       {"bands": [3, 3, 2, 1, 1, 1, 2, 2, 2, 1]},
    "Vocal":          {"bands": [-2, -2, -1, 1, 3, 4, 4, 3, 1, 0]},
    "Podcast":        {"bands": [-6, -4, -2, 0, 2, 3, 3, 2, 0, -2], "width": 80},
    "Treble Boost":   {"bands": [0, 0, 0, 0, 0, 0, 2, 4, 6, 7]},
    "Movie":          {"bands": [4, 3, 1, 0, 0, 1, 1, 1, 2, 2], "width": 130, "beat": 15},
    "Headphones":     {"bands": [3, 2, 1, 0, -1, 0, 1, 2, 2, 2]},
    "Laptop Speakers": {"bands": [-4, -2, 2, 3, 2, 1, 1, 2, 2, 1], "loudness": True},
    "Late Night":     {"bands": [-2, -1, 0, 0, 0, 0, 0, -1, -2, -3], "loudness": True},
}
_PRESET_TONE = {"bass": 0.0, "treble": 0.0, "beat": 0.0, "width": 100.0, "loudness": False}


def clamp(key: str, v) -> float:
    lo, hi = LIMITS[key]
    return float(max(lo, min(hi, float(v))))


def normalise(s: dict | None) -> dict:
    """A complete, valid settings dict from anything (old file, voice, UI)."""
    out = json.loads(json.dumps(DEFAULTS))
    s = s or {}
    for k in ("enabled", "loudness", "limiter"):
        if k in s:
            out[k] = bool(s[k])
    for k in ("preamp", "bass", "treble", "beat", "balance", "width"):
        if k in s:
            try:
                out[k] = clamp(k, s[k])
            except (TypeError, ValueError):
                pass
    b = s.get("bands")
    if isinstance(b, (list, tuple)):
        vals = []
        for i in range(10):
            try:
                vals.append(clamp("band", b[i]))
            except (IndexError, TypeError, ValueError):
                vals.append(0.0)
        out["bands"] = vals
    if isinstance(s.get("preset"), str):
        out["preset"] = s["preset"][:40]
    return out


def apply_preset(s: dict, preset: dict, name: str) -> dict:
    s = dict(s)
    for k, v in _PRESET_TONE.items():
        s[k] = preset.get(k, v)
    s["bands"] = [float(x) for x in preset.get("bands", [0] * 10)]
    for k in ("preamp", "balance", "limiter"):        # only custom presets carry these
        if k in preset:
            s[k] = preset[k]
    s["enabled"] = True
    s["preset"] = name
    return normalise(s)


def is_neutral(s: dict) -> bool:
    """True when the settings don't change the sound (the limiter alone doesn't
    count: on unboosted audio it does nothing)."""
    return (not any(abs(b) > 0.05 for b in s["bands"]) and abs(s["preamp"]) < 0.05
            and abs(s["bass"]) < 0.05 and abs(s["treble"]) < 0.05 and s["beat"] < 0.5
            and abs(s["balance"]) < 0.5 and abs(s["width"] - 100) < 0.5 and not s["loudness"])


def wants_processing(s: dict) -> bool:
    return bool(s["enabled"]) and not is_neutral(s)


def describe(s: dict) -> str:
    if not s["enabled"]:
        return "Equalizer is off."
    bands = ", ".join(f"{l} {g:+.0f}" for l, g in zip(BAND_LABELS, s["bands"]))
    bal = s["balance"]
    bal_t = "centre" if abs(bal) < 0.5 else f"{abs(bal):.0f}% {'right' if bal > 0 else 'left'}"
    return (f"Equalizer on, preset {s['preset']}. Bands (dB): {bands}. Bass {s['bass']:+.0f} dB, "
            f"treble {s['treble']:+.0f} dB, beat {s['beat']:.0f}%, preamp {s['preamp']:+.0f} dB, "
            f"balance {bal_t}, stereo width {s['width']:.0f}%, loudness "
            f"{'on' if s['loudness'] else 'off'}, limiter {'on' if s['limiter'] else 'off'}.")


# ══════════════════════════════════════════════════════════════════════════════
#  Filter maths (RBJ cookbook = Web Audio's BiquadFilterNode)
# ══════════════════════════════════════════════════════════════════════════════

def _biquad(kind: str, f: float, rate: float, gain_db: float = 0.0, q: float = 0.7071):
    if f >= rate / 2 * 0.98:
        return None
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f / rate
    c, sn = math.cos(w0), math.sin(w0)
    if kind == "peaking":
        al = sn / (2 * q)
        b = (1 + al * A, -2 * c, 1 - al * A)
        a = (1 + al / A, -2 * c, 1 - al / A)
    elif kind == "lowshelf":                          # Web Audio: shelf slope S = 1
        al = sn / 2 * math.sqrt(2.0)
        sa = 2 * math.sqrt(A) * al
        b = (A * ((A + 1) - (A - 1) * c + sa), 2 * A * ((A - 1) - (A + 1) * c),
             A * ((A + 1) - (A - 1) * c - sa))
        a = ((A + 1) + (A - 1) * c + sa, -2 * ((A - 1) + (A + 1) * c), (A + 1) + (A - 1) * c - sa)
    elif kind == "highshelf":
        al = sn / 2 * math.sqrt(2.0)
        sa = 2 * math.sqrt(A) * al
        b = (A * ((A + 1) + (A - 1) * c + sa), -2 * A * ((A - 1) + (A + 1) * c),
             A * ((A + 1) + (A - 1) * c - sa))
        a = ((A + 1) - (A - 1) * c + sa, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - sa)
    elif kind == "bandpass":                          # 0 dB peak
        al = sn / (2 * q)
        b = (al, 0.0, -al)
        a = (1 + al, -2 * c, 1 - al)
    else:
        raise ValueError(kind)
    return b, a


def _response(coefs, w: np.ndarray) -> np.ndarray:
    """|H| at normalised angular frequencies w (radians/sample)."""
    if coefs is None:
        return np.ones_like(w)
    b, a = coefs
    z1 = np.exp(-1j * w)
    z2 = z1 * z1
    return np.abs((b[0] + b[1] * z1 + b[2] * z2) / (a[0] + a[1] * z1 + a[2] * z2))


def tone_filters(s: dict, rate: float) -> list:
    """Every static filter, in the same order as the Web Audio graph."""
    out = [_biquad("peaking", f, rate, g, BAND_Q) for f, g in zip(FREQS, s["bands"]) if abs(g) > 0.01]
    if abs(s["bass"]) > 0.01:
        out.append(_biquad("lowshelf", BASS_F, rate, s["bass"]))
    if abs(s["treble"]) > 0.01:
        out.append(_biquad("highshelf", TREBLE_F, rate, s["treble"]))
    if s["loudness"]:
        out.append(_biquad("lowshelf", LOUD_LOW[0], rate, LOUD_LOW[1]))
        out.append(_biquad("highshelf", LOUD_HIGH[0], rate, LOUD_HIGH[1]))
    return out


def magnitude(s: dict, rate: float, freqs: np.ndarray) -> np.ndarray:
    """Overall linear gain of the tone settings at `freqs` (Hz), incl. preamp."""
    w = 2 * np.pi * np.asarray(freqs, dtype=float) / rate
    m = np.full(w.shape, 10 ** (s["preamp"] / 20.0))
    for c in tone_filters(s, rate):
        m = m * _response(c, w)
    return m


def response_db(s: dict, freqs, rate: float = 48000.0) -> np.ndarray:
    """For the curve on the panel."""
    if not s["enabled"]:
        return np.zeros(len(freqs))
    return 20 * np.log10(np.maximum(magnitude(s, rate, np.asarray(freqs)), 1e-6))


def minimum_phase_fir(mag: np.ndarray, taps: int) -> np.ndarray:
    """Minimum-phase impulse response (no pre-ringing, ~no delay) with the given
    magnitude on an rfft grid (homomorphic / folded-cepstrum method)."""
    nfft = (len(mag) - 1) * 2
    cep = np.fft.irfft(np.log(np.maximum(mag, 1e-5)), nfft)
    fold = np.zeros(nfft)
    fold[0] = cep[0]
    fold[1:nfft // 2] = 2 * cep[1:nfft // 2]
    fold[nfft // 2] = cep[nfft // 2]
    h = np.fft.irfft(np.exp(np.fft.rfft(fold)), nfft)[:taps]
    fade = taps // 4
    h[-fade:] *= 0.5 * (1 + np.cos(np.linspace(0, np.pi, fade)))
    return h


# ══════════════════════════════════════════════════════════════════════════════
#  numpy renderer (local files)
# ══════════════════════════════════════════════════════════════════════════════

class _Conv:
    """Streaming FFT overlap-add convolution with a fixed FIR, any block size."""

    def __init__(self, h: np.ndarray):
        self.h = h.astype(np.float64)
        self.L = len(h)
        self._H: dict[int, np.ndarray] = {}
        self.carry: np.ndarray | None = None

    def spectrum(self, n: int) -> np.ndarray:
        if n not in self._H:
            self._H[n] = np.fft.rfft(self.h, n)
        return self._H[n]

    def run(self, X: np.ndarray, N: int, n: int, ch: int, carry=None) -> tuple[np.ndarray, np.ndarray]:
        """X: rfft of the block (N points, per channel). Returns (output n×ch,
        new carry). `carry` overrides the stored one (for crossfades)."""
        y = np.fft.irfft(X * self.spectrum(N)[:, None], N, axis=0)[: n + self.L - 1]
        c = self.carry if carry is None else carry
        if c is not None and c.shape[1] == ch:
            y[: self.L - 1] += c
        return y[:n], y[n: n + self.L - 1].copy()


class Processor:
    """Applies the settings to blocks of audio: process(frames n×ch float32, rate).
    Keeps filter state between blocks; call reset() after a seek or a new track."""

    TAPS = 4096
    SUB = 64          # envelope / limiter resolution (samples)

    def __init__(self, settings: dict | None = None):
        self.s = normalise(settings)
        self._rate = 0
        self._eq: _Conv | None = None
        self._kick: _Conv | None = None
        self._old: tuple | None = None       # previous filters, for a click-free change
        self._dirty = True
        self.reset()

    # settings
    def set(self, settings: dict) -> None:
        new = normalise(settings)
        if new != self.s:
            self.s = new
            self._dirty = True

    def active(self) -> bool:
        return wants_processing(self.s)

    def reset(self) -> None:
        self._fast = self._slow = 1e-4
        self._lim_g = 1.0
        self._beat_last = None
        for c in (self._eq, self._kick):
            if c is not None:
                c.carry = None

    # design
    def _design(self, rate: int) -> None:
        nfft = 4 * self.TAPS
        f = np.fft.rfftfreq(nfft, 1.0 / rate)
        mag = magnitude(self.s, rate, f)
        old = (self._eq, self._kick) if self._eq is not None and self._rate == rate else None
        eq = _Conv(minimum_phase_fir(mag, self.TAPS))
        kick = None
        if self.s["beat"] > 0.5:
            bp = _response(_biquad("bandpass", KICK_F, rate, q=KICK_Q), 2 * np.pi * f / rate)
            kick = _Conv(minimum_phase_fir(mag * bp * bp, self.TAPS))   # 2nd-order band
        self._old = old
        self._eq, self._kick, self._rate, self._dirty = eq, kick, rate, False

    # processing
    def process(self, frames: np.ndarray, rate: int) -> np.ndarray:
        x = np.asarray(frames, dtype=np.float64)
        if x.ndim == 1:
            x = x[:, None]
        n, ch = x.shape
        if n == 0 or not self.s["enabled"]:
            return x.astype(np.float32)
        if self._dirty or rate != self._rate:
            self._design(rate)
        N = 1 << int(math.ceil(math.log2(n + self.TAPS - 1)))
        X = np.fft.rfft(x, N, axis=0)

        y, new_carry = self._eq.run(X, N, n, ch)
        kick = None
        if self._kick is not None:
            kick, k_carry = self._kick.run(X, N, n, ch)
        if self._old is not None:                      # crossfade old → new filters
            o_eq, o_kick = self._old
            y_old, _ = o_eq.run(X, N, n, ch)
            fade = np.linspace(0.0, 1.0, n)[:, None]
            y = y_old * (1 - fade) + y * fade
            if kick is not None and o_kick is not None:
                k_old, _ = o_kick.run(X, N, n, ch)
                kick = k_old * (1 - fade) + kick * fade
            self._old = None
        self._eq.carry = new_carry
        if kick is not None:
            self._kick.carry = k_carry
            y = y + kick * self._beat_gain(kick, n)[:, None]

        if ch == 2:
            y = self._stereo(y)
        if self.s["limiter"]:
            y = self._limit(y, n)
        return np.clip(y, -1.0, 1.0).astype(np.float32)

    def _subblocks(self, sig: np.ndarray, n: int) -> tuple[np.ndarray, int]:
        k = max(1, n // self.SUB)
        use = k * self.SUB if n >= self.SUB else n
        size = use // k
        return sig[:use].reshape(k, size), k

    def _expand(self, g: np.ndarray, n: int, start: float) -> np.ndarray:
        """Per-sub-block gains → per-sample, ramping smoothly from `start`."""
        k = len(g)
        pts = np.concatenate([[start], g])
        xs = np.linspace(0, n, k + 1)
        return np.interp(np.arange(n) + 1, xs, pts)

    def _beat_gain(self, kick: np.ndarray, n: int) -> np.ndarray:
        """Extra kick-band gain: a steady lift plus a lot more on the attack of
        each hit (fast envelope above the slow one = a beat is starting)."""
        a = self.s["beat"] / 100.0
        mono = np.abs(kick).max(axis=1)
        blocks, k = self._subblocks(mono, n)
        rms = np.sqrt(np.mean(blocks ** 2, axis=1)) + 1e-6
        g = np.empty(k)
        fast, slow = self._fast, self._slow
        sub_s = n / k / max(1, self._rate)
        fa = math.exp(-sub_s / 0.004)          # 4 ms attack follower
        sa = math.exp(-sub_s / 0.120)          # 120 ms body follower
        for i, r in enumerate(rms):
            fast = max(r, fast * fa)
            slow = slow * sa + r * (1 - sa)
            t = min(1.5, max(0.0, fast / (slow + 1e-6) - 1.0))
            g[i] = 0.35 * a + 1.6 * a * t
        start = g[0] if self._beat_last is None else self._beat_last
        self._fast, self._slow, self._beat_last = fast, slow, g[-1]
        return self._expand(g, n, start)

    def _stereo(self, y: np.ndarray) -> np.ndarray:
        m = stereo_matrix(self.s)
        if np.allclose(m, np.eye(2)):
            return y
        return y @ m.T

    def _limit(self, y: np.ndarray, n: int) -> np.ndarray:
        ceil = 0.97
        peak = np.abs(y).max(axis=1)
        blocks, k = self._subblocks(peak, n)
        need = np.minimum(1.0, ceil / np.maximum(blocks.max(axis=1), 1e-9))
        if n > k * blocks.shape[1]:                     # tail that didn't fit a sub-block
            need[-1] = min(need[-1], ceil / max(peak[k * blocks.shape[1]:].max(), 1e-9))
        g = np.empty(k)
        cur = self._lim_g
        rel = math.exp(-(n / k / max(1, self._rate)) / 0.15)    # 150 ms release
        for i, nd in enumerate(need):
            cur = min(nd, 1.0 - (1.0 - cur) * rel)
            g[i] = cur
        start = self._lim_g
        self._lim_g = cur
        gs = self._expand(g, n, start)
        # never let the ramp sit above what a sub-block needs (no overshoot)
        idx = np.minimum(np.arange(n) // blocks.shape[1], k - 1)
        gs = np.minimum(gs, need[idx])
        return y * gs[:, None]


def stereo_matrix(s: dict) -> np.ndarray:
    """2×2 output = M @ [L, R]: stereo width (mid/side) then balance."""
    w = s["width"] / 100.0
    m = np.array([[(1 + w) / 2, (1 - w) / 2], [(1 - w) / 2, (1 + w) / 2]])
    b = s["balance"] / 100.0
    gl, gr = min(1.0, 1.0 - b), min(1.0, 1.0 + b)
    return np.diag([gl, gr]) @ m


# ══════════════════════════════════════════════════════════════════════════════
#  Web Audio renderer (YouTube player page)
# ══════════════════════════════════════════════════════════════════════════════
# Injected (QWebEngineScript, all frames) into the YouTube profile. In the
# youtube.com player frame it routes every <video> through a Web Audio graph
# with the same filters. The Habibi page forwards settings with postMessage and
# the frame answers {habibiEqAck} so the plugin can tell it worked.

WEB_JS = r"""
(function(){
  if (window.__habibiEqInstalled) return; window.__habibiEqInstalled = true;
  var FREQS = %(freqs)s, Q = %(q)s, S = null, ctx = null, hooked = [];
  function mk(){
    if (!ctx) { try { ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch(e) { return null; } }
    return ctx;
  }
  function graph(v){
    var c = mk(); if (!c) return null;
    var g = {v: v};
    try { g.src = c.createMediaElementSource(v); } catch(e) { return null; }
    var up = c.createGain(); up.channelCount = 2; up.channelCountMode = 'explicit';
    up.channelInterpretation = 'speakers';
    g.pre = c.createGain();
    g.bands = FREQS.map(function(f){ var b = c.createBiquadFilter(); b.type='peaking';
      b.frequency.value=f; b.Q.value=Q; b.gain.value=0; return b; });
    function shelf(t,f){ var b=c.createBiquadFilter(); b.type=t; b.frequency.value=f; b.gain.value=0; return b; }
    g.bass = shelf('lowshelf', %(bass_f)s); g.treble = shelf('highshelf', %(treble_f)s);
    g.lLow = shelf('lowshelf', %(ll_f)s); g.lHigh = shelf('highshelf', %(lh_f)s);
    var chain = [up, g.pre].concat(g.bands, [g.bass, g.treble, g.lLow, g.lHigh]);
    g.src.connect(chain[0]);
    for (var i = 0; i < chain.length - 1; i++) chain[i].connect(chain[i+1]);
    var tone = chain[chain.length-1];
    // beat: kick band through a slow-attack compressor passes each hit's attack
    // and squashes the tail; added on top it makes beats punchier.
    g.kbp = c.createBiquadFilter(); g.kbp.type='bandpass'; g.kbp.frequency.value=%(kick_f)s; g.kbp.Q.value=%(kick_q)s;
    g.kbp2 = c.createBiquadFilter(); g.kbp2.type='bandpass'; g.kbp2.frequency.value=%(kick_f)s; g.kbp2.Q.value=%(kick_q)s;
    g.kcomp = c.createDynamicsCompressor(); g.kcomp.threshold.value=-40; g.kcomp.knee.value=0;
    g.kcomp.ratio.value=20; g.kcomp.attack.value=0.03; g.kcomp.release.value=0.12;
    g.kgain = c.createGain(); g.kgain.gain.value = 0;
    tone.connect(g.kbp); g.kbp.connect(g.kbp2); g.kbp2.connect(g.kcomp); g.kcomp.connect(g.kgain);
    g.mix = c.createGain(); tone.connect(g.mix); g.kgain.connect(g.mix);
    // stereo width + balance: 2x2 matrix
    var sp = c.createChannelSplitter(2), mg = c.createChannelMerger(2);
    g.m = [c.createGain(), c.createGain(), c.createGain(), c.createGain()];  // LL, RL, LR, RR
    g.mix.connect(sp);
    sp.connect(g.m[0], 0); sp.connect(g.m[1], 1); sp.connect(g.m[2], 0); sp.connect(g.m[3], 1);
    g.m[0].connect(mg, 0, 0); g.m[1].connect(mg, 0, 0); g.m[2].connect(mg, 0, 1); g.m[3].connect(mg, 0, 1);
    g.lim = c.createDynamicsCompressor(); g.lim.threshold.value=-1; g.lim.knee.value=0;
    g.lim.ratio.value=20; g.lim.attack.value=0.002; g.lim.release.value=0.1;
    g.out = mg; g.limOn = null;
    v.addEventListener('play', function(){ if (ctx && ctx.state !== 'running') ctx.resume(); });
    return g;
  }
  function route(g, lim){
    if (g.limOn === lim) return;
    try { g.out.disconnect(); g.lim.disconnect(); } catch(e) {}
    if (lim) { g.out.connect(g.lim); g.lim.connect(ctx.destination); }
    else { g.out.connect(ctx.destination); }
    g.limOn = lim;
  }
  function setp(p, v){ try { p.setTargetAtTime(v, ctx.currentTime, 0.03); } catch(e) { p.value = v; } }
  function apply(g){
    var on = S.enabled, i;
    setp(g.pre.gain, Math.pow(10, (on ? S.preamp : 0) / 20));
    for (i = 0; i < g.bands.length; i++) setp(g.bands[i].gain, on ? S.bands[i] : 0);
    setp(g.bass.gain, on ? S.bass : 0); setp(g.treble.gain, on ? S.treble : 0);
    setp(g.lLow.gain, on && S.loudness ? %(ll_g)s : 0); setp(g.lHigh.gain, on && S.loudness ? %(lh_g)s : 0);
    setp(g.kgain.gain, on ? 3.0 * S.beat / 100 : 0);
    var w = on ? S.width / 100 : 1, b = on ? S.balance / 100 : 0;
    var gl = Math.min(1, 1 - b), gr = Math.min(1, 1 + b);
    setp(g.m[0].gain, gl * (1 + w) / 2); setp(g.m[1].gain, gl * (1 - w) / 2);
    setp(g.m[2].gain, gr * (1 - w) / 2); setp(g.m[3].gain, gr * (1 + w) / 2);
    route(g, !!(on && S.limiter));
  }
  function sync(){
    if (!S) return 0;
    var vids = document.querySelectorAll('video'), n = 0;
    for (var i = 0; i < vids.length; i++) {
      var v = vids[i], g = null;
      for (var j = 0; j < hooked.length; j++) if (hooked[j].v === v) g = hooked[j];
      if (!g && S.active) { g = graph(v); if (g) hooked.push(g); }
      if (g) { apply(g); n++; }
    }
    if (ctx && ctx.state !== 'running' && n) ctx.resume();
    return n;
  }
  window.addEventListener('message', function(ev){
    var d = ev.data;
    if (!d || typeof d !== 'object' || !d.habibiEq) return;
    S = d.habibiEq;
    var n = sync();
    try { if (ev.source) ev.source.postMessage({habibiEqAck: {videos: n,
      state: ctx ? ctx.state : 'none', host: location.hostname}}, '*'); } catch(e) {}
  });
  setInterval(sync, 1500);
})();
""" % {"freqs": json.dumps(FREQS), "q": BAND_Q, "bass_f": BASS_F, "treble_f": TREBLE_F,
       "ll_f": LOUD_LOW[0], "ll_g": LOUD_LOW[1], "lh_f": LOUD_HIGH[0], "lh_g": LOUD_HIGH[1],
       "kick_f": KICK_F, "kick_q": KICK_Q}


def web_settings(s: dict) -> dict:
    d = dict(s)
    d["active"] = wants_processing(s)
    return d


def web_forward_js(s: dict) -> str:
    """Run in the Habibi YouTube page (main frame): pass the settings to the
    player frame(s) and return the frame's last answer."""
    return ("(function(s){if(!window.__habibiEqListen){window.__habibiEqListen=1;"
            "window.addEventListener('message',function(e){if(e.data&&e.data.habibiEqAck)"
            "window.__habibiEqAck=e.data.habibiEqAck;});}"
            "var f=document.querySelectorAll('iframe');"
            "for(var i=0;i<f.length;i++){try{f[i].contentWindow.postMessage({habibiEq:s},'*');}catch(e){}}"
            "return JSON.stringify(window.__habibiEqAck||null);})(" + json.dumps(web_settings(s)) + ")")
