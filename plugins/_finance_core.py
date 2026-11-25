"""
plugins/_finance_core.py — the finance records: expenses, remittances, salary,
DPS, loans, settings, PIN and reminders. No UI and no voice wording here, so
everything can be tested on its own (packaging/finance_selftest.py).

All figures are the user's own records. Nothing here gives financial advice.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional


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
fdb = _sibling("_finance_db")
frates = _sibling("_finance_rates")
FinanceError = fm.FinanceError

UNLOCK_MINUTES = 10


class Finance:
    def __init__(self, store=None, fetcher=None, clock: Callable[[], date] = fm.today):
        self.store = store or fdb.FinanceStore()
        self.rates = frates.Rates(self.store, fetcher or frates.fetch_online)
        self.clock = clock
        self._unlocked_until = 0.0
        self._listeners: list[Callable[[], None]] = []

    # -- change notifications (the sidebar card refreshes on these) -----------
    def on_change(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def changed(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════════════════
    #  Settings
    # ══════════════════════════════════════════════════════════════════════
    def settings(self) -> dict:
        s = self.store
        return {
            "home_country": s.setting("home_country"),
            "work_country": s.setting("work_country"),
            "main_currency": s.setting("main_currency"),
            "second_currency": s.setting("second_currency"),
            "alert_days_before": int(s.setting("alert_days_before") or 3),
            "hide_amounts": s.setting("hide_amounts") == "1",
            "pin_set": bool(s.setting("pin_hash")),
        }

    @property
    def main(self) -> str:
        return self.store.setting("main_currency")

    @property
    def second(self) -> str:
        return self.store.setting("second_currency")

    def update_settings(self, home_country=None, work_country=None, main_currency=None,
                        second_currency=None, alert_days_before=None, hide_amounts=None) -> dict:
        vals: dict = {}
        if home_country:
            vals["home_country"] = fm.norm_country(home_country)
            if not main_currency:
                vals["main_currency"] = fm.country_currency(vals["home_country"])
        if work_country:
            vals["work_country"] = fm.norm_country(work_country)
            if not second_currency:
                vals["second_currency"] = fm.country_currency(vals["work_country"])
        if main_currency:
            vals["main_currency"] = fm.norm_currency(main_currency)
        if second_currency:
            vals["second_currency"] = fm.norm_currency(second_currency)
        if alert_days_before not in (None, ""):
            try:
                n = int(alert_days_before)
            except (TypeError, ValueError):
                raise FinanceError("Reminder days must be a whole number.")
            if not 0 <= n <= 14:
                raise FinanceError("Reminders can start 0 to 14 days before.")
            vals["alert_days_before"] = n
        if hide_amounts is not None:
            vals["hide_amounts"] = "1" if hide_amounts else "0"
        if vals:
            self.store.set_settings(**vals)
            self.changed()
        return self.settings()

    # -- PIN ------------------------------------------------------------------
    @staticmethod
    def _hash_pin(pin: str, salt: bytes) -> str:
        return salt.hex() + "$" + hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 200_000).hex()

    def set_pin(self, new_pin: str, old_pin: str = "") -> None:
        if self.store.setting("pin_hash") and not self.check_pin(old_pin):
            raise FinanceError("The current PIN is wrong.")
        new_pin = str(new_pin or "").strip()
        if new_pin and not (new_pin.isdigit() and 4 <= len(new_pin) <= 8):
            raise FinanceError("The PIN must be 4 to 8 digits.")
        self.store.set_settings(pin_hash=self._hash_pin(new_pin, os.urandom(16)) if new_pin else "")
        self._unlocked_until = time.time() + UNLOCK_MINUTES * 60 if new_pin else 0
        self.changed()

    def check_pin(self, pin) -> bool:
        h = self.store.setting("pin_hash")
        if not h:
            return True
        try:
            salt_hex, _ = h.split("$", 1)
            ok = hmac.compare_digest(self._hash_pin(str(pin or "").strip(), bytes.fromhex(salt_hex)), h)
        except ValueError:
            return False
        if ok:
            self._unlocked_until = time.time() + UNLOCK_MINUTES * 60
            self.changed()
        return ok

    def locked(self) -> bool:
        return bool(self.store.setting("pin_hash")) and time.time() > self._unlocked_until

    def lock(self) -> None:
        self._unlocked_until = 0
        self.changed()

    # ══════════════════════════════════════════════════════════════════════
    #  Conversion helpers
    # ══════════════════════════════════════════════════════════════════════
    def both(self, minor: int, currency: str, allow_fetch: bool = True) -> dict:
        """An amount in its own currency plus the main and second currencies."""
        out = {currency: minor}
        stale = None
        for cur in (self.main, self.second):
            if cur in out:
                continue
            v, r = self.rates.convert(minor, currency, cur, allow_fetch)
            if v is not None:
                out[cur] = v
                if r.stale and stale is None:
                    stale = r
        return {"amounts": out, "stale_rate": stale}

    def _sum_in(self, rows: list, currency_field: str, amount_field: str, target: str,
                allow_fetch: bool = True) -> Optional[int]:
        total = 0
        for r in rows:
            v, _ = self.rates.convert(r[amount_field], r[currency_field], target, allow_fetch)
            if v is None:
                return None
            total += v
        return total

    # ══════════════════════════════════════════════════════════════════════
    #  Expenses
    # ══════════════════════════════════════════════════════════════════════
    def add_expense(self, amount, currency=None, category=None, note: str = "", spent_on=None) -> dict:
        if currency in (None, ""):
            raise FinanceError(f"Which currency was that: {self.second} or {self.main}?")
        cur = fm.norm_currency(currency)
        minor = fm.to_minor(amount, cur)
        cat = fm.norm_category(category)
        if not cat:
            raise FinanceError("What was it for? For example food, transport, rent or phone.")
        d = fm.parse_date(spent_on, self.clock())
        if d > self.clock() + timedelta(days=1):
            raise FinanceError("That date is in the future.")
        with self.store.tx() as c:
            cur_id = c.execute("INSERT INTO expenses(amount_minor, currency, category, note, spent_on, created_at) "
                               "VALUES (?, ?, ?, ?, ?, ?)",
                               (minor, cur, cat, str(note or "")[:200], d.isoformat(), fm.utc_now())).lastrowid
        self.changed()
        return {"id": cur_id, "amount_minor": minor, "currency": cur, "category": cat, "spent_on": d.isoformat()}

    def delete_expense(self, expense_id: int) -> bool:
        with self.store.tx() as c:
            n = c.execute("DELETE FROM expenses WHERE id=?", (int(expense_id),)).rowcount
        self.changed()
        return n > 0

    def last_expense(self):
        return self.store.one("SELECT * FROM expenses ORDER BY id DESC LIMIT 1")

    def expenses(self, start: str, end: str) -> list:
        return self.store.q("SELECT * FROM expenses WHERE spent_on BETWEEN ? AND ? "
                            "ORDER BY spent_on DESC, id DESC", (start, end))

    def expense_totals(self, start: str, end: str, allow_fetch: bool = True) -> dict:
        rows = self.expenses(start, end)
        by_cur: dict[str, int] = {}
        by_cat: dict[str, int] = {}
        for r in rows:
            by_cur[r["currency"]] = by_cur.get(r["currency"], 0) + r["amount_minor"]
        main_total = self._sum_in(rows, "currency", "amount_minor", self.main, allow_fetch)
        second_total = self._sum_in(rows, "currency", "amount_minor", self.second, allow_fetch)
        for r in rows:
            v, _ = self.rates.convert(r["amount_minor"], r["currency"], self.main, allow_fetch)
            if v is not None:
                by_cat[r["category"]] = by_cat.get(r["category"], 0) + v
        return {"count": len(rows), "by_currency": by_cur, "by_category_main": by_cat,
                "main": main_total, "second": second_total}

    def today_and_month(self, allow_fetch: bool = True) -> dict:
        t = self.clock()
        ms, me = fm.month_bounds(t)
        return {"today": self.expense_totals(t.isoformat(), t.isoformat(), allow_fetch),
                "month": self.expense_totals(ms, me, allow_fetch)}

    # ══════════════════════════════════════════════════════════════════════
    #  Remittances
    # ══════════════════════════════════════════════════════════════════════
    def add_remittance(self, amount, rate=None, fee=None, received=None, recipient: str = "",
                       method: str = "", note: str = "", sent_on=None, currency=None) -> dict:
        src = fm.norm_currency(currency) if currency else self.second
        dst = self.main
        amt = fm.to_minor(amount, src)
        fee_minor = fm.to_minor_or_zero(fee, src)
        if fee_minor >= amt:
            raise FinanceError("The fee can't be as much as the amount sent.")
        d = fm.parse_date(sent_on, self.clock())
        if received not in (None, ""):
            recv = fm.to_minor(received, dst)
            rate_micro = fm.rate_to_micro(rate) if rate not in (None, "") else \
                max(1, round(recv * fm.RATE_SCALE * 10 ** fm.digits(src) / (amt * 10 ** fm.digits(dst))))
            rate_note = ""
        else:
            if rate not in (None, ""):
                rate_micro = fm.rate_to_micro(rate)
                rate_note = ""
            else:
                r = self.rates.get(src, dst)
                if r is None:
                    raise FinanceError(f"I don't have a {src} to {dst} rate. Tell me the rate you got, "
                                       f"or how much {dst} was received.")
                rate_micro = r.micro
                rate_note = r.describe()
            recv = fm.convert(amt, src, dst, rate_micro)
        with self.store.tx() as c:
            rid = c.execute(
                "INSERT INTO remittances(sent_on, amount_sar_minor, rate_micro, fee_sar_minor, received_bdt_minor, "
                "recipient, method, note, sent_currency, received_currency) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (d.isoformat(), amt, rate_micro, fee_minor, recv, str(recipient or "")[:80],
                 str(method or "")[:40], str(note or "")[:200], src, dst)).lastrowid
        self.changed()
        return {"id": rid, "sent": amt, "src": src, "dst": dst, "fee": fee_minor, "received": recv,
                "rate_micro": rate_micro, "rate_note": rate_note, "sent_on": d.isoformat()}

    def remittances(self, start: str, end: str) -> list:
        return self.store.q("SELECT * FROM remittances WHERE sent_on BETWEEN ? AND ? "
                            "ORDER BY sent_on DESC, id DESC", (start, end))

    def remittance_month(self, d: Optional[date] = None) -> dict:
        ms, me = fm.month_bounds(d or self.clock())
        rows = self.remittances(ms, me)
        sent: dict[str, int] = {}
        fees: dict[str, int] = {}
        recv: dict[str, int] = {}
        for r in rows:
            sent[r["sent_currency"]] = sent.get(r["sent_currency"], 0) + r["amount_sar_minor"]
            fees[r["sent_currency"]] = fees.get(r["sent_currency"], 0) + r["fee_sar_minor"]
            recv[r["received_currency"]] = recv.get(r["received_currency"], 0) + r["received_bdt_minor"]
        return {"count": len(rows), "sent": sent, "fees": fees, "received": recv}

    # ══════════════════════════════════════════════════════════════════════
    #  Salary
    # ══════════════════════════════════════════════════════════════════════
    def set_salary(self, amount, pay_day, currency=None) -> dict:
        cur = fm.norm_currency(currency) if currency else self.second
        minor = fm.to_minor(amount, cur)
        day = fm.check_day(pay_day)
        with self.store.tx() as c:
            c.execute("UPDATE salary SET active=0 WHERE active=1")
            c.execute("INSERT INTO salary(amount_minor, currency, pay_day, active) VALUES (?, ?, ?, 1)",
                      (minor, cur, day))
        self.changed()
        return self.salary()

    def salary(self) -> Optional[dict]:
        r = self.store.one("SELECT * FROM salary WHERE active=1 ORDER BY id DESC LIMIT 1")
        if not r:
            return None
        t = self.clock()
        nxt = fm.next_due(r["pay_day"], t)
        if self.salary_logged_this_month() and nxt.month == t.month and nxt.year == t.year:
            y, m = fm.add_months(t, 1)
            nxt = fm.day_in_month(y, m, r["pay_day"])
        return {"amount_minor": r["amount_minor"], "currency": r["currency"], "pay_day": r["pay_day"],
                "next": nxt.isoformat(), "days_left": (nxt - t).days}

    def salary_logged_this_month(self) -> bool:
        ms, me = fm.month_bounds(self.clock())
        return self.store.one("SELECT 1 FROM salary_log WHERE paid_on BETWEEN ? AND ?", (ms, me)) is not None

    def log_salary(self, amount=None, currency=None, paid_on=None) -> dict:
        s = self.salary()
        if amount in (None, ""):
            if not s:
                raise FinanceError("How much was the salary? I don't have a salary amount saved yet.")
            minor, cur = s["amount_minor"], s["currency"]
        else:
            cur = fm.norm_currency(currency) if currency else (s["currency"] if s else self.second)
            minor = fm.to_minor(amount, cur)
        d = fm.parse_date(paid_on, self.clock())
        with self.store.tx() as c:
            c.execute("INSERT INTO salary_log(paid_on, amount_minor, currency) VALUES (?, ?, ?)",
                      (d.isoformat(), minor, cur))
        self.changed()
        return {"amount_minor": minor, "currency": cur, "paid_on": d.isoformat(), "split": self.salary_split(minor, cur)}

    def salary_split(self, salary_minor: int, currency: str) -> dict:
        """Arithmetic from the user's own records: salary − this month's DPS and loan
        instalments − average monthly spending (last 3 months) = what's left, which
        could be sent home. A calculation, not advice."""
        commitments = 0
        for d in self.dps_list():
            if d["active"] and d["remaining_count"] > 0:
                v, _ = self.rates.convert(d["monthly_minor"], d["currency"], currency)
                commitments += v or 0
        for l in self.loan_list():
            if l["active"] and l["remaining"] > 0 and l["monthly_minor"]:
                v, _ = self.rates.convert(min(l["monthly_minor"], l["remaining"]), l["currency"], currency)
                commitments += v or 0
        t = self.clock()
        start_y, start_m = fm.add_months(t, -3)
        start = date(start_y, start_m, 1).isoformat()
        end = (t.replace(day=1) - timedelta(days=1)).isoformat()
        rows = self.expenses(start, end)
        spend = self._sum_in(rows, "currency", "amount_minor", currency) or 0
        months = len({r["spent_on"][:7] for r in rows}) or 1
        avg_spend = spend // months if rows else 0
        left = salary_minor - commitments - avg_spend
        home, _ = self.rates.convert(max(left, 0), currency, self.main)
        return {"currency": currency, "commitments": commitments, "avg_spend": avg_spend,
                "left": left, "left_in_main": home, "months_of_history": len({r["spent_on"][:7] for r in rows})}

    # ══════════════════════════════════════════════════════════════════════
    #  DPS
    # ══════════════════════════════════════════════════════════════════════
    def add_dps(self, monthly, term_months, start_on=None, due_day=None, maturity=None,
                name: str = "DPS", currency=None) -> dict:
        cur = fm.norm_currency(currency) if currency else self.main
        monthly_minor = fm.to_minor(monthly, cur)
        try:
            term = int(term_months)
        except (TypeError, ValueError):
            raise FinanceError("How many months is the DPS term? For example 36, 60 or 120.")
        if not 1 <= term <= 600:
            raise FinanceError("The DPS term must be between 1 and 600 months.")
        start = fm.parse_date(start_on, self.clock())
        day = fm.check_day(due_day if due_day not in (None, "") else start.day)
        mat = fm.to_minor_or_zero(maturity, cur)
        with self.store.tx() as c:
            did = c.execute("INSERT INTO dps(name, monthly_minor, currency, start_on, term_months, due_day, "
                            "maturity_minor, active) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                            (str(name or "DPS")[:60], monthly_minor, cur, start.isoformat(), term, day, mat)).lastrowid
        self.changed()
        return self.dps_status(did)

    def _find(self, table: str, ident, name_col: str):
        rows = self.store.q(f"SELECT * FROM {table} WHERE active=1 ORDER BY id")  # table is a constant
        if not rows:
            return None
        if ident in (None, ""):
            if len(rows) == 1:
                return rows[0]
            raise FinanceError(f"You have {len(rows)}: " +
                               ", ".join(f"{r[name_col] or '#' + str(r['id'])}" for r in rows) + ". Which one?")
        s = str(ident).strip().lower().lstrip("#")
        for r in rows:
            if s == str(r["id"]) or (r[name_col] and s in r[name_col].lower()):
                return r
        raise FinanceError(f"I couldn't find '{ident}'.")

    def pay_dps(self, which=None, amount=None, paid_on=None) -> dict:
        d = self._find("dps", which, "name")
        if d is None:
            raise FinanceError("You have no DPS saved yet.")
        minor = fm.to_minor(amount, d["currency"]) if amount not in (None, "") else d["monthly_minor"]
        when = fm.parse_date(paid_on, self.clock())
        with self.store.tx() as c:
            c.execute("INSERT INTO dps_payments(dps_id, paid_on, amount_minor) VALUES (?, ?, ?)",
                      (d["id"], when.isoformat(), minor))
        self.changed()
        return self.dps_status(d["id"])

    def dps_status(self, dps_id: int) -> dict:
        d = self.store.one("SELECT * FROM dps WHERE id=?", (int(dps_id),))
        if d is None:
            raise FinanceError("That DPS doesn't exist.")
        pays = self.store.one("SELECT COUNT(*), COALESCE(SUM(amount_minor), 0) FROM dps_payments WHERE dps_id=?",
                              (d["id"],))
        paid_count, paid_sum = int(pays[0]), int(pays[1])
        start = date.fromisoformat(d["start_on"])
        t = self.clock()
        my, mm = fm.add_months(start, d["term_months"] - 1)
        last_due = fm.day_in_month(my, mm, d["due_day"])
        first_due = fm.next_due(d["due_day"], start)
        due_so_far = 0
        if t >= first_due:
            due_so_far = min(d["term_months"],
                             (t.year - first_due.year) * 12 + (t.month - first_due.month) + 1
                             - (1 if fm.day_in_month(t.year, t.month, d["due_day"]) > t else 0))
        nxt = None
        if paid_count < d["term_months"]:
            k = paid_count   # the next unpaid instalment is number paid_count+1
            y, m = fm.add_months(first_due.replace(day=1), k)
            nxt = fm.day_in_month(y, m, d["due_day"])
        return {"id": d["id"], "name": d["name"], "currency": d["currency"], "monthly_minor": d["monthly_minor"],
                "term_months": d["term_months"], "paid_count": paid_count, "paid_minor": paid_sum,
                "remaining_count": max(d["term_months"] - paid_count, 0),
                "remaining_minor": max(d["term_months"] - paid_count, 0) * d["monthly_minor"],
                "behind": max(due_so_far - paid_count, 0), "maturity_minor": d["maturity_minor"],
                "maturity_on": last_due.isoformat(), "next_due": nxt.isoformat() if nxt else None,
                "active": bool(d["active"])}

    def dps_list(self) -> list[dict]:
        return [self.dps_status(r["id"]) for r in self.store.q("SELECT id FROM dps WHERE active=1 ORDER BY id")]

    def close_dps(self, which) -> None:
        d = self._find("dps", which, "name")
        if d is None:
            raise FinanceError("You have no DPS saved.")
        with self.store.tx() as c:
            c.execute("UPDATE dps SET active=0 WHERE id=?", (d["id"],))
        self.changed()

    # ══════════════════════════════════════════════════════════════════════
    #  Loans
    # ══════════════════════════════════════════════════════════════════════
    def add_loan(self, amount, lender: str = "", monthly=None, due_day=None, interest_pct=None,
                 start_on=None, currency=None) -> dict:
        cur = fm.norm_currency(currency) if currency else self.main
        principal = fm.to_minor(amount, cur)
        monthly_minor = fm.to_minor_or_zero(monthly, cur)
        if monthly_minor > principal:
            raise FinanceError("The monthly payment is bigger than the whole loan.")
        day = fm.check_day(due_day) if due_day not in (None, "") else None
        bps = None
        if interest_pct not in (None, ""):
            try:
                bps = int(round(float(interest_pct) * 100))
            except (TypeError, ValueError):
                raise FinanceError("The interest rate should be a number, like 9 for 9%.")
            if not 0 <= bps <= 10000:
                raise FinanceError("The interest rate must be between 0 and 100%.")
        start = fm.parse_date(start_on, self.clock())
        with self.store.tx() as c:
            lid = c.execute("INSERT INTO loans(lender, principal_minor, currency, monthly_minor, due_day, "
                            "interest_bps, start_on, active) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                            (str(lender or "")[:80], principal, cur, monthly_minor, day, bps,
                             start.isoformat())).lastrowid
        self.changed()
        return self.loan_status(lid)

    def pay_loan(self, which=None, amount=None, paid_on=None) -> dict:
        l = self._find("loans", which, "lender")
        if l is None:
            raise FinanceError("You have no loans saved.")
        if amount in (None, ""):
            if not l["monthly_minor"]:
                raise FinanceError("How much did you pay?")
            minor = l["monthly_minor"]
        else:
            minor = fm.to_minor(amount, l["currency"])
        st = self.loan_status(l["id"])
        if minor > st["remaining"]:
            raise FinanceError(f"That's more than what's left on this loan "
                               f"({fm.spoken(st['remaining'], l['currency'])}).")
        when = fm.parse_date(paid_on, self.clock())
        with self.store.tx() as c:
            c.execute("INSERT INTO loan_payments(loan_id, paid_on, amount_minor) VALUES (?, ?, ?)",
                      (l["id"], when.isoformat(), minor))
            if minor == st["remaining"]:
                c.execute("UPDATE loans SET active=0 WHERE id=?", (l["id"],))
        self.changed()
        return self.loan_status(l["id"])

    def loan_status(self, loan_id: int) -> dict:
        l = self.store.one("SELECT * FROM loans WHERE id=?", (int(loan_id),))
        if l is None:
            raise FinanceError("That loan doesn't exist.")
        paid = int(self.store.one("SELECT COALESCE(SUM(amount_minor), 0) FROM loan_payments WHERE loan_id=?",
                                  (l["id"],))[0])
        remaining = max(l["principal_minor"] - paid, 0)
        t = self.clock()
        nxt = None
        if remaining > 0 and l["due_day"]:
            nxt = fm.next_due(l["due_day"], t)
            ms, me = fm.month_bounds(nxt)
            if self.store.one("SELECT 1 FROM loan_payments WHERE loan_id=? AND paid_on BETWEEN ? AND ?",
                              (l["id"], ms, me)):
                y, m = fm.add_months(nxt, 1)
                nxt = fm.day_in_month(y, m, l["due_day"])
        months_left = None
        if l["monthly_minor"]:
            months_left = -(-remaining // l["monthly_minor"])
        return {"id": l["id"], "lender": l["lender"], "currency": l["currency"], "principal": l["principal_minor"],
                "paid": paid, "remaining": remaining, "monthly_minor": l["monthly_minor"],
                "due_day": l["due_day"], "interest_bps": l["interest_bps"],
                "next_due": nxt.isoformat() if nxt else None, "months_left": months_left,
                "active": bool(l["active"])}

    def loan_list(self) -> list[dict]:
        return [self.loan_status(r["id"]) for r in self.store.q("SELECT id FROM loans WHERE active=1 ORDER BY id")]

    # ══════════════════════════════════════════════════════════════════════
    #  Reminders
    # ══════════════════════════════════════════════════════════════════════
    def due_alerts(self) -> list[tuple[str, str]]:
        """(key, message) for every reminder due today. Pure: doesn't mark anything."""
        t = self.clock()
        before = int(self.store.setting("alert_days_before") or 3)
        out: list[tuple[str, str]] = []

        def window(due: date, key: str, what_day: str, what_pre: str):
            days = (due - t).days
            if days == 0:
                out.append((f"{key}:{due.isoformat()}:day", what_day))
            elif 0 < days <= before:
                out.append((f"{key}:{due.isoformat()}:pre",
                            what_pre.format(days=days, when="tomorrow" if days == 1 else f"in {days} days")))

        s = self.salary()
        if s:   # salary() already skips to next month once this month's salary is logged
            due = date.fromisoformat(s["next"])
            window(due, "salary", "Today is salary day. Say 'salary received' to log it, and I'll show how it "
                                  "splits across your DPS, loans and money for home.",
                   "Salary is due {when}.")
        for d in self.dps_list():
            if d["next_due"] and d["remaining_count"] > 0:
                due = date.fromisoformat(d["next_due"])
                label = d["name"] or "DPS"
                if due < t:
                    out.append((f"dps{d['id']}:{due.isoformat()}:late",
                                f"The {label} instalment due {due.isoformat()} isn't marked as paid."))
                else:
                    window(due, f"dps{d['id']}", f"The {label} instalment is due today.",
                           f"The {label} instalment is due {{when}}.")
        for l in self.loan_list():
            if l["next_due"] and l["remaining"] > 0:
                due = date.fromisoformat(l["next_due"])
                label = f"loan from {l['lender']}" if l["lender"] else "loan"
                window(due, f"loan{l['id']}", f"The {label} payment is due today.",
                       f"The {label} payment is due {{when}}.")
        return out

    def pending_alerts(self) -> list[tuple[str, str]]:
        """Reminders due now that haven't been given yet."""
        out = []
        for key, msg in self.due_alerts():
            if not self.store.one("SELECT 1 FROM alert_log WHERE key=?", (key,)):
                out.append((key, msg))
        return out

    def mark_alerted(self, key: str) -> None:
        if self.store.read_only:
            return
        with self.store.tx() as c:
            c.execute("INSERT OR IGNORE INTO alert_log(key, fired_at) VALUES (?, ?)", (key, fm.utc_now()))
            c.execute("DELETE FROM alert_log WHERE fired_at < ?",
                      ((self.clock() - timedelta(days=400)).isoformat(),))


class AlertWatcher:
    """Checks for due reminders every few minutes in a background thread and
    hands each one to notify() once."""

    def __init__(self, finance: Finance, notify: Callable[[str], None], every_s: float = 600):
        self.f, self.notify, self.every = finance, notify, every_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def check_now(self) -> list[str]:
        said = []
        try:
            for key, msg in self.f.pending_alerts():
                self.notify(msg)
                self.f.mark_alerted(key)
                said.append(msg)
        except Exception as e:
            print(f"[Finance] reminder check failed: {type(e).__name__}")
        return said

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop():
            self._stop.wait(20)          # let Habibi finish starting first
            while not self._stop.is_set():
                self.check_now()
                self._stop.wait(self.every)

        self._thread = threading.Thread(target=loop, name="finance-alerts", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
