"""
plugins/_trading_ui.py — the MARKETS card in Habibi's left sidebar.

  • live watchlist: price, today's change and a mini intraday chart per symbol,
    refreshed every 30 seconds
  • type a name or ticker to add it, ✕ to remove
  • click a row for the detail view: 6-month chart with the next-week range
    drawn as a cone, sentiment gauge, next-week and long-term estimates, and
    how often the weekly call has been right on that symbol

Shown through the plugin sidebar_card() hook (see plugins/trading.py). All
network work runs on background threads; results come back through signals.
"""
from __future__ import annotations

import threading

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QBrush
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QVBoxLayout, QWidget)

import ui as U

REFRESH_MS = 30_000


def _btn(text: str, tip: str, w: int = 0) -> QPushButton:
    b = QPushButton(text)
    b.setToolTip(tip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFont(U._mono(9, True))
    if w:
        b.setFixedWidth(w)
    b.setFixedHeight(24)
    b.setStyleSheet(f"""
        QPushButton {{ background: {U.C.PRI_GHO}; color: {U.C.TEXT};
                      border: 1px solid {U.C.BORDER}; border-radius: 4px; padding: 0 4px; }}
        QPushButton:hover {{ color: {U.C.PRI}; border-color: {U.C.PRI_DIM}; }}
    """)
    return b


def _lbl(text: str = "", color: str | None = None, size: int = 8, bold: bool = False,
         wrap: bool = False) -> QLabel:
    l = QLabel(text)
    l.setFont(U._mono(size, bold))
    l.setStyleSheet(f"color: {color or U.C.TEXT_MED}; background: transparent;")
    l.setWordWrap(wrap)
    return l


def _chg_color(v: float) -> str:
    return U.C.GREEN if v > 0.005 else U.C.RED if v < -0.005 else U.C.TEXT_DIM


def _short(sym: str) -> str:
    return (sym.replace("-USD", "").replace("=X", "").replace("=F", "")
            .replace("^", "").replace(".SR", ""))[:8]


NICE = {"^GSPC": "S&P500", "^IXIC": "NASDAQ", "^DJI": "DOW", "GC=F": "GOLD", "SI=F": "SILVER",
        "CL=F": "OIL", "BZ=F": "BRENT", "^TASI.SR": "TASI", "DX-Y.NYB": "DXY", "^VIX": "VIX",
        "^NDX": "NDX100", "^FTSE": "FTSE", "^N225": "NIKKEI", "BDT=X": "USD/BDT",
        "EURUSD=X": "EUR/USD", "SAR=X": "USD/SAR"}


# ══════════════════════════════════════════════════════════════════════════════
#  Little charts
# ══════════════════════════════════════════════════════════════════════════════

class MiniSpark(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.vals: list[float] = []
        self.base = 0.0
        self.setFixedSize(54, 18)

    def set(self, vals, base):
        self.vals = [v for v in (vals or []) if v]
        self.base = base or (self.vals[0] if self.vals else 0)
        self.update()

    def paintEvent(self, _):
        if len(self.vals) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        lo, hi = min(self.vals + [self.base]), max(self.vals + [self.base])
        rng = (hi - lo) or 1
        y = lambda v: H - 2 - (v - lo) / rng * (H - 4)
        col = QColor(U.C.GREEN if self.vals[-1] >= self.base else U.C.RED)
        p.setPen(QPen(U.qcol(U.C.BORDER_B, 150), 1, Qt.PenStyle.DotLine))
        p.drawLine(QPointF(0, y(self.base)), QPointF(W, y(self.base)))
        path = QPainterPath()
        n = len(self.vals)
        for i, v in enumerate(self.vals):
            pt = QPointF(i / (n - 1) * (W - 1), y(v))
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        p.setPen(QPen(col, 1.4))
        p.drawPath(path)


class ForecastChart(QWidget):
    """Last ~6 months of closes, then the next-week 68 % / 95 % cone."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hist: list[float] = []
        self.week: dict | None = None
        self.setFixedHeight(100)

    def set(self, hist, week):
        self.hist = list(hist or [])[-130:]
        self.week = week
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), U.qcol(U.C.DARK, 170))
        if len(self.hist) < 2:
            p.setPen(U.qcol(U.C.TEXT_DIM))
            p.setFont(U._mono(8))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "loading…")
            return
        w = self.week or {}
        vals = self.hist + [v for v in (w.get("low_95"), w.get("high_95")) if v]
        lo, hi = min(vals), max(vals)
        rng = (hi - lo) or 1
        pad = 6
        cone_w = W * 0.22
        hw = W - cone_w
        y = lambda v: pad + (hi - v) / rng * (H - 2 * pad)
        n = len(self.hist)
        x = lambda i: i / (n - 1) * (hw - 2)

        p.setPen(QPen(U.qcol(U.C.BORDER, 110), 1, Qt.PenStyle.DotLine))
        for i in range(1, 4):
            p.drawLine(QPointF(0, H * i / 4), QPointF(W, H * i / 4))

        up = self.hist[-1] >= self.hist[0]
        col = QColor(U.C.GREEN if up else U.C.RED)
        path = QPainterPath()
        for i, v in enumerate(self.hist):
            pt = QPointF(x(i), y(v))
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        fill = QPainterPath(path)
        fill.lineTo(QPointF(x(n - 1), H)); fill.lineTo(QPointF(0, H)); fill.closeSubpath()
        c2 = QColor(col); c2.setAlpha(35)
        p.fillPath(fill, QBrush(c2))
        p.setPen(QPen(col, 1.5))
        p.drawPath(path)

        if w.get("low_68"):
            x0, y0 = x(n - 1), y(self.hist[-1])
            x1 = W - 2
            for lo_k, hi_k, a in (("low_95", "high_95", 40), ("low_68", "high_68", 80)):
                cone = QPainterPath(QPointF(x0, y0))
                cone.lineTo(QPointF(x1, y(w[hi_k]))); cone.lineTo(QPointF(x1, y(w[lo_k])))
                cone.closeSubpath()
                p.fillPath(cone, QBrush(U.qcol(U.C.PRI, a)))
            p.setPen(QPen(U.qcol(U.C.PRI), 1.2, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x0, y0), QPointF(x1, y(w["expected"])))
            p.setPen(U.qcol(U.C.TEXT_DIM))
            p.setFont(U._mono(7))
            p.drawText(QRectF(x0, 0, x1 - x0, 12), Qt.AlignmentFlag.AlignCenter, "1 WK")


class SentimentBar(QWidget):
    """-1 … +1 gauge: red ← neutral → green, with a marker."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.score = None
        self.setFixedHeight(10)

    def set(self, s):
        self.score = s
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        r = QRectF(0, 2, W, H - 4)
        from PyQt6.QtGui import QLinearGradient
        g = QLinearGradient(0, 0, W, 0)
        g.setColorAt(0, QColor(U.C.RED)); g.setColorAt(0.5, QColor(U.C.TEXT_DIM))
        g.setColorAt(1, QColor(U.C.GREEN))
        p.setOpacity(0.55)
        p.fillRect(r, QBrush(g))
        p.setOpacity(1)
        if self.score is not None:
            mx = (max(-1, min(1, self.score)) + 1) / 2 * W
            p.setPen(QPen(QColor(U.C.WHITE), 2))
            p.drawLine(QPointF(mx, 0), QPointF(mx, H))


# ══════════════════════════════════════════════════════════════════════════════
#  Watchlist row
# ══════════════════════════════════════════════════════════════════════════════

class Row(QWidget):
    clicked = pyqtSignal(str)
    remove = pyqtSignal(str)

    def __init__(self, sym: str, parent=None):
        super().__init__(parent)
        self.sym = sym
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._sel = False
        self._style()
        h = QHBoxLayout(self); h.setContentsMargins(4, 2, 2, 2); h.setSpacing(4)
        self.name = _lbl(NICE.get(sym, _short(sym)), U.C.WHITE, 8, True)
        self.name.setFixedWidth(58)
        self.name.setToolTip(sym)
        self.spark = MiniSpark()
        self.price = _lbl("…", U.C.TEXT, 8)
        self.price.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.chg = _lbl("", U.C.TEXT_DIM, 8, True)
        self.chg.setFixedWidth(52)
        self.chg.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.x = QPushButton("✕"); self.x.setFixedSize(14, 14)
        self.x.setToolTip("Remove from watchlist")
        self.x.setCursor(Qt.CursorShape.PointingHandCursor)
        self.x.setStyleSheet(f"QPushButton {{ color: {U.C.TEXT_DIM}; background: transparent;"
                             f" border: none; font-size: 9px; }} QPushButton:hover {{ color: {U.C.RED}; }}")
        self.x.clicked.connect(lambda: self.remove.emit(self.sym))
        for wdg in (self.name, self.spark):
            h.addWidget(wdg)
        h.addWidget(self.price, 1); h.addWidget(self.chg); h.addWidget(self.x)

    def _style(self):
        bg = U.C.CARD_HI if self._sel else "transparent"
        self.setStyleSheet(f"Row {{ background: {bg}; border-radius: 3px; }}")

    def select(self, on: bool):
        self._sel = on; self._style()

    def update_quote(self, q, M):
        if q is None:
            self.price.setText("n/a")
            return
        self.price.setText(M.fmt_price(q.price, q.currency).replace(" USD", ""))
        self.chg.setText(f"{q.change_pct:+.2f}%")
        self.chg.setStyleSheet(f"color: {_chg_color(q.change_pct)}; background: transparent;")
        self.spark.set(q.intraday, q.prev_close)
        self.name.setToolTip(f"{q.name} ({q.symbol}) · {q.exchange}")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.sym)


# ══════════════════════════════════════════════════════════════════════════════
#  Card
# ══════════════════════════════════════════════════════════════════════════════

class MarketsCard(U.CornerCard):
    _quotes_ready = pyqtSignal(object)        # {sym: Quote}
    _detail_ready = pyqtSignal(str, object)   # sym, {"q","s","f"}
    _engine_evt = pyqtSignal(str, object)     # events from voice commands (any thread)
    _added = pyqtSignal(str, str)             # query, resolved symbol or ""

    def __init__(self, engine, parent=None):
        super().__init__("MARKETS", parent)
        self.M = engine
        self.rows: dict[str, Row] = {}
        self.sel: str | None = None
        self._busy = False
        lay = self.body()

        top = QHBoxLayout(); top.setSpacing(4)
        self.inp = QLineEdit()
        self.inp.setPlaceholderText("Add: bitcoin, AAPL, gold…")
        self.inp.setFont(U._mono(8))
        self.inp.setFixedHeight(24)
        self.inp.setStyleSheet(f"""QLineEdit {{ background: {U.C.DARK}; color: {U.C.TEXT};
            border: 1px solid {U.C.BORDER}; border-radius: 4px; padding: 0 5px; }}
            QLineEdit:focus {{ border-color: {U.C.PRI_DIM}; }}""")
        self.inp.returnPressed.connect(self._add)
        b_add = _btn("+", "Add to watchlist", 26); b_add.clicked.connect(self._add)
        b_ref = _btn("⟳", "Refresh now", 26); b_ref.clicked.connect(self.refresh)
        top.addWidget(self.inp, 1); top.addWidget(b_add); top.addWidget(b_ref)
        lay.addLayout(top)

        self.list_box = QVBoxLayout(); self.list_box.setSpacing(1)
        lay.addLayout(self.list_box)
        self.status = _lbl("", U.C.TEXT_DIM, 7)
        lay.addWidget(self.status)

        # detail view
        self.detail = QWidget(); self.detail.setStyleSheet("background: transparent;")
        d = QVBoxLayout(self.detail); d.setContentsMargins(0, 4, 0, 0); d.setSpacing(4)
        hdr = QHBoxLayout()
        self.d_name = _lbl("", U.C.WHITE, 9, True)
        self.d_name.setWordWrap(True)
        b_close = QPushButton("✕"); b_close.setFixedSize(16, 16)
        b_close.setCursor(Qt.CursorShape.PointingHandCursor)
        b_close.setStyleSheet(f"QPushButton {{ color: {U.C.TEXT_DIM}; background: transparent; border: none; }}"
                              f" QPushButton:hover {{ color: {U.C.PRI}; }}")
        b_close.clicked.connect(lambda: self._select(None))
        hdr.addWidget(self.d_name, 1); hdr.addWidget(b_close)
        d.addLayout(hdr)
        self.d_price = _lbl("", U.C.TEXT, 8)
        d.addWidget(self.d_price)
        self.chart = ForecastChart(); d.addWidget(self.chart)
        srow = QHBoxLayout(); srow.setSpacing(6)
        srow.addWidget(_lbl("SENTIMENT", U.C.TEXT_DIM, 7, True))
        self.s_bar = SentimentBar(); srow.addWidget(self.s_bar, 1)
        self.s_lbl = _lbl("…", U.C.TEXT, 8, True); srow.addWidget(self.s_lbl)
        d.addLayout(srow)
        self.s_info = _lbl("", U.C.TEXT_DIM, 7, wrap=True); d.addWidget(self.s_info)
        self.w_lbl = _lbl("", U.C.TEXT, 8, wrap=True); d.addWidget(self.w_lbl)
        self.l_lbl = _lbl("", U.C.TEXT_MED, 8, wrap=True)
        self.l_lbl.setTextFormat(Qt.TextFormat.RichText); d.addWidget(self.l_lbl)
        self.bt_lbl = _lbl("", U.C.TEXT_DIM, 7, wrap=True); d.addWidget(self.bt_lbl)
        self.news_lbl = _lbl("", U.C.TEXT_DIM, 7, wrap=True)
        self.news_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.news_lbl.setOpenExternalLinks(True)
        d.addWidget(self.news_lbl)
        d.addWidget(_lbl("⚠ " + engine.DISCLAIMER, U.C.ACC2, 7, wrap=True))
        self.detail.hide()
        lay.addWidget(self.detail)

        self._quotes_ready.connect(self._on_quotes)
        self._detail_ready.connect(self._on_detail)
        self._engine_evt.connect(self._on_engine_event)
        self._added.connect(self._on_added)
        engine.on_event(lambda ev, data: self._engine_evt.emit(ev, data))

        self._build_rows()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)
        QTimer.singleShot(1500, self.refresh)

    # ── watchlist ────────────────────────────────────────────────────────────
    def _build_rows(self):
        wl = self.M.watchlist()
        for s in list(self.rows):
            if s not in wl:
                r = self.rows.pop(s); r.setParent(None); r.deleteLater()
        for i, s in enumerate(wl):
            if s not in self.rows:
                r = Row(s)
                r.clicked.connect(lambda sym: self._select(None if sym == self.sel else sym))
                r.remove.connect(self._remove)
                self.rows[s] = r
            self.list_box.insertWidget(i, self.rows[s])
        if self.sel and self.sel not in self.rows:
            self._select(None)

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        syms = list(self.rows)

        def work():
            try:
                qs = self.M.get_quotes(syms)
            except Exception:
                qs = {}
            self._quotes_ready.emit(qs)

        threading.Thread(target=work, daemon=True, name="markets-refresh").start()

    def _on_quotes(self, qs):
        self._busy = False
        for s, r in self.rows.items():
            r.update_quote(qs.get(s), self.M)
        import time as _t
        self.status.setText(("updated " + _t.strftime("%H:%M:%S")) if qs else
                            "offline — couldn't reach market data")

    def _add(self):
        text = self.inp.text().strip()
        if not text:
            return
        self.inp.clear()
        self.status.setText(f"looking up {text}…")

        def work():
            try:
                s = self.M.resolve(text) or ""
            except Exception:
                s = ""
            self._added.emit(text, s)

        threading.Thread(target=work, daemon=True).start()

    def _on_added(self, text, sym):
        if not sym:
            self.status.setText(f"couldn't find “{text}”")
            return
        self.M.add_to_watchlist(sym)          # emits "watchlist" → rows rebuilt
        self._select(sym)

    def _remove(self, sym):
        self.M.remove_from_watchlist(sym)

    def _on_engine_event(self, ev, data):
        if ev == "watchlist":
            self._build_rows(); self.refresh()
        elif ev == "show" and data.get("view") == "detail" and data.get("symbol"):
            sym = data["symbol"].upper()
            self._select(sym)

    # ── detail ───────────────────────────────────────────────────────────────
    def _select(self, sym):
        if self.sel in self.rows:
            self.rows[self.sel].select(False)
        self.sel = sym
        if not sym:
            self.detail.hide()
            return
        if sym in self.rows:
            self.rows[sym].select(True)
        self.d_name.setText(sym)
        self.d_price.setText("")
        self.chart.set([], None)
        self.s_bar.set(None); self.s_lbl.setText("…")
        for l in (self.s_info, self.w_lbl, self.l_lbl, self.bt_lbl, self.news_lbl):
            l.setText("")
        self.w_lbl.setText("Analysing price history and news…")
        self.detail.show()

        def work():
            out = {}
            try:
                out["q"] = self.M.get_quote(sym)
                name = out["q"].name if out["q"] else sym
                h = self.M.get_history(sym, "1y")
                out["s"] = self.M.sentiment(sym, name, h[1] if h else None)
                out["f"] = self.M.forecast(sym, out["s"])
            except Exception as e:
                out["err"] = str(e)
            self._detail_ready.emit(sym, out)

        threading.Thread(target=work, daemon=True, name="markets-detail").start()

    def _on_detail(self, sym, out):
        if sym != self.sel:
            return
        M = self.M
        q, s, f = out.get("q"), out.get("s"), out.get("f")
        if out.get("err") or not f:
            self.w_lbl.setText("Couldn't analyse: " + (out.get("err") or "no data"))
            return
        cur = f.get("currency", "USD")
        if q:
            self.d_name.setText(f"{q.name}  ·  {q.symbol}")
            col = _chg_color(q.change_pct)
            self.d_price.setText(f"<b>{M.fmt_price(q.price, cur)}</b> "
                                 f"<span style='color:{col}'>{q.change_pct:+.2f}% today</span>")
        if s:
            self.s_bar.set(s["score"])
            sc = s["score"]
            self.s_lbl.setText(s["label"].upper())
            self.s_lbl.setStyleSheet(f"color: {_chg_color(sc if abs(sc) >= 0.12 else 0)};"
                                     " background: transparent;")
            info = f"{s['positive']}▲ {s['negative']}▼ {s['neutral']}• headlines"
            if s.get("mood"):
                info += f" · {s['mood']['name']} {s['mood']['value']} ({s['mood']['label']})"
            self.s_info.setText(info)
            heads = [h for h in s.get("headlines", [])][:4]
            if heads:
                def esc(t):
                    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                items = []
                for h in heads:
                    c = _chg_color(h["score"]) if h["score"] else U.C.TEXT_DIM
                    t = esc(h["title"][:90])
                    link = h.get("link") or ""
                    items.append(f"<span style='color:{c}'>●</span> "
                                 + (f"<a style='color:{U.C.TEXT_MED}; text-decoration:none' "
                                    f"href='{esc(link)}'>{t}</a>" if link.startswith("http") else t))
                self.news_lbl.setText("<br>".join(items))
        if f.get("error"):
            self.w_lbl.setText(f["error"])
            return
        w = f["week"]
        self.chart.set(f.get("history"), w)
        arrow = {"up": "▲", "down": "▼"}.get(w["bias"], "▶")
        self.w_lbl.setText(f"NEXT WEEK {arrow} {w['bias'].upper()} · {int(w['prob_up'] * 100)}% chance higher\n"
                           f"likely {M.fmt_price(w['low_68'], cur)} – {M.fmt_price(w['high_68'], cur)}")
        rows = []
        for x in f["long_term"]:
            if x["horizon"] in ("3 months", "1 year", "5 years"):
                c = _chg_color(x["change_pct"])
                rows.append(f"{x['horizon'].upper():<9} <b>{M.fmt_price(x['median'], cur)}</b> "
                            f"<span style='color:{c}'>{x['change_pct']:+.0f}%</span>"
                            f"<br><span style='color:{U.C.TEXT_DIM}'>&nbsp;&nbsp;range "
                            f"{M.fmt_price(x['low_68'], cur)} – {M.fmt_price(x['high_68'], cur)}</span>")
        self.l_lbl.setText("<br>".join(rows))
        t = f["technicals"]
        bt = w.get("backtest")
        info = f"Trend: {t['trend']} · RSI {t['rsi']}"
        if bt:
            info += (f"\nWeekly call right {int(bt['direction_hit_rate'] * 100)}% over the last year "
                     f"(up weeks: {int(bt['base_rate_up'] * 100)}%)")
        self.bt_lbl.setText(info)
