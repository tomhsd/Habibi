"""
plugins/_finance_ui.py — the white FINANCE card for Habibi's left sidebar and
the finance window it opens (expenses, money sent home, salary, DPS, loans,
settings). Simple white theme on purpose, so money reads clearly.

Nothing here blocks the window: anything that may need the network (an
exchange-rate fetch) runs in a background thread and the result comes back
through a Qt signal.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QDate, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog,
                             QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
                             QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
                             QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
                             QTabWidget, QVBoxLayout, QWidget)


def _sibling(stem: str):
    import importlib.util
    name = f"plugins.{stem}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{stem}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return mod


fm = _sibling("_finance_money")
FinanceError = fm.FinanceError

# ── simple white theme ────────────────────────────────────────────────────────
WHITE, BG, TEXT, MUTED = "#FFFFFF", "#F7F8FA", "#1F2933", "#6B7280"
BORDER, ACCENT, GOOD, WARN = "#E3E6EA", "#1A73E8", "#188038", "#C5221F"

STYLE = f"""
    QWidget {{ background: {WHITE}; color: {TEXT}; font-family: 'Segoe UI', Arial; font-size: 10pt; }}
    QLabel {{ background: transparent; }}
    QLineEdit, QComboBox, QDateEdit, QSpinBox {{
        background: {WHITE}; border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 8px; }}
    QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus {{ border: 1px solid {ACCENT}; }}
    QPushButton {{ background: {WHITE}; border: 1px solid {BORDER}; border-radius: 6px;
                   padding: 5px 12px; color: {TEXT}; }}
    QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
    QPushButton[primary="true"] {{ background: {ACCENT}; color: {WHITE}; border: 1px solid {ACCENT}; }}
    QPushButton[primary="true"]:hover {{ background: #1557B0; color: {WHITE}; }}
    QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; top: -1px; }}
    QTabBar::tab {{ background: {BG}; color: {MUTED}; padding: 7px 14px; border: 1px solid {BORDER};
                    border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px; }}
    QTabBar::tab:selected {{ background: {WHITE}; color: {TEXT}; font-weight: 600; }}
    QTableWidget {{ border: 1px solid {BORDER}; border-radius: 6px; gridline-color: {BORDER};
                    alternate-background-color: {BG}; }}
    QHeaderView::section {{ background: {BG}; color: {MUTED}; border: none;
                            border-bottom: 1px solid {BORDER}; padding: 5px; }}
"""


class _Bridge(QObject):
    changed = pyqtSignal()
    done = pyqtSignal(object, object)       # (callback, result-or-exception)


def _bg(bridge: _Bridge, fn, then) -> None:
    """Run fn() off the Qt thread; then(result_or_exception) runs on the Qt thread."""
    def work():
        try:
            r = fn()
        except Exception as e:          # delivered to then() as the result
            r = e
        bridge.done.emit(then, r)
    threading.Thread(target=work, daemon=True).start()


def _btn(text: str, primary: bool = False, tip: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        b.setProperty("primary", True)
    if tip:
        b.setToolTip(tip)
    return b


def _muted(text: str = "", size: int = 9) -> QLabel:
    l = QLabel(text)
    l.setStyleSheet(f"color: {MUTED}; font-size: {size}pt;")
    l.setWordWrap(True)
    return l


def _err(parent, e) -> None:
    msg = str(e) if isinstance(e, FinanceError) else f"Something went wrong ({type(e).__name__})."
    QMessageBox.warning(parent, "Finance", msg)


def ask_pin(parent, f) -> bool:
    if not f.locked():
        return True
    pin, ok = QInputDialog.getText(parent, "Finance", "Enter your finance PIN:", QLineEdit.EchoMode.Password)
    if ok and f.check_pin(pin):
        return True
    if ok:
        QMessageBox.warning(parent, "Finance", "That PIN is wrong.")
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  Sidebar card
# ══════════════════════════════════════════════════════════════════════════════

class FinanceCard(QFrame):
    """The white FINANCE card in the left sidebar."""

    def __init__(self, finance, parent=None):
        super().__init__(parent)
        self.f = finance
        self.bridge = _Bridge()
        self.bridge.changed.connect(self.refresh)
        self.bridge.done.connect(lambda cb, r: cb(r))
        finance.on_change(self.bridge.changed.emit)       # any thread → Qt thread
        self.setObjectName("FinanceCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(STYLE + f"""
            QFrame#FinanceCard {{ background: {WHITE}; border: 1px solid {BORDER}; border-radius: 10px; }}
            QPushButton {{ padding: 3px 8px; font-size: 9pt; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(5)

        head = QHBoxLayout()
        title = QLabel("FINANCE")
        title.setStyleSheet(f"font-weight: 700; letter-spacing: 1px; color: {TEXT}; font-size: 10pt;")
        head.addWidget(title)
        head.addStretch()
        self.b_hide = _btn("👁", tip="Hide or show amounts")
        self.b_lock = _btn("🔒", tip="Lock (needs a PIN, set one in Settings)")
        self.b_open = _btn("↗", tip="Open the finance window")
        for b in (self.b_hide, self.b_lock, self.b_open):
            b.setFixedSize(28, 24)
            head.addWidget(b)
        lay.addLayout(head)

        self.today_lbl = QLabel()
        self.today_lbl.setStyleSheet("font-size: 11pt; font-weight: 600;")
        self.month_lbl = QLabel()
        self.rows = QLabel()
        self.rows.setWordWrap(True)
        self.rows.setTextFormat(Qt.TextFormat.RichText)
        self.rate_lbl = _muted("", 8)
        for w in (self.today_lbl, self.month_lbl, self.rows, self.rate_lbl):
            lay.addWidget(w)

        btns = QHBoxLayout()
        self.b_exp = _btn("+ Expense", primary=True)
        self.b_rem = _btn("+ Sent home")
        btns.addWidget(self.b_exp)
        btns.addWidget(self.b_rem)
        lay.addLayout(btns)

        self.b_open.clicked.connect(lambda: self._open(""))
        self.b_exp.clicked.connect(lambda: self._open("expenses"))
        self.b_rem.clicked.connect(lambda: self._open("remittances"))
        self.b_hide.clicked.connect(self._toggle_hide)
        self.b_lock.clicked.connect(self._toggle_lock)

        self._rate_timer = QTimer(self)
        self._rate_timer.timeout.connect(self._refresh_rate)
        self._rate_timer.start(6 * 3600 * 1000)
        self._tick = QTimer(self)                        # re-check lock expiry / day change
        self._tick.timeout.connect(self.refresh)
        self._tick.start(60 * 1000)
        self.refresh()
        QTimer.singleShot(1500, self._refresh_rate)

    def _refresh_rate(self):
        f = self.f
        _bg(self.bridge, lambda: f.rates.get(f.second, f.main), lambda r: self.refresh())

    def _open(self, tab: str):
        if ask_pin(self, self.f):
            open_window(self.f, tab)

    def _toggle_hide(self):
        try:
            self.f.update_settings(hide_amounts=not self.f.settings()["hide_amounts"])
        except Exception as e:
            _err(self, e)

    def _toggle_lock(self):
        if not self.f.settings()["pin_set"]:
            QMessageBox.information(self, "Finance", "Set a PIN first: open the finance window → Settings.")
            return
        if self.f.locked():
            ask_pin(self, self.f)
        else:
            self.f.lock()

    def refresh(self):
        f = self.f
        try:
            s = f.settings()
            locked = f.locked()
            self.b_lock.setText("🔒" if locked else "🔓")
            if locked:
                self.today_lbl.setText("Locked")
                self.month_lbl.setText("")
                self.rows.setText(f"<span style='color:{MUTED}'>Tap 🔒 and enter your PIN to see your records.</span>")
                self.rate_lbl.setText("")
                return
            hide = s["hide_amounts"]
            main, second = f.main, f.second

            def pair(t):
                if t["main"] is None:
                    return " + ".join(fm.fmt(v, c, hide) for c, v in t["by_currency"].items()) or fm.fmt(0, main, hide)
                return f"{fm.fmt(t['main'], main, hide)}  ·  {fm.fmt(t['second'], second, hide)}"

            tm = f.today_and_month(allow_fetch=False)
            self.today_lbl.setText(f"Today  {pair(tm['today'])}")
            self.month_lbl.setText(f"This month  {pair(tm['month'])}")

            lines = []
            rm = f.remittance_month()
            if rm["count"]:
                lines.append("Sent home: " + ", ".join(fm.fmt(v, c, hide) for c, v in rm["sent"].items())
                             + f" ({rm['count']})")
            sal = f.salary()
            if sal:
                d = sal["days_left"]
                when = "today" if d == 0 else "tomorrow" if d == 1 else f"in {d} days"
                col = ACCENT if d <= s["alert_days_before"] else TEXT
                lines.append(f"Salary: <span style='color:{col}'>{when}</span> ({sal['next'][8:]}/{sal['next'][5:7]})")
            for d in f.dps_list():
                t = f"{d['name']}: {d['paid_count']}/{d['term_months']}"
                if d["behind"]:
                    t += f" <span style='color:{WARN}'>· {d['behind']} unpaid</span>"
                elif d["next_due"]:
                    t += f" · next {d['next_due'][8:]}/{d['next_due'][5:7]}"
                lines.append(t)
            loans = f.loan_list()
            if loans:
                tot: dict = {}
                for l in loans:
                    tot[l["currency"]] = tot.get(l["currency"], 0) + l["remaining"]
                lines.append("Loans left: " + ", ".join(fm.fmt(v, c, hide) for c, v in tot.items()))
            self.rows.setText("<br>".join(lines) if lines else
                              f"<span style='color:{MUTED}'>Say “spent 50 riyal on food” to start.</span>")
            r = f.rates.get(second, main, allow_fetch=False)
            self.rate_lbl.setText(r.describe() if r else f"No {second}→{main} rate yet (offline?)")
        except Exception as e:
            self.rows.setText(f"<span style='color:{WARN}'>Couldn't read records ({type(e).__name__}).</span>")


# ══════════════════════════════════════════════════════════════════════════════
#  Finance window
# ══════════════════════════════════════════════════════════════════════════════

_window = None


def open_window(finance, tab: str = ""):
    global _window
    if _window is None:
        _window = FinanceWindow(finance)
    _window.show_tab(tab)
    _window.show()
    _window.raise_()
    _window.activateWindow()
    return _window


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setAlternatingRowColors(True)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return t


def _fill(t: QTableWidget, rows: list[list]) -> None:
    t.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, v in enumerate(row):
            it = QTableWidgetItem(str(v))
            if j and isinstance(v, str) and any(ch.isdigit() for ch in v) and not v[:4].isdigit():
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            t.setItem(i, j, it)


def _date_edit() -> QDateEdit:
    d = QDateEdit(QDate.currentDate())
    d.setCalendarPopup(True)
    d.setDisplayFormat("yyyy-MM-dd")
    return d


def _cur_combo(f, default: str) -> QComboBox:
    c = QComboBox()
    codes = [f.second, f.main] + [k for k in fm.CURRENCIES if k not in (f.second, f.main)]
    c.addItems(codes)
    c.setCurrentText(default)
    return c


class FinanceWindow(QDialog):
    TABS = ["expenses", "remittances", "salary", "dps", "loans", "settings"]

    def __init__(self, finance, parent=None):
        super().__init__(parent)
        self.f = finance
        self.setWindowTitle("Habibi · Finance")
        self.setStyleSheet(STYLE)
        self.resize(860, 600)
        self.bridge = _Bridge()
        self.bridge.changed.connect(self.refresh)
        self.bridge.done.connect(lambda cb, r: cb(r))
        finance.on_change(self.bridge.changed.emit)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 12)
        self.header = QLabel()
        self.header.setStyleSheet("font-size: 13pt; font-weight: 600;")
        root.addWidget(self.header)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        root.addWidget(_muted("All figures are your own records and estimates. Habibi gives no financial "
                              "advice. Your records stay on this PC, encrypted."
                              if self.f.store.encrypted else
                              "All figures are your own records and estimates. Habibi gives no financial "
                              "advice. Your records stay on this PC.", 8))
        self._build_expenses()
        self._build_remittances()
        self._build_salary()
        self._build_dps()
        self._build_loans()
        self._build_settings()
        self.refresh()

    def show_tab(self, name: str) -> None:
        if name in self.TABS:
            self.tabs.setCurrentIndex(self.TABS.index(name))

    def _h(self) -> bool:
        return self.f.settings()["hide_amounts"]

    def _run(self, fn, ok_msg_fn=None, slow: bool = False):
        """Do a write; show a plain message on error. slow=True → off the Qt thread."""
        def after(r):
            if isinstance(r, Exception):
                _err(self, r)
            elif ok_msg_fn:
                m = ok_msg_fn(r)
                if m:
                    QMessageBox.information(self, "Finance", m)
        if slow:
            _bg(self.bridge, fn, after)
        else:
            try:
                after(fn())
            except Exception as e:
                after(e)

    # ── expenses ─────────────────────────────────────────────────────────
    def _build_expenses(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QHBoxLayout()
        self.e_amt = QLineEdit(); self.e_amt.setPlaceholderText("Amount")
        self.e_cur = _cur_combo(self.f, self.f.second)
        self.e_cat = QComboBox(); self.e_cat.addItems(fm.EXPENSE_CATEGORIES); self.e_cat.setEditable(True)
        self.e_note = QLineEdit(); self.e_note.setPlaceholderText("Note (optional)")
        self.e_date = _date_edit()
        add = _btn("Add", primary=True)
        for x in (self.e_amt, self.e_cur, self.e_cat, self.e_note, self.e_date, add):
            form.addWidget(x)
        lay.addLayout(form)
        self.e_sum = QLabel()
        lay.addWidget(self.e_sum)
        self.e_tab = _table(["Date", "Category", "Amount", "Note"])
        lay.addWidget(self.e_tab, 1)
        row = QHBoxLayout()
        row.addStretch()
        rm = _btn("Delete selected")
        row.addWidget(rm)
        lay.addLayout(row)

        def do_add():
            self._run(lambda: self.f.add_expense(self.e_amt.text(), self.e_cur.currentText(),
                                                 self.e_cat.currentText(), self.e_note.text(),
                                                 self.e_date.date().toString("yyyy-MM-dd")))
            self.e_amt.clear(); self.e_note.clear(); self.e_amt.setFocus()

        def do_rm():
            r = self.e_tab.currentRow()
            if 0 <= r < len(self._exp_ids):
                self._run(lambda: self.f.delete_expense(self._exp_ids[r]))
        add.clicked.connect(do_add)
        self.e_amt.returnPressed.connect(do_add)
        rm.clicked.connect(do_rm)
        self._exp_ids: list[int] = []
        self.tabs.addTab(w, "Expenses")

    # ── remittances ──────────────────────────────────────────────────────
    def _build_remittances(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        g = QGridLayout()
        self.r_amt = QLineEdit(); self.r_amt.setPlaceholderText("Amount sent")
        self.r_fee = QLineEdit(); self.r_fee.setPlaceholderText("Fee")
        self.r_rate = QLineEdit(); self.r_rate.setPlaceholderText("Rate (blank = today's)")
        self.r_recv = QLineEdit(); self.r_recv.setPlaceholderText("Received at home (optional)")
        self.r_to = QLineEdit(); self.r_to.setPlaceholderText("Recipient (optional)")
        self.r_how = QComboBox(); self.r_how.setEditable(True)
        self.r_how.addItems(["", "Bank", "bKash", "Nagad", "Exchange house", "Rocket", "Other"])
        self.r_date = _date_edit()
        add = _btn("Save transfer", primary=True)
        for i, x in enumerate((self.r_amt, self.r_fee, self.r_rate, self.r_recv,
                               self.r_to, self.r_how, self.r_date, add)):
            g.addWidget(x, i // 4, i % 4)
        lay.addLayout(g)
        self.r_sum = QLabel()
        lay.addWidget(self.r_sum)
        self.r_tab = _table(["Date", "Sent", "Fee", "Rate", "Received", "Recipient", "Method"])
        lay.addWidget(self.r_tab, 1)

        def do_add():
            self._run(lambda: self.f.add_remittance(self.r_amt.text(), self.r_rate.text() or None,
                                                    self.r_fee.text() or None, self.r_recv.text() or None,
                                                    self.r_to.text(), self.r_how.currentText(), "",
                                                    self.r_date.date().toString("yyyy-MM-dd")),
                      lambda r: (f"Received {fm.fmt(r['received'], r['dst'])}. {r['rate_note']}"
                                 if r["rate_note"] else None), slow=True)
            for x in (self.r_amt, self.r_fee, self.r_rate, self.r_recv):
                x.clear()
        add.clicked.connect(do_add)
        self.tabs.addTab(w, "Sent home")

    # ── salary ───────────────────────────────────────────────────────────
    def _build_salary(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.s_amt = QLineEdit()
        self.s_cur = _cur_combo(self.f, self.f.second)
        self.s_day = QSpinBox(); self.s_day.setRange(1, 31); self.s_day.setValue(28)
        form.addRow("Monthly salary", self.s_amt)
        form.addRow("Currency", self.s_cur)
        form.addRow("Pay day (day of month)", self.s_day)
        lay.addLayout(form)
        row = QHBoxLayout()
        save = _btn("Save salary", primary=True)
        got = _btn("Salary received today")
        row.addWidget(save); row.addWidget(got); row.addStretch()
        lay.addLayout(row)
        self.s_info = QLabel(); self.s_info.setWordWrap(True)
        lay.addWidget(self.s_info)
        self.s_split = _muted("", 9)
        lay.addWidget(self.s_split)
        self.s_tab = _table(["Paid on", "Amount"])
        lay.addWidget(self.s_tab, 1)
        save.clicked.connect(lambda: self._run(lambda: self.f.set_salary(self.s_amt.text(), self.s_day.value(),
                                                                         self.s_cur.currentText())))

        def split_msg(r):
            sp = r["split"]
            c = sp["currency"]
            t = (f"From your records: DPS and loan instalments {fm.fmt(sp['commitments'], c)}, "
                 f"average monthly spending {fm.fmt(sp['avg_spend'], c)}, "
                 f"left {fm.fmt(sp['left'], c)}")
            if sp["left_in_main"] is not None and sp["left"] > 0 and c != self.f.main:
                t += f" (about {fm.fmt(sp['left_in_main'], self.f.main)} if sent home)"
            self.s_split.setText(t + ". A calculation from your entries, not advice.")
            return None
        got.clicked.connect(lambda: self._run(lambda: self.f.log_salary(), split_msg, slow=True))
        self.tabs.addTab(w, "Salary")

    # ── DPS ──────────────────────────────────────────────────────────────
    def _build_dps(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        g = QGridLayout()
        self.d_name = QLineEdit("DPS"); self.d_name.setPlaceholderText("Name / bank")
        self.d_amt = QLineEdit(); self.d_amt.setPlaceholderText("Monthly instalment")
        self.d_cur = _cur_combo(self.f, self.f.main)
        self.d_term = QSpinBox(); self.d_term.setRange(1, 600); self.d_term.setValue(60); self.d_term.setSuffix(" months")
        self.d_start = _date_edit()
        self.d_day = QSpinBox(); self.d_day.setRange(1, 31); self.d_day.setPrefix("due day ")
        self.d_day.setValue(QDate.currentDate().day())
        self.d_mat = QLineEdit(); self.d_mat.setPlaceholderText("Maturity amount (optional)")
        add = _btn("Add DPS", primary=True)
        for i, x in enumerate((self.d_name, self.d_amt, self.d_cur, self.d_term,
                               self.d_start, self.d_day, self.d_mat, add)):
            g.addWidget(x, i // 4, i % 4)
        lay.addLayout(g)
        self.d_tab = _table(["Name", "Monthly", "Paid", "Remaining", "Next due", "Matures", "Maturity"])
        lay.addWidget(self.d_tab, 1)
        row = QHBoxLayout(); row.addStretch()
        pay = _btn("Mark instalment paid", primary=True)
        close = _btn("Close DPS")
        row.addWidget(pay); row.addWidget(close)
        lay.addLayout(row)
        add.clicked.connect(lambda: self._run(lambda: self.f.add_dps(
            self.d_amt.text(), self.d_term.value(), self.d_start.date().toString("yyyy-MM-dd"),
            self.d_day.value(), self.d_mat.text() or None, self.d_name.text(), self.d_cur.currentText())))

        def sel():
            r = self.d_tab.currentRow()
            return self._dps_ids[r] if 0 <= r < len(self._dps_ids) else (self._dps_ids[0] if len(self._dps_ids) == 1 else None)

        def do_pay():
            i = sel()
            if i is None:
                return QMessageBox.information(self, "Finance", "Select a DPS first.")
            self._run(lambda: self.f.pay_dps(str(i)))

        def do_close():
            i = sel()
            if i is not None and QMessageBox.question(self, "Finance", "Close this DPS? It will be hidden "
                                                      "from the list (records are kept).") == QMessageBox.StandardButton.Yes:
                self._run(lambda: self.f.close_dps(str(i)))
        pay.clicked.connect(do_pay)
        close.clicked.connect(do_close)
        self._dps_ids: list[int] = []
        self.tabs.addTab(w, "DPS")

    # ── loans ────────────────────────────────────────────────────────────
    def _build_loans(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        g = QGridLayout()
        self.l_who = QLineEdit(); self.l_who.setPlaceholderText("Lender (optional)")
        self.l_amt = QLineEdit(); self.l_amt.setPlaceholderText("Loan amount")
        self.l_cur = _cur_combo(self.f, self.f.main)
        self.l_mon = QLineEdit(); self.l_mon.setPlaceholderText("Monthly payment")
        self.l_day = QSpinBox(); self.l_day.setRange(0, 31); self.l_day.setPrefix("due day ")
        self.l_day.setSpecialValueText("no due day")
        self.l_int = QLineEdit(); self.l_int.setPlaceholderText("Interest % / year (optional)")
        self.l_start = _date_edit()
        add = _btn("Add loan", primary=True)
        for i, x in enumerate((self.l_who, self.l_amt, self.l_cur, self.l_mon,
                               self.l_day, self.l_int, self.l_start, add)):
            g.addWidget(x, i // 4, i % 4)
        lay.addLayout(g)
        self.l_tab = _table(["Lender", "Amount", "Paid", "Left", "Monthly", "Next due", "Months left"])
        lay.addWidget(self.l_tab, 1)
        row = QHBoxLayout(); row.addStretch()
        self.l_pay_amt = QLineEdit(); self.l_pay_amt.setPlaceholderText("Amount (blank = monthly)")
        pay = _btn("Record payment", primary=True)
        row.addWidget(self.l_pay_amt); row.addWidget(pay)
        lay.addLayout(row)
        lay.addWidget(_muted("Left = amount − payments recorded. Interest isn't added automatically; "
                             "record payments as you make them.", 8))
        add.clicked.connect(lambda: self._run(lambda: self.f.add_loan(
            self.l_amt.text(), self.l_who.text(), self.l_mon.text() or None, self.l_day.value() or None,
            self.l_int.text() or None, self.l_start.date().toString("yyyy-MM-dd"), self.l_cur.currentText())))

        def do_pay():
            r = self.l_tab.currentRow()
            ids = self._loan_ids
            i = ids[r] if 0 <= r < len(ids) else (ids[0] if len(ids) == 1 else None)
            if i is None:
                return QMessageBox.information(self, "Finance", "Select a loan first.")
            self._run(lambda: self.f.pay_loan(str(i), self.l_pay_amt.text() or None))
            self.l_pay_amt.clear()
        pay.clicked.connect(do_pay)
        self._loan_ids: list[int] = []
        self.tabs.addTab(w, "Loans")

    # ── settings ─────────────────────────────────────────────────────────
    def _build_settings(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.t_home = QComboBox(); self.t_home.addItems(sorted(fm.COUNTRIES))
        self.t_work = QComboBox(); self.t_work.addItems(sorted(fm.COUNTRIES))
        self.t_main = _cur_combo(self.f, self.f.main)
        self.t_second = _cur_combo(self.f, self.f.second)
        self.t_days = QSpinBox(); self.t_days.setRange(0, 14); self.t_days.setSuffix(" days before")
        self.t_hide = QCheckBox("Hide amounts on screen")
        self.t_rate = QLineEdit(); self.t_rate.setPlaceholderText("blank = online rate")
        form.addRow("Home country", self.t_home)
        form.addRow("Working country", self.t_work)
        form.addRow("Main currency", self.t_main)
        form.addRow("Second currency", self.t_second)
        form.addRow("Reminders", self.t_days)
        form.addRow("", self.t_hide)
        self.t_rate_lbl = QLabel()
        form.addRow("Manual rate", self.t_rate)
        form.addRow("", self.t_rate_lbl)
        lay.addLayout(form)
        self.t_home.currentTextChanged.connect(lambda c: self.t_main.setCurrentText(fm.COUNTRIES[c]))
        self.t_work.currentTextChanged.connect(lambda c: self.t_second.setCurrentText(fm.COUNTRIES[c]))

        row = QHBoxLayout()
        save = _btn("Save settings", primary=True)
        pin = _btn("Set / change PIN")
        exp = _btn("Export (encrypted)…")
        imp = _btn("Import…")
        for b in (save, pin, exp, imp):
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        self.t_info = _muted("", 8)
        lay.addWidget(self.t_info)
        lay.addStretch()

        def do_save():
            def go():
                self.f.update_settings(home_country=self.t_home.currentText(),
                                       work_country=self.t_work.currentText(),
                                       main_currency=self.t_main.currentText(),
                                       second_currency=self.t_second.currentText(),
                                       alert_days_before=self.t_days.value(),
                                       hide_amounts=self.t_hide.isChecked())
                rate = self.t_rate.text().strip()
                if rate:
                    self.f.rates.set_manual(self.f.second, self.f.main, rate)
                else:
                    self.f.rates.clear_manual(self.f.second, self.f.main)
                self.f.changed()
            self._run(go, lambda r: "Settings saved.")

        def do_pin():
            old = ""
            if self.f.settings()["pin_set"]:
                old, ok = QInputDialog.getText(self, "Finance", "Current PIN:", QLineEdit.EchoMode.Password)
                if not ok:
                    return
            new, ok = QInputDialog.getText(self, "Finance", "New PIN, 4 to 8 digits (blank = no PIN):",
                                           QLineEdit.EchoMode.Password)
            if ok:
                self._run(lambda: self.f.set_pin(new, old), lambda r: "PIN saved." if new else "PIN removed.")

        def do_export():
            path, _ = QFileDialog.getSaveFileName(self, "Export finance records", "habibi-finance.hfin",
                                                  "Habibi finance export (*.hfin)")
            if not path:
                return
            pw, ok = QInputDialog.getText(self, "Finance", "Passphrase for this export (needed to import it):",
                                          QLineEdit.EchoMode.Password)
            if ok:
                self._run(lambda: self.f.store.export_to(Path(path), pw), lambda r: f"Exported to {r}.")

        def do_import():
            path, _ = QFileDialog.getOpenFileName(self, "Import finance records", "",
                                                  "Habibi finance export (*.hfin)")
            if not path:
                return
            if QMessageBox.question(self, "Finance", "Replace the records on this PC with the export? "
                                    "A backup of the current records is kept.") != QMessageBox.StandardButton.Yes:
                return
            pw, ok = QInputDialog.getText(self, "Finance", "Export passphrase:", QLineEdit.EchoMode.Password)
            if ok:
                def go():
                    self.f.store.import_from(Path(path), pw)
                    self.f.changed()
                self._run(go, lambda r: "Records imported.")
        save.clicked.connect(do_save)
        pin.clicked.connect(do_pin)
        exp.clicked.connect(do_export)
        imp.clicked.connect(do_import)
        self.tabs.addTab(w, "Settings")

    # ── refresh ──────────────────────────────────────────────────────────
    def refresh(self):
        f = self.f
        try:
            if f.locked():
                self.header.setText("Finance · locked")
                for t in (self.e_tab, self.r_tab, self.s_tab, self.d_tab, self.l_tab):
                    t.setRowCount(0)
                return
            h = self._h()
            s = f.settings()
            tm = f.today_and_month(allow_fetch=False)

            def pair(t):
                if t["main"] is None:
                    return " + ".join(fm.fmt(v, c, h) for c, v in t["by_currency"].items()) or "—"
                return f"{fm.fmt(t['main'], f.main, h)} · {fm.fmt(t['second'], f.second, h)}"
            self.header.setText(f"Today {pair(tm['today'])}     This month {pair(tm['month'])}")

            ms, me = fm.month_bounds(f.clock())
            rows = f.expenses("0000-01-01", "9999-12-31")[:300]
            self._exp_ids = [r["id"] for r in rows]
            _fill(self.e_tab, [[r["spent_on"], r["category"], fm.fmt(r["amount_minor"], r["currency"], h), r["note"]]
                               for r in rows])
            cats = sorted(tm["month"]["by_category_main"].items(), key=lambda kv: -kv[1])
            self.e_sum.setText("This month by category: " + (", ".join(
                f"{k} {fm.fmt(v, f.main, h)}" for k, v in cats) if cats else "nothing yet"))

            rem = f.remittances("0000-01-01", "9999-12-31")[:300]
            _fill(self.r_tab, [[r["sent_on"], fm.fmt(r["amount_sar_minor"], r["sent_currency"], h),
                                fm.fmt(r["fee_sar_minor"], r["sent_currency"], h),
                                f"{fm.micro_to_rate(r['rate_micro']).normalize():f}",
                                fm.fmt(r["received_bdt_minor"], r["received_currency"], h),
                                r["recipient"], r["method"]] for r in rem])
            m = f.remittance_month()
            self.r_sum.setText("This month: " + (", ".join(fm.fmt(v, c, h) for c, v in m["sent"].items())
                                                + f" sent in {m['count']} transfers" if m["count"] else "nothing sent yet"))

            sal = f.salary()
            if sal:
                self.s_info.setText(f"Salary {fm.fmt(sal['amount_minor'], sal['currency'], h)} on day "
                                    f"{sal['pay_day']}. Next: {sal['next']} ({sal['days_left']} days).")
                if not self.s_amt.text():
                    self.s_amt.setText(str(fm.from_minor(sal["amount_minor"], sal["currency"]).normalize()))
                    self.s_cur.setCurrentText(sal["currency"])
                    self.s_day.setValue(sal["pay_day"])
            else:
                self.s_info.setText("No salary saved yet.")
            log = f.store.q("SELECT * FROM salary_log ORDER BY paid_on DESC LIMIT 60")
            _fill(self.s_tab, [[r["paid_on"], fm.fmt(r["amount_minor"], r["currency"], h)] for r in log])

            dl = f.dps_list()
            self._dps_ids = [d["id"] for d in dl]
            _fill(self.d_tab, [[d["name"], fm.fmt(d["monthly_minor"], d["currency"], h),
                                f"{d['paid_count']}/{d['term_months']} · {fm.fmt(d['paid_minor'], d['currency'], h)}",
                                fm.fmt(d["remaining_minor"], d["currency"], h),
                                (d["next_due"] or "—") + (f"  ({d['behind']} unpaid)" if d["behind"] else ""),
                                d["maturity_on"], fm.fmt(d["maturity_minor"], d["currency"], h) if d["maturity_minor"] else "—"]
                               for d in dl])
            ll = f.loan_list()
            self._loan_ids = [l["id"] for l in ll]
            _fill(self.l_tab, [[l["lender"] or f"#{l['id']}", fm.fmt(l["principal"], l["currency"], h),
                                fm.fmt(l["paid"], l["currency"], h), fm.fmt(l["remaining"], l["currency"], h),
                                fm.fmt(l["monthly_minor"], l["currency"], h) if l["monthly_minor"] else "—",
                                l["next_due"] or "—", l["months_left"] if l["months_left"] is not None else "—"]
                               for l in ll])

            for combo, val in ((self.t_home, s["home_country"]), (self.t_work, s["work_country"])):
                combo.blockSignals(True); combo.setCurrentText(val); combo.blockSignals(False)
            self.t_main.setCurrentText(s["main_currency"])
            self.t_second.setCurrentText(s["second_currency"])
            self.t_days.setValue(s["alert_days_before"])
            self.t_hide.setChecked(s["hide_amounts"])
            r = f.rates.get(f.second, f.main, allow_fetch=False)
            self.t_rate_lbl.setText(r.describe() if r else "No rate yet.")
            if r and r.manual and not self.t_rate.text():
                self.t_rate.setText(f"{r.value.normalize():f}")
            where = f.store.path
            self.t_info.setText(f"Records file: {where}" + ("  (encrypted with Windows)" if f.store.encrypted else "")
                                + f"\nDaily backups (last 7): {f.store.backup_dir}"
                                + (f"\n⚠ {f.store.load_error}" if f.store.load_error else ""))
        except Exception as e:
            self.header.setText(f"Couldn't read records ({type(e).__name__}).")
