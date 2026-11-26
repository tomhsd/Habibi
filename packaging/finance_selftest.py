"""
packaging/finance_selftest.py — checks for the personal finance plugin.

    python packaging/finance_selftest.py           # all checks, no network
    python packaging/finance_selftest.py --live    # also fetch a real SAR→BDT rate

Covers: money rounding, BDT↔SAR conversion both ways, month-end due days,
migration from an older schema (with backup), bad input rejection, the
encrypted file reopening (real DPAPI on Windows, a stand-in cipher elsewhere),
expenses read back, remittances, salary/DPS/loan reminders firing on their
dates, rate fallback when offline, export/import, private activity log,
the voice tool, and (when PyQt6 is present) building the FINANCE card.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LIVE = "--live" in sys.argv
if LIVE:
    sys.argv.remove("--live")

import importlib.util  # noqa: E402


def _load(stem):
    name = f"plugins.{stem}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "plugins" / f"{stem}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fm = _load("_finance_money")
fdb = _load("_finance_db")
fcore = _load("_finance_core")
FinanceError = fm.FinanceError


def offline_fetch(base, quote):
    raise OSError("offline")


def fixed_fetch(rates):
    def f(base, quote):
        if (base, quote) in rates:
            return fm.rate_to_micro(rates[(base, quote)]), "test"
        raise OSError("no rate")
    return f


def xor_cipher(b: bytes) -> bytes:          # stand-in for DPAPI off Windows
    return bytes(x ^ 0x5A for x in b)


class Clock:
    def __init__(self, d):
        self.d = d

    def __call__(self):
        return self.d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="habibi-fin-"))
        self.path = self.tmp / "finance.db"
        self.clock = Clock(date(2026, 11, 25))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, fetch=None, **kw):
        store = fdb.FinanceStore(self.path, **kw) if kw else fdb.FinanceStore(self.path, use_os_encryption=True)
        return fcore.Finance(store, fetcher=fetch or fixed_fetch({("SAR", "BDT"): "32.50"}), clock=self.clock)


class Money(unittest.TestCase):
    def test_rounding(self):
        self.assertEqual(fm.to_minor("10.005", "SAR"), 1001)       # half up, not banker's
        self.assertEqual(fm.to_minor("10.004", "SAR"), 1000)
        self.assertEqual(fm.to_minor("1,234.5", "BDT"), 123450)
        self.assertEqual(fm.to_minor("1.0005", "KWD"), 1001)       # 3-decimal currency
        self.assertEqual(fm.to_minor(0.1 + 0.2, "SAR"), 30)        # float noise is rounded away
        self.assertEqual(fm.fmt(123450, "BDT"), "৳ 1,234.50")
        self.assertEqual(fm.fmt(5000, "SAR"), "SR 50")
        self.assertEqual(fm.fmt(5000, "SAR", hide=True), "SR •••")

    def test_convert_both_ways(self):
        r = fm.rate_to_micro("32.50")                              # 1 SAR = 32.50 BDT
        self.assertEqual(fm.convert(10000, "SAR", "BDT", r), 325000)   # 100 SAR → 3,250 BDT
        inv = fm.invert_rate(r)                                    # 1 BDT = 0.030769 SAR
        self.assertEqual(inv, 30769)
        self.assertEqual(fm.convert(325000, "BDT", "SAR", inv), 10000)  # 3,250 BDT → 99.99925 → 100 SAR
        self.assertEqual(fm.convert(1, "SAR", "BDT", r), 33)       # 0.01 SAR → 0.325 → 0.33 BDT
        self.assertEqual(fm.convert(500, "SAR", "SAR", r), 500)

    def test_month_end_due_days(self):
        self.assertEqual(fm.day_in_month(2026, 2, 31), date(2026, 2, 28))
        self.assertEqual(fm.day_in_month(2028, 2, 30), date(2028, 2, 29))
        self.assertEqual(fm.day_in_month(2026, 4, 31), date(2026, 4, 30))
        self.assertEqual(fm.next_due(31, date(2026, 11, 25)), date(2026, 11, 30))
        self.assertEqual(fm.next_due(31, date(2027, 2, 1)), date(2027, 2, 28))
        self.assertEqual(fm.next_due(5, date(2026, 12, 6)), date(2027, 1, 5))

    def test_bad_input(self):
        for bad in ("0", "-5", "abc", "nan", "inf", ""):
            with self.assertRaises(FinanceError, msg=bad):
                fm.to_minor(bad, "SAR")
        with self.assertRaises(FinanceError):
            fm.norm_currency("XYZ")
        with self.assertRaises(FinanceError):
            fm.check_day(32)
        with self.assertRaises(FinanceError):
            fm.check_day(0)
        with self.assertRaises(FinanceError):
            fm.parse_date("2026-02-30")
        with self.assertRaises(FinanceError):
            fm.rate_to_micro("-1")

    def test_parse_voice_text(self):
        self.assertEqual(fm.parse_expense_text("spent 50 riyal on food"),
                         {"amount": "50", "currency": "SAR", "category": "Food"})
        self.assertEqual(fm.parse_expense_text("spent fifty"), {})
        self.assertEqual(fm.parse_expense_text("200 taka taxi"),
                         {"amount": "200", "currency": "BDT", "category": "Transport"})


class Records(Base):
    def test_expense_add_and_read_back(self):
        f = self.make()
        f.add_expense("50", "riyal", "food", note="lunch")
        f.add_expense("120", "BDT", "phone")
        f.add_expense("10", "SAR", "transport", spent_on="2026-11-01")
        f2 = self.make()                                          # reopen from disk
        tm = f2.today_and_month()
        self.assertEqual(tm["today"]["count"], 2)
        self.assertEqual(tm["today"]["by_currency"], {"SAR": 5000, "BDT": 12000})
        self.assertEqual(tm["today"]["main"], 5000 * 325 // 10 + 12000)      # 1,625 + 120 BDT
        self.assertEqual(tm["month"]["count"], 3)
        last = f2.last_expense()
        self.assertEqual((last["amount_minor"], last["currency"], last["category"]), (1000, "SAR", "Transport"))

    def test_unclear_expense_asks(self):
        f = self.make()
        with self.assertRaisesRegex(FinanceError, "Which currency"):
            f.add_expense("50", None, "food")
        with self.assertRaisesRegex(FinanceError, "What was it for"):
            f.add_expense("50", "SAR", "")
        self.assertEqual(f.today_and_month()["month"]["count"], 0)

    def test_failed_write_rolls_back(self):
        f = self.make()
        with self.assertRaises(sqlite3.IntegrityError):
            with f.store.tx() as c:
                c.execute("INSERT INTO expenses(amount_minor, currency, category, note, spent_on, created_at) "
                          "VALUES (500, 'SAR', 'Food', '', '2026-11-25', 'x')")
                c.execute("INSERT INTO expenses(amount_minor, currency, category, note, spent_on, created_at) "
                          "VALUES (-1, 'SAR', 'Food', '', '2026-11-25', 'x')")     # CHECK fails
        self.assertEqual(f.store.one("SELECT COUNT(*) FROM expenses")[0], 0)
        with self.assertRaises(sqlite3.IntegrityError):                            # FK enforced
            with f.store.tx() as c:
                c.execute("INSERT INTO dps_payments(dps_id, paid_on, amount_minor) VALUES (99, '2026-11-25', 1)")

    def test_remittance(self):
        f = self.make()
        r = f.add_remittance("1000", fee="15", recipient="Mother")
        self.assertEqual(r["received"], 3250000)                  # 32,500 BDT at the fetched rate
        f.add_remittance("500", rate="32.80", fee="10")
        r3 = f.add_remittance("200", received="6500")            # rate worked out from what arrived
        self.assertEqual(r3["rate_micro"], 32_500_000)
        m = f.remittance_month()
        self.assertEqual(m["sent"], {"SAR": 170000})
        self.assertEqual(m["fees"], {"SAR": 2500})
        self.assertEqual(m["received"], {"BDT": 3250000 + 1640000 + 650000})
        with self.assertRaises(FinanceError):
            f.add_remittance("10", fee="10")

    def test_rate_offline_fallbacks(self):
        f = self.make(fetch=offline_fetch)
        with self.assertRaisesRegex(FinanceError, "rate"):
            f.add_remittance("100")                               # no rate at all → asks
        f.add_expense("50", "SAR", "food")                        # saving still works
        f.rates.set_manual("SAR", "BDT", "32.00")
        self.assertEqual(f.add_remittance("100")["received"], 320000)
        self.assertEqual(f.rates.get("BDT", "SAR").micro, fm.invert_rate(32_000_000))
        f.rates.clear_manual("SAR", "BDT")
        with f.store.tx() as c:                                   # an old fetched rate
            c.execute("INSERT INTO rates(base, quote, rate_micro, fetched_at, source, is_manual) "
                      "VALUES ('SAR','BDT',31000000,'2026-01-01T00:00:00+00:00','test',0)")
        r = f.rates.get("SAR", "BDT")
        self.assertTrue(r.stale)
        self.assertIn("old rate", r.describe())

    def test_salary_dps_loan_alerts_fire_on_date(self):
        self.clock.d = date(2026, 11, 20)
        f = self.make()
        f.update_settings(alert_days_before=3)
        f.set_salary("2500", 28, "SAR")
        f.add_dps("2000", 60, start_on="2026-11-05", due_day=31, maturity="150000", name="DPS")
        f.add_loan("50000", lender="Cousin", monthly="5000", due_day=1, currency="BDT")
        self.assertEqual(f.pending_alerts(), [])                  # 20 Nov: nothing yet

        def keys():
            return [k for k, _ in f.pending_alerts()]

        self.clock.d = date(2026, 11, 25)                         # salary in 3 days
        self.assertEqual(keys(), ["salary:2026-11-28:pre"])
        w = fcore.AlertWatcher(f, notify=lambda m: None)
        self.assertEqual(len(w.check_now()), 1)
        self.assertEqual(w.check_now(), [])                       # said once only

        self.clock.d = date(2026, 11, 28)                         # salary day; DPS (30 Nov) & loan (1 Dec) close
        k = keys()
        self.assertIn("salary:2026-11-28:day", k)
        self.assertIn("dps1:2026-11-30:pre", k)                    # 31 → last day of November
        self.assertIn("loan1:2026-12-01:pre", k)
        w.check_now()
        out = f.log_salary()
        self.assertEqual(out["amount_minor"], 250000)
        sp = out["split"]
        self.assertEqual(sp["commitments"], round(200000 / 32.5) + round(500000 / 32.5))
        self.assertEqual(f.salary()["next"], "2026-12-28")        # logged → next month

        self.clock.d = date(2026, 11, 30)
        self.assertEqual(keys(), ["dps1:2026-11-30:day"])
        self.clock.d = date(2026, 12, 1)
        self.assertIn("dps1:2026-11-30:late", keys())              # not marked paid
        self.assertIn("loan1:2026-12-01:day", keys())
        f.pay_dps()
        f.pay_loan()
        self.assertEqual(keys(), [])
        self.assertEqual(f.dps_list()[0]["paid_count"], 1)
        self.assertEqual(f.loan_list()[0]["remaining"], 4500000)
        self.assertEqual(f.loan_list()[0]["next_due"], "2027-01-01")

    def test_dps_and_loan_status(self):
        self.clock.d = date(2026, 11, 25)
        f = self.make()
        d = f.add_dps("1000", 12, start_on="2026-08-10", due_day=10)
        self.assertEqual(d["behind"], 4)                          # Aug, Sep, Oct, Nov due, none paid
        for _ in range(4):
            f.pay_dps()
        s = f.dps_list()[0]
        self.assertEqual((s["paid_count"], s["remaining_count"], s["behind"]), (4, 8, 0))
        self.assertEqual(s["next_due"], "2026-12-10")
        self.assertEqual(s["maturity_on"], "2027-07-10")
        l = f.add_loan("1000", lender="Bank", monthly="400", due_day=15)
        f.pay_loan("bank")
        f.pay_loan("bank")
        with self.assertRaises(FinanceError):
            f.pay_loan("bank", "300")                             # more than what's left (200)
        st = f.pay_loan("bank", "200")
        self.assertEqual(st["remaining"], 0)
        self.assertFalse(st["active"])
        self.assertEqual(f.loan_list(), [])

    def test_settings_countries(self):
        f = self.make()
        self.assertEqual((f.main, f.second), ("BDT", "SAR"))
        f.update_settings(work_country="UAE")
        self.assertEqual(f.second, "AED")
        f.update_settings(home_country="india", work_country="Saudi Arabia")
        self.assertEqual((f.main, f.second), ("INR", "SAR"))
        with self.assertRaises(FinanceError):
            f.update_settings(home_country="Atlantis")

    def test_pin(self):
        f = self.make()
        self.assertFalse(f.locked())
        f.set_pin("4321")
        f.lock()
        self.assertTrue(f.locked())
        self.assertFalse(f.check_pin("0000"))
        self.assertTrue(f.check_pin("4321"))
        self.assertFalse(f.locked())
        with self.assertRaises(FinanceError):
            f.set_pin("12")
        with self.assertRaises(FinanceError):
            f.set_pin("5555", old_pin="1111")


class Storage(Base):
    def test_encrypted_file_reopens(self):
        if sys.platform == "win32":
            f = self.make()                                       # real Windows DPAPI
            self.assertTrue(f.store.encrypted)
        else:
            f = self.make(encrypt=xor_cipher, decrypt=xor_cipher)
        f.add_expense("77", "SAR", "food")
        raw = self.path.read_bytes()
        self.assertTrue(raw.startswith(fdb.FILE_MAGIC))
        self.assertNotIn(b"SQLite format 3", raw)
        self.assertNotIn(b"Food", raw)
        g = self.make() if sys.platform == "win32" else self.make(encrypt=xor_cipher, decrypt=xor_cipher)
        self.assertEqual(g.last_expense()["amount_minor"], 7700)
        # a key that doesn't match → read-only, the file is left untouched
        bad = fdb.FinanceStore(self.path, encrypt=xor_cipher, decrypt=lambda b: b"garbage")
        self.assertTrue(bad.read_only)
        with self.assertRaises(FinanceError):
            fcore.Finance(bad, fetcher=offline_fetch).add_expense("1", "SAR", "food")
        self.assertEqual(self.path.read_bytes(), raw)

    def test_migration_from_v1_with_backup(self):
        con = sqlite3.connect(self.path)
        for sql in fdb.MIGRATIONS[1]:
            con.execute(sql)
        con.execute("INSERT INTO settings VALUES ('schema_version', '1')")
        con.execute("INSERT INTO settings VALUES ('main_currency', 'BDT')")
        con.execute("INSERT INTO remittances(sent_on, amount_sar_minor, rate_micro, fee_sar_minor, "
                    "received_bdt_minor) VALUES ('2026-11-02', 100000, 32500000, 1500, 3250000)")
        con.commit()
        con.close()
        f = self.make(use_os_encryption=False)
        self.assertEqual(f.store.schema_version(), fdb.SCHEMA_VERSION)
        m = f.remittance_month()
        self.assertEqual(m["sent"], {"SAR": 100000})              # old rows got the new columns
        self.assertEqual(m["received"], {"BDT": 3250000})
        backups = list((self.tmp / "backups").glob("finance-pre-v*"))
        self.assertEqual(len(backups), 1)
        f.mark_alerted("x:1")                                     # v2 table works

    def test_newer_schema_is_read_only(self):
        f = self.make(use_os_encryption=False)
        f.store.set_settings(schema_version=99)
        g = fdb.FinanceStore(self.path, use_os_encryption=False)
        self.assertTrue(g.read_only)

    def test_daily_backups_keep_7(self):
        f = self.make(use_os_encryption=False)
        bdir = self.tmp / "backups"
        bdir.mkdir(exist_ok=True)
        for i in range(1, 11):
            (bdir / f"finance-2026-10-{i:02d}.db").write_bytes(b"x")
        f.add_expense("1", "SAR", "food")
        self.assertEqual(len(list(bdir.glob("finance-????-??-??.db"))), 7)

    def test_export_import(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("cryptography not installed")
        f = self.make(use_os_encryption=False)
        f.add_expense("42", "SAR", "food")
        out = f.store.export_to(self.tmp / "export.hfin", "secret-pass")
        self.assertNotIn(b"Food", out.read_bytes())
        other = self.tmp / "other"
        other.mkdir()
        g = fcore.Finance(fdb.FinanceStore(other / "finance.db", use_os_encryption=False),
                          fetcher=offline_fetch, clock=self.clock)
        with self.assertRaises(FinanceError):
            g.store.import_from(out, "wrong-pass")
        g.store.import_from(out, "secret-pass")
        self.assertEqual(g.last_expense()["amount_minor"], 4200)


class Plugin(Base):
    def setUp(self):
        super().setUp()
        self.pf = _load("personal_finance")
        self.pf._F = fcore.Finance(fdb.FinanceStore(self.path, use_os_encryption=False),
                                   fetcher=fixed_fetch({("SAR", "BDT"): "32.50"}), clock=self.clock)

    def run_tool(self, **p):
        return self.pf.run(p)

    def test_voice_flow(self):
        self.assertTrue(self.pf.PRIVATE_LOG)
        out = self.run_tool(action="add_expense", text="spent 50 riyal on food")
        self.assertIn("50 riyal", out)
        self.assertIn("1,625 taka", out)
        self.assertIn("which currency", self.run_tool(action="add_expense", text="spent fifty").lower())
        self.assertIn("Which currency", self.run_tool(action="add_expense", amount="50", category="food"))
        s = self.run_tool(action="spending", period="month")
        self.assertIn("1,625 taka", s)
        self.assertIn("50 riyal", s)
        self.assertIn("32,500 taka", self.run_tool(action="add_remittance", amount="1000", fee="15"))
        self.assertIn("pay day", self.run_tool(action="salary_status").lower())
        self.run_tool(action="set_salary", amount="2500", pay_day="28")
        self.assertIn("28", self.run_tool(action="salary_status"))
        self.assertIn("no DPS", self.run_tool(action="dps_status"))
        self.run_tool(action="add_dps", amount="2000", term_months="60", due_day="5")
        self.assertIn("60", self.run_tool(action="dps_status"))
        self.assertIn("no loans", self.run_tool(action="loan_status").lower())
        self.run_tool(action="add_loan", amount="10000", lender="Bank", monthly="1000", due_day="10")
        self.assertIn("10,000 taka", self.run_tool(action="loan_status"))
        self.assertIn("1 SAR =", self.run_tool(action="convert", amount="100", currency="SAR"))
        self.assertIn("3,250 taka", self.run_tool(action="convert", amount="100", currency="SAR"))
        self.assertIn("100 riyal", self.run_tool(action="convert", amount="3250", currency="BDT"))
        self.assertIn("more than zero", self.run_tool(action="add_expense", amount="-3", currency="SAR", category="food"))
        self.assertIn("Unknown", self.run_tool(action="fly_to_moon"))

    def test_pin_blocks_reading_aloud(self):
        self.pf._F.set_pin("2468")
        self.pf._F.lock()
        self.assertIn("PIN", self.run_tool(action="spending"))
        self.assertIn("unlocked", self.run_tool(action="unlock", pin="2468").lower())
        self.assertNotIn("PIN", self.run_tool(action="spending"))

    def test_private_activity_log(self):
        from core import plugin_loader, safety
        logged = []
        orig = safety.log_activity
        safety.log_activity = lambda tool, params, result: logged.append((tool, params, result))
        try:
            rec = plugin_loader.PluginRecord(name="personal_finance", run=self.pf.run, valid=True)
            reg = plugin_loader.PluginRegistry({"personal_finance": rec}, logger=lambda m: None)
            plugin_loader.get_plugin_enabled = lambda n: True
            reg.run("personal_finance", {"action": "add_expense", "amount": "99", "currency": "SAR",
                                         "category": "food", "note": "secret lunch"})
        finally:
            safety.log_activity = orig
        self.assertEqual(logged, [("personal_finance", {"action": "add_expense"}, "(private: details not logged)")])

    def test_card_builds(self):
        try:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PyQt6.QtWidgets import QApplication
        except Exception:
            self.skipTest("PyQt6 not installed")
        app = QApplication.instance() or QApplication([])
        ui = _load("_finance_ui")
        self.pf._F.add_expense("50", "SAR", "food")
        self.pf._F.rates.get("SAR", "BDT")          # the card itself never waits on the network
        card = ui.FinanceCard(self.pf._F)
        card.refresh()
        self.assertIn("1,625", card.today_lbl.text())
        self.pf._F.update_settings(hide_amounts=True)
        card.refresh()
        self.assertIn("•••", card.today_lbl.text())
        win = ui.FinanceWindow(self.pf._F)
        win.refresh()
        self.assertGreaterEqual(win.tabs.count(), 6)
        if sys.platform == "win32" or os.environ.get("FIN_SHOT"):
            card.resize(300, card.sizeHint().height())
            self.pf._F.update_settings(hide_amounts=False)
            card.refresh()
            card.grab().save(str(ROOT / "finance_card_selftest.png"))
            win.resize(820, 560)
            win.grab().save(str(ROOT / "finance_window_selftest.png"))
        app.processEvents()


@unittest.skipUnless(LIVE, "live rate fetch only with --live")
class Live(unittest.TestCase):
    def test_fetch_sar_bdt(self):
        frates = _load("_finance_rates")
        micro, src = frates.fetch_online("SAR", "BDT")
        rate = micro / 1e6
        print(f"\n  live rate: 1 SAR = {rate:.4f} BDT from {src}")
        self.assertTrue(20 < rate < 60)


if __name__ == "__main__":
    res = unittest.main(verbosity=2, exit=False).result
    ok = res.wasSuccessful()
    print("FINANCE SELF-TEST", "OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)
