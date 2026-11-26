"""
plugins/_eq_ui.py — the EQUALIZER panel that opens inside the MEDIA PLAYER card
(helper for plugins/equalizer.py; the loader skips files starting with "_").

  [⏻ ON] [preset ▾] [💾] [↺]
  response curve (what the settings do to the sound)
  10 band sliders 31 Hz … 16 kHz (±12 dB)
  BASS · BEAT · TREBLE · PREAMP · BALANCE · WIDTH sliders
  [LOUDNESS] [LIMITER]
  where it works right now (local files / YouTube)
"""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QInputDialog,
                             QLabel, QSlider, QVBoxLayout, QWidget)

import ui as U
from ui_media import _btn, _slider_css



def _sibling(stem: str):
    """plugins/<stem>.py by path (plugins/ ships as plain files in the .exe)."""
    import importlib.util
    import sys
    from pathlib import Path
    name = f"plugins.{stem}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{stem}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


D = _sibling("_eq_dsp")


def _vslider_css() -> str:
    return f"""
        QSlider::groove:vertical {{ width: 4px; background: {U.C.BORDER}; border-radius: 2px; }}
        QSlider::add-page:vertical {{ background: {U.C.PRI}; border-radius: 2px; }}
        QSlider::handle:vertical {{ background: {U.C.WHITE}; height: 10px; margin: 0 -4px;
                                    border-radius: 5px; }}
    """


def _lbl(text: str, size: int = 7, color: str | None = None, bold: bool = False) -> QLabel:
    l = QLabel(text)
    l.setFont(U._mono(size, bold))
    l.setStyleSheet(f"color: {color or U.C.TEXT_DIM}; background: transparent;")
    return l


class Curve(QWidget):
    """Frequency response of the current settings, 20 Hz – 20 kHz, ±15 dB."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(54)
        self.freqs = np.geomspace(20, 20000, 120)
        self.db = np.zeros(len(self.freqs))
        self.on = True

    def set_settings(self, s: dict) -> None:
        self.db = D.response_db(s, self.freqs)
        self.on = bool(s["enabled"])
        self.update()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), U.qcol(U.C.BG, 170))
        grid = QPen(QColor(U.C.BORDER)); grid.setWidthF(0.6)
        p.setPen(grid)
        for f in D.FREQS:
            x = W * np.log10(f / 20) / 3
            p.drawLine(QPointF(x, 2), QPointF(x, H - 2))
        mid = H / 2
        p.drawLine(QPointF(0, mid), QPointF(W, mid))
        path = QPainterPath()
        for i, (f, d) in enumerate(zip(self.freqs, self.db)):
            x = W * np.log10(f / 20) / 3
            y = mid - float(np.clip(d, -15, 15)) / 15 * (H / 2 - 4)
            path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
        pen = QPen(QColor(U.C.PRI if self.on else U.C.TEXT_DIM)); pen.setWidthF(2)
        p.setPen(pen)
        p.drawPath(path)
        p.end()


class EqPanel(QWidget):
    """Calls ctl.update(changes) / ctl.preset(name) / ctl.save_preset(name) /
    ctl.reset(); ctl.panel_refresh() calls show_settings() back."""

    def __init__(self, ctl, parent=None):
        super().__init__(parent)
        self.ctl = ctl
        self._busy = False
        self.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 2)
        lay.setSpacing(4)

        head = QHBoxLayout(); head.setSpacing(4)
        self.b_on = _btn("⏻ ON", "Equalizer on / off", 52); self.b_on.setCheckable(True)
        self.preset = QComboBox()
        self.preset.setFont(U._mono(8))
        self.preset.setFixedHeight(26)
        self.preset.setToolTip("Presets (your own ones are at the bottom)")
        self.preset.setStyleSheet(f"""
            QComboBox {{ background: {U.C.PRI_GHO}; color: {U.C.TEXT}; border: 1px solid {U.C.BORDER};
                        border-radius: 4px; padding: 0 4px; }}
            QComboBox QAbstractItemView {{ background: {U.C.BG}; color: {U.C.TEXT};
                        selection-background-color: {U.C.PRI_GHO}; selection-color: {U.C.PRI}; }}
        """)
        self.b_save = _btn("💾", "Save these settings as your own preset", 30)
        self.b_reset = _btn("↺", "Reset everything to flat", 30)
        head.addWidget(self.b_on); head.addWidget(self.preset, 1)
        head.addWidget(self.b_save); head.addWidget(self.b_reset)
        lay.addLayout(head)

        self.curve = Curve()
        lay.addWidget(self.curve)

        bands = QGridLayout(); bands.setHorizontalSpacing(0); bands.setVerticalSpacing(1)
        self.band_sl: list[QSlider] = []
        for i, name in enumerate(D.BAND_LABELS):
            sl = QSlider(Qt.Orientation.Vertical)
            sl.setRange(-120, 120)                       # tenths of a dB
            sl.setFixedHeight(84)
            sl.setStyleSheet(_vslider_css())
            sl.setToolTip(f"{name} Hz")
            sl.valueChanged.connect(lambda v, i=i: self._band(i, v))
            bands.addWidget(sl, 0, i, Qt.AlignmentFlag.AlignHCenter)
            l = _lbl(name, 6)
            bands.addWidget(l, 1, i, Qt.AlignmentFlag.AlignHCenter)
            self.band_sl.append(sl)
        lay.addLayout(bands)

        grid = QGridLayout(); grid.setHorizontalSpacing(6); grid.setVerticalSpacing(2)
        self.knobs: dict[str, tuple[QSlider, QLabel, float]] = {}
        rows = [("bass", "BASS", "Bass boost / cut (low shelf, 100 Hz)", 10),
                ("beat", "BEAT", "Beat punch: makes kick-drum hits stronger", 1),
                ("treble", "TREBLE", "Treble boost / cut (high shelf, 8 kHz)", 10),
                ("preamp", "PREAMP", "Overall level before the EQ", 10),
                ("balance", "BAL", "Left / right balance", 1),
                ("width", "WIDTH", "Stereo width: 0 = mono, 100 = normal, 200 = extra wide", 1)]
        for r, (key, name, tip, scale) in enumerate(rows):
            lo, hi = D.LIMITS[key]
            sl = QSlider(Qt.Orientation.Horizontal)
            sl.setRange(int(lo * scale), int(hi * scale))
            sl.setStyleSheet(_slider_css())
            sl.setToolTip(tip)
            val = _lbl("", 7, U.C.TEXT_MED)
            val.setFixedWidth(40)
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            n = _lbl(name, 7, U.C.TEXT_MED, True); n.setFixedWidth(44); n.setToolTip(tip)
            sl.valueChanged.connect(lambda v, k=key, s=scale: self._knob(k, v / s))
            grid.addWidget(n, r, 0); grid.addWidget(sl, r, 1); grid.addWidget(val, r, 2)
            self.knobs[key] = (sl, val, scale)
        lay.addLayout(grid)

        tog = QHBoxLayout(); tog.setSpacing(4)
        self.b_loud = _btn("LOUDNESS", "More bass and treble for quiet listening"); self.b_loud.setCheckable(True)
        self.b_lim = _btn("LIMITER", "Prevents distortion when you boost a lot"); self.b_lim.setCheckable(True)
        tog.addWidget(self.b_loud); tog.addWidget(self.b_lim)
        lay.addLayout(tog)

        self.note = _lbl("", 7)
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

        self.b_on.toggled.connect(lambda on: self._emit({"enabled": on}))
        self.b_loud.toggled.connect(lambda on: self._emit({"loudness": on}))
        self.b_lim.toggled.connect(lambda on: self._emit({"limiter": on}))
        self.b_reset.clicked.connect(lambda: self.ctl.reset())
        self.b_save.clicked.connect(self._save)
        self.preset.activated.connect(lambda i: self.ctl.preset(self.preset.itemText(i)))

        self._note_t = QTimer(self)
        self._note_t.timeout.connect(lambda: self.note.setText(self.ctl.where_text()))
        self._note_t.start(1500)

    # ── from the controls ────────────────────────────────────────────────────
    def _emit(self, changes: dict) -> None:
        if not self._busy:
            self.ctl.update(changes)

    def _band(self, i: int, v: int) -> None:
        if self._busy:
            return
        bands = list(self.ctl.settings["bands"])
        bands[i] = v / 10.0
        self.band_sl[i].setToolTip(f"{D.BAND_LABELS[i]} Hz: {v / 10:+.1f} dB")
        self.ctl.update({"bands": bands})

    def _knob(self, key: str, v: float) -> None:
        self.knobs[key][1].setText(self._fmt(key, v))
        self._emit({key: v})

    def _save(self) -> None:
        name, ok = QInputDialog.getText(self, "Save preset", "Name for your preset:")
        if ok and name.strip():
            self.ctl.save_preset(name.strip())

    @staticmethod
    def _fmt(key: str, v: float) -> str:
        if key in ("bass", "treble", "preamp"):
            return f"{v:+.1f}dB"
        if key == "balance":
            return "C" if abs(v) < 0.5 else f"{'R' if v > 0 else 'L'}{abs(v):.0f}"
        return f"{v:.0f}%"

    # ── to the controls ──────────────────────────────────────────────────────
    def show_settings(self, s: dict, preset_names: list[str]) -> None:
        self._busy = True
        try:
            self.b_on.setChecked(s["enabled"])
            self.b_on.setText("⏻ ON" if s["enabled"] else "⏻ OFF")
            items = [self.preset.itemText(i) for i in range(self.preset.count())]
            names = list(preset_names)
            cur = s["preset"] if s["preset"] in names else "Custom"
            if cur == "Custom":
                names = names + ["Custom"]
            if items != names:
                self.preset.clear()
                self.preset.addItems(names)
            self.preset.setCurrentText(cur)
            for sl, g in zip(self.band_sl, s["bands"]):
                sl.setValue(int(round(g * 10)))
            for i, sl in enumerate(self.band_sl):
                sl.setToolTip(f"{D.BAND_LABELS[i]} Hz: {s['bands'][i]:+.1f} dB")
            for key, (sl, val, scale) in self.knobs.items():
                sl.setValue(int(round(s[key] * scale)))
                val.setText(self._fmt(key, s[key]))
            self.b_loud.setChecked(s["loudness"])
            self.b_lim.setChecked(s["limiter"])
            self.curve.set_settings(s)
            self.note.setText(self.ctl.where_text())
            for w in self.band_sl + [k[0] for k in self.knobs.values()] + [self.b_loud, self.preset]:
                w.setEnabled(True)
        finally:
            self._busy = False
