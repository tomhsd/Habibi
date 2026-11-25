"""
ui_media.py — Habibi's built-in audio/video player (no browser).

  • MediaEngine   plays files from the PC with Qt Multimedia (FFmpeg backend:
                  mp3, m4a, aac, flac, wav, ogg, opus, wma, mp4, mkv, webm, avi,
                  mov…), keeps a playlist, and measures bass/mid/treble live.
  • MediaCard     the MEDIA PLAYER card in the left sidebar: now playing, seek,
                  ⏮ ⏯ ⏹ ⏭, shuffle, repeat, volume, open files/folder, playlist.
  • Spectrum      rainbow bars that move with the music.
  • RgbAnimator   while audio plays, the whole interface cycles through RGB
                  colours, faster and brighter on loud parts and bass hits, and
                  the reactor pulses with the music. Your own colour comes back
                  when playback stops or pauses.

The AI controls it through actions/media_player.py → HabibiUI.media().
"""
from __future__ import annotations

import colorsys
import math
import random
import time
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QObject, QRectF, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPen, QBrush
from PyQt6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QListWidget,
                             QListWidgetItem, QPushButton, QSlider, QVBoxLayout,
                             QWidget)

import ui as U

AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".oga", ".opus",
             ".wma", ".aiff", ".aif", ".alac", ".mka"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".m4v", ".mpg",
             ".mpeg", ".3gp", ".ts", ".flv"}
MEDIA_EXT = AUDIO_EXT | VIDEO_EXT


def is_media(p: Path) -> bool:
    return p.suffix.lower() in MEDIA_EXT


def collect(paths, limit: int = 2000) -> list[Path]:
    """Media files from files and folders (folders searched inside, sorted)."""
    out: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            try:
                files = sorted((f for f in p.rglob("*") if f.is_file() and is_media(f)),
                               key=lambda f: str(f).lower())
            except Exception:
                files = []
            out.extend(files)
        elif p.is_file() and is_media(p):
            out.append(p)
        if len(out) >= limit:
            break
    return out[:limit]


def _fmt(ms: int) -> str:
    s = max(0, int(ms // 1000))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ══════════════════════════════════════════════════════════════════════════════
#  Engine
# ══════════════════════════════════════════════════════════════════════════════

class MediaEngine(QObject):
    changed = pyqtSignal()            # track / state / playlist changed
    position = pyqtSignal(int, int)   # position ms, duration ms
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
        self._QMP = QMediaPlayer
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self._vol = 70
        self._duck = False
        self.audio.setVolume(0.7)
        self.playlist: list[Path] = []
        self.index = -1
        self.shuffle = False
        self.repeat = "all"           # off | all | one
        self._order: list[int] = []

        # live analysis: (bass, mid, treble, overall), each 0..1
        self.bands = np.zeros(16)
        self.levels = (0.0, 0.0, 0.0, 0.0)
        self._last_buf = 0.0
        self._meter = None
        self._peak_avg = 0.05
        self._setup_analysis()

        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.playbackStateChanged.connect(lambda *_: self.changed.emit())
        self.player.positionChanged.connect(lambda p: self.position.emit(p, self.player.duration()))
        self.player.durationChanged.connect(lambda d: self.position.emit(self.player.position(), d))
        self.player.errorOccurred.connect(self._on_error)
        self.player.hasVideoChanged.connect(lambda *_: self.changed.emit())

    # ── analysis ─────────────────────────────────────────────────────────────
    def _setup_analysis(self) -> None:
        try:                                            # Qt ≥ 6.8: the decoded audio itself
            from PyQt6.QtMultimedia import QAudioBufferOutput
            self._abo = QAudioBufferOutput(self)
            self.player.setAudioBufferOutput(self._abo)
            self._abo.audioBufferReceived.connect(self._on_buffer)
        except Exception:
            self._abo = None
        try:                                            # Windows output meter (fallback)
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
            dev = AudioUtilities.GetSpeakers()
            dev = getattr(dev, "_dev", dev)
            iface = dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
            from ctypes import POINTER, cast
            self._meter = cast(iface, POINTER(IAudioMeterInformation))
        except Exception:
            self._meter = None

    def _on_buffer(self, buf) -> None:
        try:
            fmt = buf.format()
            n = buf.byteCount()
            if n <= 0:
                return
            ptr = buf.constData()
            ptr.setsize(n)
            from PyQt6.QtMultimedia import QAudioFormat
            sf = fmt.sampleFormat()
            if sf == QAudioFormat.SampleFormat.Float:
                a = np.frombuffer(bytes(ptr), dtype=np.float32)
            elif sf == QAudioFormat.SampleFormat.Int16:
                a = np.frombuffer(bytes(ptr), dtype=np.int16).astype(np.float32) / 32768.0
            elif sf == QAudioFormat.SampleFormat.Int32:
                a = np.frombuffer(bytes(ptr), dtype=np.int32).astype(np.float32) / 2147483648.0
            elif sf == QAudioFormat.SampleFormat.UInt8:
                a = (np.frombuffer(bytes(ptr), dtype=np.uint8).astype(np.float32) - 128) / 128.0
            else:
                return
            ch = max(1, fmt.channelCount())
            a = a[: len(a) - len(a) % ch].reshape(-1, ch).mean(axis=1)
            self.analyse(a, fmt.sampleRate() or 44100)
            self._last_buf = time.time()
        except Exception:
            pass

    def analyse(self, mono: np.ndarray, rate: int) -> None:
        """Spectrum → 16 log-spaced bands + bass/mid/treble/overall (0..1)."""
        if len(mono) < 256:
            return
        x = mono[-2048:] * np.hanning(min(2048, len(mono)))
        spec = np.abs(np.fft.rfft(x)) / (len(x) / 2)
        freqs = np.fft.rfftfreq(len(x), 1.0 / rate)
        edges = np.geomspace(30, min(16000, rate / 2 - 1), 17)
        bands = np.zeros(16)
        for i in range(16):
            m = (freqs >= edges[i]) & (freqs < edges[i + 1])
            if m.any():
                bands[i] = spec[m].max()
        bands = np.clip(np.log10(1 + 60 * bands) / 1.4, 0, 1)
        self.bands = np.maximum(bands, self.bands * 0.82)          # fall slowly
        bass = float(self.bands[:4].mean())
        mid = float(self.bands[4:10].mean())
        treble = float(self.bands[10:].mean())
        rms = float(np.sqrt(np.mean(mono[-2048:] ** 2)))
        overall = min(1.0, rms * 3.2)
        self.levels = (bass, mid, treble, overall)

    def poll_levels(self) -> tuple[float, float, float, float]:
        """Current levels; uses the Windows meter / a gentle pulse when no
        buffers arrive (older Qt)."""
        if not self.playing():
            self.bands *= 0.85
            self.levels = tuple(v * 0.85 for v in self.levels)
            return self.levels
        if time.time() - self._last_buf < 0.4:
            return self.levels
        peak = None
        if self._meter is not None:
            try:
                peak = float(self._meter.GetPeakValue())
            except Exception:
                peak = None
        t = time.time()
        if peak is None:                                 # no analysis available: musical-looking pulse
            peak = 0.35 + 0.25 * abs(math.sin(t * 3.7)) + 0.1 * random.random()
        self._peak_avg = self._peak_avg * 0.97 + peak * 0.03
        kick = max(0.0, peak - self._peak_avg * 1.25) * 2.5
        fake = np.array([peak * (0.6 + 0.4 * abs(math.sin(t * (1.3 + i * 0.37) + i)))
                         for i in range(16)])
        fake[:4] = np.clip(fake[:4] + kick, 0, 1)
        self.bands = np.maximum(np.clip(fake, 0, 1), self.bands * 0.82)
        self.levels = (float(self.bands[:4].mean()), float(self.bands[4:10].mean()),
                       float(self.bands[10:].mean()), min(1.0, peak))
        return self.levels

    # ── playlist ─────────────────────────────────────────────────────────────
    def load(self, paths, play: bool = True, append: bool = False) -> int:
        files = collect(paths)
        if not files:
            return 0
        if append and self.playlist:
            self.playlist.extend(files)
        else:
            self.playlist = files
            self.index = -1
        self._reshuffle()
        if play:
            self.play_index(len(self.playlist) - len(files) if append else
                            (self._order[0] if self.shuffle else 0))
        self.changed.emit()
        return len(files)

    def _reshuffle(self) -> None:
        self._order = list(range(len(self.playlist)))
        if self.shuffle:
            random.shuffle(self._order)

    def set_shuffle(self, on: bool) -> None:
        self.shuffle = on
        self._reshuffle()
        self.changed.emit()

    def cycle_repeat(self) -> str:
        self.repeat = {"off": "all", "all": "one", "one": "off"}[self.repeat]
        self.changed.emit()
        return self.repeat

    def play_index(self, i: int) -> None:
        if not (0 <= i < len(self.playlist)):
            return
        self.index = i
        self.player.setSource(QUrl.fromLocalFile(str(self.playlist[i])))
        self.player.play()
        self.changed.emit()

    def _step(self, d: int) -> bool:
        if not self.playlist:
            return False
        pos = self._order.index(self.index) if self.index in self._order else -1
        nxt = pos + d
        if not 0 <= nxt < len(self._order):
            if self.repeat == "off":
                return False
            nxt %= len(self._order)
        self.play_index(self._order[nxt])
        return True

    def next(self) -> bool:
        return self._step(1)

    def previous(self) -> bool:
        if self.player.position() > 4000:
            self.player.setPosition(0)
            return True
        return self._step(-1)

    def toggle(self) -> None:
        if self.playing():
            self.player.pause()
        elif self.index >= 0:
            self.player.play()
        elif self.playlist:
            self.play_index(0)

    def stop(self) -> None:
        self.player.stop()
        self.changed.emit()

    def seek(self, ms: int) -> None:
        self.player.setPosition(max(0, int(ms)))

    def set_volume(self, pct: int) -> None:
        self._vol = max(0, min(100, int(pct)))
        self._apply_volume()
        self.changed.emit()

    def duck(self, on: bool) -> None:
        """Lower the music while Habibi is talking, so you hear her and the
        mic doesn't mistake the music for you."""
        if on != self._duck:
            self._duck = on
            self._apply_volume()

    def _apply_volume(self) -> None:
        self.audio.setVolume(self._vol / 100.0 * (0.25 if self._duck else 1.0))

    def volume(self) -> int:
        return self._vol

    # ── state ────────────────────────────────────────────────────────────────
    def playing(self) -> bool:
        return self.player.playbackState() == self._QMP.PlaybackState.PlayingState

    def paused(self) -> bool:
        return self.player.playbackState() == self._QMP.PlaybackState.PausedState

    def has_video(self) -> bool:
        return bool(self.player.hasVideo())

    def current(self) -> Path | None:
        return self.playlist[self.index] if 0 <= self.index < len(self.playlist) else None

    def title(self) -> str:
        try:
            from PyQt6.QtMultimedia import QMediaMetaData
            md = self.player.metaData()
            t = md.stringValue(QMediaMetaData.Key.Title)
            a = md.stringValue(QMediaMetaData.Key.ContributingArtist) or \
                md.stringValue(QMediaMetaData.Key.AlbumArtist)
            if t:
                return f"{t} — {a}" if a else t
        except Exception:
            pass
        c = self.current()
        return c.stem if c else ""

    def status_text(self) -> str:
        c = self.current()
        if c is None:
            return "Nothing is loaded."
        st = "Playing" if self.playing() else "Paused" if self.paused() else "Stopped"
        kind = "video" if self.has_video() else "audio"
        return (f"{st} {kind}: {self.title()} ({c.name}), track {self.index + 1} of "
                f"{len(self.playlist)}, {_fmt(self.player.position())} / "
                f"{_fmt(self.player.duration())}, volume {self.volume()}%, "
                f"shuffle {'on' if self.shuffle else 'off'}, repeat {self.repeat}.")

    def _on_status(self, status) -> None:
        if status == self._QMP.MediaStatus.EndOfMedia:
            if self.repeat == "one":
                self.player.setPosition(0)
                self.player.play()
            elif not self.next():
                self.changed.emit()
        elif status == self._QMP.MediaStatus.LoadedMedia:
            self.changed.emit()
        elif status == self._QMP.MediaStatus.InvalidMedia:
            self.error.emit(f"Can't play {self.current().name if self.current() else 'this file'}.")

    def _on_error(self, *_):
        msg = self.player.errorString() or "Playback error"
        self.error.emit(msg)


# ══════════════════════════════════════════════════════════════════════════════
#  Spectrum (rainbow bars)
# ══════════════════════════════════════════════════════════════════════════════

class Spectrum(QWidget):
    def __init__(self, engine: MediaEngine, height: int = 46, parent=None):
        super().__init__(parent)
        self.e = engine
        self.setFixedHeight(height)
        self.hue = 0.0
        self.rgb = True

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), U.qcol(U.C.BG, 160))
        n = len(self.e.bands)
        gap = 3
        bw = (W - gap * (n + 1)) / n
        for i, v in enumerate(self.e.bands):
            h = max(2.0, float(v) * (H - 6))
            x = gap + i * (bw + gap)
            if self.rgb:
                r, g, b = colorsys.hsv_to_rgb((self.hue + i / n) % 1.0, 0.85, 1.0)
                col = QColor(int(r * 255), int(g * 255), int(b * 255))
            else:
                col = QColor(U.C.PRI)
            grad = QLinearGradient(x, H, x, H - h)
            c0 = QColor(col); c0.setAlpha(110)
            grad.setColorAt(0.0, c0)
            grad.setColorAt(1.0, col)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(QRectF(x, H - 3 - h, bw, h), 2, 2)
        p.end()


# ══════════════════════════════════════════════════════════════════════════════
#  RGB animation of the whole interface
# ══════════════════════════════════════════════════════════════════════════════

class RgbAnimator(QObject):
    """Cycles the interface colour with the music while audio plays."""

    def __init__(self, win, engine: MediaEngine, spectrum: Spectrum):
        super().__init__(win)
        self.win, self.e, self.spec = win, engine, spectrum
        self.enabled = True
        self.active = False
        self.hue = 0.0
        self._step_q = -1
        self._last_full = 0.0
        self._t = QTimer(self)
        self._t.timeout.connect(self._tick)
        self._t.start(33)

    def _saved_accent(self) -> str:
        try:
            c = (U._read_full_config().get("ui_color") or "").strip()
        except Exception:
            c = ""
        return c or U.DEFAULT_UI_COLOR

    def _apply(self, hex_color: str, full: bool) -> None:
        """Painted parts follow every step; stylesheets (panels, buttons,
        borders) are re-mapped from the colours they actually hold."""
        if U.apply_ui_accent(hex_color) and full:
            new = U.current_palette()
            U.retheme_all_widgets(self._sheet_palette, new)
            self._sheet_palette = new

    def _tick(self) -> None:
        bass, mid, treble, overall = self.e.poll_levels()
        try:
            self.e.duck(bool(getattr(self.win.hud, "speaking", False)))
        except Exception:
            pass
        self.spec.hue = self.hue
        self.spec.update()
        want = self.enabled and self.e.playing() and not self.e.has_video()
        if want:
            if not self.active:
                self.active = True
                self._sheet_palette = U.current_palette()
            # speed: slow drift + loudness + a jump on bass hits
            self.hue = (self.hue + 0.0025 + 0.012 * overall + 0.03 * max(0.0, bass - 0.55)) % 1.0
            q = int(self.hue * 90)                       # 90 colour steps around the wheel
            if q != self._step_q:
                self._step_q = q
                r, g, b = colorsys.hsv_to_rgb(q / 90.0, 1.0, 1.0)
                hexc = "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))
                now = time.time()
                full = now - self._last_full > 0.5            # panels/borders twice a second
                if full:
                    self._last_full = now
                self._apply(hexc, full)
            try:
                self.win.hud.set_audio_level(min(1.0, overall * 1.1 + bass * 0.4))
            except Exception:
                pass
        elif self.active:
            self.active = False
            self._step_q = -1
            self._apply(self._saved_accent(), True)          # your own colour comes back


# ══════════════════════════════════════════════════════════════════════════════
#  Sidebar card
# ══════════════════════════════════════════════════════════════════════════════

def _btn(text: str, tip: str, w: int = 0) -> QPushButton:
    b = QPushButton(text)
    b.setToolTip(tip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFont(U._mono(10, True))
    if w:
        b.setFixedWidth(w)
    b.setFixedHeight(26)
    b.setStyleSheet(f"""
        QPushButton {{ background: {U.C.PRI_GHO}; color: {U.C.TEXT};
                      border: 1px solid {U.C.BORDER}; border-radius: 4px; padding: 0 4px; }}
        QPushButton:hover {{ color: {U.C.PRI}; border-color: {U.C.PRI_DIM}; }}
        QPushButton:checked {{ color: {U.C.BG}; background: {U.C.PRI}; border-color: {U.C.PRI}; }}
    """)
    return b


def _slider_css() -> str:
    return f"""
        QSlider::groove:horizontal {{ height: 4px; background: {U.C.BORDER}; border-radius: 2px; }}
        QSlider::sub-page:horizontal {{ background: {U.C.PRI}; border-radius: 2px; }}
        QSlider::handle:horizontal {{ background: {U.C.WHITE}; width: 10px; margin: -4px 0;
                                      border-radius: 5px; }}
    """


class MediaCard(U.CornerCard):
    want_video = pyqtSignal(bool)        # show the video in the reactor area or not

    def __init__(self, engine: MediaEngine, parent=None):
        super().__init__("MEDIA PLAYER", parent)
        self.e = engine
        lay = self.body()

        self.spectrum = Spectrum(engine)
        lay.addWidget(self.spectrum)

        self.now = QLabel("No media — open files or ask Habibi")
        self.now.setFont(U._mono(9, True))
        self.now.setStyleSheet(f"color: {U.C.WHITE}; background: transparent;")
        self.now.setWordWrap(False)
        lay.addWidget(self.now)

        row = QHBoxLayout(); row.setSpacing(6)
        self.t_pos = QLabel("0:00"); self.t_dur = QLabel("0:00")
        for l in (self.t_pos, self.t_dur):
            l.setFont(U._mono(8)); l.setStyleSheet(f"color: {U.C.TEXT_DIM}; background: transparent;")
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setStyleSheet(_slider_css())
        self.seek.setRange(0, 0)
        self._dragging = False
        self.seek.sliderPressed.connect(lambda: setattr(self, "_dragging", True))
        self.seek.sliderReleased.connect(self._seek_release)
        row.addWidget(self.t_pos); row.addWidget(self.seek, 1); row.addWidget(self.t_dur)
        lay.addLayout(row)

        ctr = QHBoxLayout(); ctr.setSpacing(4)
        self.b_prev = _btn("⏮", "Previous"); self.b_play = _btn("▶", "Play / pause")
        self.b_stop = _btn("⏹", "Stop"); self.b_next = _btn("⏭", "Next")
        self.b_shuf = _btn("🔀", "Shuffle"); self.b_shuf.setCheckable(True)
        self.b_rep = _btn("🔁", "Repeat: all"); self.b_rep.setCheckable(True); self.b_rep.setChecked(True)
        for b in (self.b_prev, self.b_play, self.b_stop, self.b_next, self.b_shuf, self.b_rep):
            ctr.addWidget(b)
        lay.addLayout(ctr)
        self.b_prev.clicked.connect(engine.previous)
        self.b_play.clicked.connect(engine.toggle)
        self.b_stop.clicked.connect(engine.stop)
        self.b_next.clicked.connect(engine.next)
        self.b_shuf.toggled.connect(engine.set_shuffle)
        self.b_rep.clicked.connect(self._repeat)

        vol = QHBoxLayout(); vol.setSpacing(6)
        self.vol_lbl = QLabel("🔊 70"); self.vol_lbl.setFont(U._mono(8))
        self.vol_lbl.setStyleSheet(f"color: {U.C.TEXT_MED}; background: transparent;")
        self.vol_lbl.setFixedWidth(44)
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 100); self.vol.setValue(engine.volume())
        self.vol.setStyleSheet(_slider_css())
        self.vol.valueChanged.connect(engine.set_volume)
        vol.addWidget(self.vol_lbl); vol.addWidget(self.vol, 1)
        lay.addLayout(vol)

        opt = QHBoxLayout(); opt.setSpacing(4)
        self.b_files = _btn("📂 FILES", "Open audio or video files")
        self.b_folder = _btn("📁 FOLDER", "Play a whole folder")
        self.b_list = _btn("☰", "Show playlist", 30); self.b_list.setCheckable(True)
        self.b_rgb = _btn("🌈", "RGB colours with the music", 30); self.b_rgb.setCheckable(True)
        self.b_rgb.setChecked(True)
        self.b_vid = _btn("🎬", "Show the video", 30); self.b_vid.setCheckable(True)
        self.b_vid.setChecked(True); self.b_vid.setVisible(False)
        for b in (self.b_files, self.b_folder, self.b_list, self.b_rgb, self.b_vid):
            opt.addWidget(b)
        lay.addLayout(opt)
        self.b_files.clicked.connect(self._open_files)
        self.b_folder.clicked.connect(self._open_folder)
        self.b_list.toggled.connect(lambda on: self.listw.setVisible(on))
        self.b_vid.toggled.connect(lambda on: self.want_video.emit(on and self.e.has_video()))

        self.listw = QListWidget()
        self.listw.setVisible(False)
        self.listw.setMaximumHeight(150)
        self.listw.setFont(U._mono(8))
        self.listw.setStyleSheet(f"""
            QListWidget {{ background: {U.C.BG}; color: {U.C.TEXT_MED};
                          border: 1px solid {U.C.BORDER}; border-radius: 3px; }}
            QListWidget::item:selected {{ background: {U.C.PRI_GHO}; color: {U.C.PRI}; }}
        """)
        self.listw.itemDoubleClicked.connect(lambda it: engine.play_index(self.listw.row(it)))
        lay.addWidget(self.listw)

        engine.changed.connect(self.refresh)
        engine.position.connect(self._pos)
        engine.error.connect(lambda m: self.set_extra("ERROR", U.C.RED) or self.now.setText(m[:60]))
        self._last_list = None
        self.refresh()

    def _repeat(self):
        mode = self.e.cycle_repeat()
        self.b_rep.setChecked(mode != "off")
        self.b_rep.setText("🔂" if mode == "one" else "🔁")
        self.b_rep.setToolTip(f"Repeat: {mode}")

    def _seek_release(self):
        self._dragging = False
        self.e.seek(self.seek.value())

    def _pos(self, pos: int, dur: int):
        self.t_pos.setText(_fmt(pos)); self.t_dur.setText(_fmt(dur))
        if self.seek.maximum() != dur:
            self.seek.setRange(0, max(0, dur))
        if not self._dragging:
            self.seek.setValue(pos)

    def _start_dir(self) -> str:
        c = self.e.current()
        if c:
            return str(c.parent)
        for d in (Path.home() / "Music", Path.home() / "Videos", Path.home()):
            if d.exists():
                return str(d)
        return ""

    def _open_files(self):
        exts = " ".join(f"*{e}" for e in sorted(MEDIA_EXT))
        files, _ = QFileDialog.getOpenFileNames(self, "Open media", self._start_dir(),
                                                f"Media ({exts});;All files (*)")
        if files:
            self.e.load(files)

    def _open_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Play folder", self._start_dir())
        if d:
            n = self.e.load([d])
            if not n:
                self.now.setText("No audio or video in that folder.")

    def refresh(self):
        e = self.e
        playing = e.playing()
        self.b_play.setText("⏸" if playing else "▶")
        c = e.current()
        if c is None:
            self.set_extra("IDLE", U.C.TEXT_DIM)
        else:
            self.now.setText(self.fontMetrics().elidedText(
                e.title() or c.stem, Qt.TextElideMode.ElideRight, 260))
            self.now.setToolTip(str(c))
            state = "PLAYING" if playing else "PAUSED" if e.paused() else "STOPPED"
            self.set_extra(f"{state}  {e.index + 1}/{len(e.playlist)}",
                           U.C.GREEN if playing else U.C.ACC2)
        self.vol_lbl.setText(f"🔊 {e.volume()}")
        if self.vol.value() != e.volume():
            self.vol.blockSignals(True); self.vol.setValue(e.volume()); self.vol.blockSignals(False)
        self.b_shuf.blockSignals(True); self.b_shuf.setChecked(e.shuffle); self.b_shuf.blockSignals(False)
        key = (id(e.playlist), len(e.playlist))
        if key != self._last_list:
            self._last_list = key
            self.listw.clear()
            for i, p in enumerate(e.playlist[:2000]):
                QListWidgetItem(f"{i + 1:>3}. {p.stem}", self.listw)
        if 0 <= e.index < self.listw.count():
            self.listw.setCurrentRow(e.index)
        vid = e.has_video()
        self.b_vid.setVisible(vid)
        self.want_video.emit(vid and self.b_vid.isChecked() and c is not None
                             and (playing or e.paused()))


# ══════════════════════════════════════════════════════════════════════════════
#  Commands from the AI (actions/media_player.py)
# ══════════════════════════════════════════════════════════════════════════════

def run_command(engine: MediaEngine, card: MediaCard, rgb: RgbAnimator, cmd: str, a: dict) -> str:
    cmd = (cmd or "").lower().strip()
    if cmd == "play":
        paths = a.get("paths") or []
        if paths:
            n = engine.load(paths, play=True, append=bool(a.get("append")))
            if not n:
                return "No audio or video files found there."
            if a.get("append"):
                return f"Added {n} file(s) to the playlist ({len(engine.playlist)} in total)."
            c = engine.current()
            return (f"Now playing {c.name if c else ''} — track {engine.index + 1} of "
                    f"{len(engine.playlist)} in Habibi's player.")
        if not engine.playing():
            engine.toggle()
        return engine.status_text()
    if cmd in ("pause",):
        engine.player.pause(); return engine.status_text()
    if cmd in ("resume",):
        if not engine.playing():
            engine.toggle()
        return engine.status_text()
    if cmd == "toggle":
        engine.toggle(); return engine.status_text()
    if cmd == "stop":
        engine.stop(); return "Stopped."
    def _now() -> str:
        c = engine.current()
        return f"Now playing {c.name} — track {engine.index + 1} of {len(engine.playlist)}." if c else "Nothing is loaded."
    if cmd == "next":
        return _now() if engine.next() else "That was the last track."
    if cmd == "previous":
        engine.previous(); return _now()
    if cmd == "seek":
        sec = float(a.get("seconds", 0))
        if a.get("relative"):
            engine.seek(engine.player.position() + sec * 1000)
        else:
            engine.seek(sec * 1000)
        return engine.status_text()
    if cmd == "volume":
        v = a.get("volume")
        if v is None:
            return f"Volume is {engine.volume()}%."
        v = int(v)
        if a.get("relative"):
            v = engine.volume() + v
        engine.set_volume(v); return f"Volume {engine.volume()}%."
    if cmd == "shuffle":
        engine.set_shuffle(bool(a.get("on", not engine.shuffle)))
        return f"Shuffle {'on' if engine.shuffle else 'off'}."
    if cmd == "repeat":
        mode = str(a.get("mode", "")).lower()
        if mode in ("off", "all", "one"):
            engine.repeat = mode; engine.changed.emit()
        else:
            card._repeat()
        return f"Repeat {engine.repeat}."
    if cmd == "rgb":
        on = bool(a.get("on", not rgb.enabled))
        rgb.enabled = on
        card.b_rgb.setChecked(on)
        return f"RGB colours {'on' if on else 'off'}."
    if cmd == "list":
        if not engine.playlist:
            return "The playlist is empty."
        rows = [f"{'▶' if i == engine.index else ' '} {i + 1}. {p.name}" for i, p in enumerate(engine.playlist[:40])]
        more = f"\n… {len(engine.playlist) - 40} more" if len(engine.playlist) > 40 else ""
        return "Playlist:\n" + "\n".join(rows) + more
    if cmd == "play_index":
        i = int(a.get("index", 1)) - 1
        if not 0 <= i < len(engine.playlist):
            return f"There is no track {i + 1}."
        engine.play_index(i); return _now()
    if cmd == "status":
        return engine.status_text()
    return "Unknown media command."
