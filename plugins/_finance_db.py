"""
plugins/_finance_db.py — the personal finance database.

One SQLite database, kept in memory while Habibi runs and written to
config/finance/finance.db after every change. On Windows the file on disk is
encrypted with DPAPI (core/secure.py): the key is held by Windows for YOUR
account on THIS PC, so the copy on disk, in backups or on a USB stick is only
scrambled bytes. On other systems the file is plain SQLite.

  • every write runs inside one transaction (BEGIN … COMMIT, ROLLBACK on error)
    and is saved to disk atomically (temp file + replace)
  • parameterised queries only
  • schema_version + forward-only migrations; the file is backed up before each
  • one automatic backup per day, the last 7 kept, in config/finance/backups/
  • export / import with your own passphrase (works on another PC)
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Callable, Optional



def _sibling(stem: str):
    """Import plugins/<stem>.py by path (works from source and from the .exe)."""
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

FILE_MAGIC = b"HABIBI-FIN-DPAPI-1\n"
EXPORT_MAGIC = b"HABIBI-FIN-EXPORT-1\n"
SQLITE_MAGIC = b"SQLite format 3\x00"
BACKUPS_KEPT = 7

DEFAULT_SETTINGS = {
    "home_country": "Bangladesh",
    "work_country": "Saudi Arabia",
    "main_currency": "BDT",
    "second_currency": "SAR",
    "alert_days_before": "3",
    "hide_amounts": "0",
    "pin_hash": "",
}

# Forward-only migrations. Never edit a released step; add a new one.
MIGRATIONS: dict[int, list[str]] = {
    1: [
        """CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS expenses(
            id INTEGER PRIMARY KEY,
            amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
            currency TEXT NOT NULL CHECK(length(currency) = 3),
            category TEXT NOT NULL DEFAULT 'Other',
            note TEXT NOT NULL DEFAULT '',
            spent_on TEXT NOT NULL,
            created_at TEXT NOT NULL)""",
        """CREATE INDEX IF NOT EXISTS ix_expenses_spent_on ON expenses(spent_on)""",
        """CREATE TABLE IF NOT EXISTS rates(
            id INTEGER PRIMARY KEY,
            base TEXT NOT NULL CHECK(length(base) = 3),
            quote TEXT NOT NULL CHECK(length(quote) = 3),
            rate_micro INTEGER NOT NULL CHECK(rate_micro > 0),
            fetched_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            is_manual INTEGER NOT NULL DEFAULT 0 CHECK(is_manual IN (0, 1)))""",
        """CREATE INDEX IF NOT EXISTS ix_rates_pair ON rates(base, quote, fetched_at)""",
        """CREATE TABLE IF NOT EXISTS remittances(
            id INTEGER PRIMARY KEY,
            sent_on TEXT NOT NULL,
            amount_sar_minor INTEGER NOT NULL CHECK(amount_sar_minor > 0),
            rate_micro INTEGER NOT NULL CHECK(rate_micro > 0),
            fee_sar_minor INTEGER NOT NULL DEFAULT 0 CHECK(fee_sar_minor >= 0),
            received_bdt_minor INTEGER NOT NULL CHECK(received_bdt_minor > 0),
            recipient TEXT NOT NULL DEFAULT '',
            method TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '')""",
        """CREATE INDEX IF NOT EXISTS ix_remit_sent_on ON remittances(sent_on)""",
        """CREATE TABLE IF NOT EXISTS salary(
            id INTEGER PRIMARY KEY,
            amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
            currency TEXT NOT NULL CHECK(length(currency) = 3),
            pay_day INTEGER NOT NULL CHECK(pay_day BETWEEN 1 AND 31),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)))""",
        """CREATE TABLE IF NOT EXISTS salary_log(
            id INTEGER PRIMARY KEY,
            paid_on TEXT NOT NULL,
            amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
            currency TEXT NOT NULL CHECK(length(currency) = 3))""",
        """CREATE TABLE IF NOT EXISTS dps(
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            monthly_minor INTEGER NOT NULL CHECK(monthly_minor > 0),
            currency TEXT NOT NULL CHECK(length(currency) = 3),
            start_on TEXT NOT NULL,
            term_months INTEGER NOT NULL CHECK(term_months > 0),
            due_day INTEGER NOT NULL CHECK(due_day BETWEEN 1 AND 31),
            maturity_minor INTEGER NOT NULL DEFAULT 0 CHECK(maturity_minor >= 0),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)))""",
        """CREATE TABLE IF NOT EXISTS dps_payments(
            id INTEGER PRIMARY KEY,
            dps_id INTEGER NOT NULL REFERENCES dps(id) ON DELETE CASCADE,
            paid_on TEXT NOT NULL,
            amount_minor INTEGER NOT NULL CHECK(amount_minor > 0))""",
        """CREATE INDEX IF NOT EXISTS ix_dps_pay ON dps_payments(dps_id)""",
        """CREATE TABLE IF NOT EXISTS loans(
            id INTEGER PRIMARY KEY,
            lender TEXT NOT NULL DEFAULT '',
            principal_minor INTEGER NOT NULL CHECK(principal_minor > 0),
            currency TEXT NOT NULL CHECK(length(currency) = 3),
            monthly_minor INTEGER NOT NULL DEFAULT 0 CHECK(monthly_minor >= 0),
            due_day INTEGER CHECK(due_day IS NULL OR due_day BETWEEN 1 AND 31),
            interest_bps INTEGER CHECK(interest_bps IS NULL OR interest_bps >= 0),
            start_on TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)))""",
        """CREATE TABLE IF NOT EXISTS loan_payments(
            id INTEGER PRIMARY KEY,
            loan_id INTEGER NOT NULL REFERENCES loans(id) ON DELETE CASCADE,
            paid_on TEXT NOT NULL,
            amount_minor INTEGER NOT NULL CHECK(amount_minor > 0))""",
        """CREATE INDEX IF NOT EXISTS ix_loan_pay ON loan_payments(loan_id)""",
    ],
    2: [
        # which reminders were already given, so each one is said once
        """CREATE TABLE IF NOT EXISTS alert_log(
            key TEXT PRIMARY KEY,
            fired_at TEXT NOT NULL)""",
        # remittances work for any pair of countries, not only Saudi → Bangladesh
        """ALTER TABLE remittances ADD COLUMN sent_currency TEXT NOT NULL DEFAULT 'SAR'""",
        """ALTER TABLE remittances ADD COLUMN received_currency TEXT NOT NULL DEFAULT 'BDT'""",
    ],
}
SCHEMA_VERSION = max(MIGRATIONS)


# ── encryption at rest ────────────────────────────────────────────────────────

def _dpapi_pair() -> tuple[Optional[Callable], Optional[Callable]]:
    if sys.platform != "win32":
        return None, None
    try:
        from core.secure import _dpapi
    except Exception:
        return None, None
    return (lambda b: _dpapi(b, True)), (lambda b: _dpapi(b, False))


def data_dir() -> Path:
    try:
        from memory.config_manager import get_base_dir
        base = get_base_dir()
    except Exception:
        base = Path(__file__).resolve().parent.parent
    return base / "config" / "finance"


class FinanceStore:
    """Thread-safe access to the finance database."""

    def __init__(self, path: Optional[Path] = None,
                 encrypt: Optional[Callable[[bytes], bytes]] = None,
                 decrypt: Optional[Callable[[bytes], bytes]] = None,
                 use_os_encryption: bool = True):
        self.path = Path(path) if path else data_dir() / "finance.db"
        self.backup_dir = self.path.parent / "backups"
        if encrypt is None and decrypt is None and use_os_encryption:
            encrypt, decrypt = _dpapi_pair()
        self._enc, self._dec = encrypt, decrypt
        self._lock = threading.RLock()
        self.read_only = False      # set if the file exists but can't be opened
        self.load_error = ""
        self.conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._open()

    @property
    def encrypted(self) -> bool:
        return self._enc is not None

    # -- file I/O --------------------------------------------------------------
    def _read_bytes(self) -> Optional[bytes]:
        if not self.path.exists():
            return None
        raw = self.path.read_bytes()
        if raw.startswith(FILE_MAGIC):
            if self._dec is None:
                raise fm.FinanceError("The finance file is encrypted by Windows and can only "
                                      "be opened on the PC and Windows account that made it.")
            try:
                return self._dec(raw[len(FILE_MAGIC):])
            except Exception:
                raise fm.FinanceError("Windows could not unlock the finance file. It was made "
                                      "by another Windows account or PC. Use an export file to "
                                      "move your records instead.")
        if raw.startswith(SQLITE_MAGIC):
            return raw
        if not raw:
            return None
        raise fm.FinanceError("The finance file is damaged. Restore one of the backups in "
                              f"{self.backup_dir}.")

    def _open(self) -> None:
        try:
            data = self._read_bytes()
        except fm.FinanceError as e:
            # Keep the file untouched: work in memory only, never overwrite it.
            self.read_only, self.load_error = True, str(e)
            data = None
        if data:
            try:
                self.conn.deserialize(data)
                self.conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            except sqlite3.DatabaseError:
                self.read_only = True
                self.load_error = ("The finance file couldn't be read. It's left untouched; restore a "
                                   f"backup from {self.backup_dir} or import an export.")
                self.conn.close()
                self.conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
                self.conn.row_factory = sqlite3.Row
                data = None
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate(had_file=bool(data))

    def _save(self) -> None:
        if self.read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._daily_backup()
        data = self.conn.serialize()
        if self._enc is not None:
            data = FILE_MAGIC + self._enc(bytes(data))
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _daily_backup(self) -> None:
        if not self.path.exists():
            return
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            dest = self.backup_dir / f"finance-{date.today().isoformat()}.db"
            if not dest.exists():
                shutil.copy2(self.path, dest)
            daily = sorted(self.backup_dir.glob("finance-????-??-??.db"))
            for old in daily[:-BACKUPS_KEPT]:
                old.unlink(missing_ok=True)
        except OSError as e:
            print(f"[Finance] daily backup skipped: {type(e).__name__}")

    # -- schema ----------------------------------------------------------------
    def schema_version(self) -> int:
        try:
            r = self.conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
            return int(r[0]) if r else 0
        except sqlite3.OperationalError:
            return 0

    def _migrate(self, had_file: bool) -> None:
        cur = self.schema_version()
        if cur > SCHEMA_VERSION:
            self.read_only = True
            self.load_error = ("The finance file was made by a newer Habibi. Update Habibi "
                               "before changing your records.")
            return
        if cur == SCHEMA_VERSION:
            return
        if had_file and self.path.exists() and not self.read_only:
            try:
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.path, self.backup_dir /
                             f"finance-pre-v{SCHEMA_VERSION}-from-v{cur}-{date.today().isoformat()}.db")
            except OSError as e:
                raise fm.FinanceError(f"Couldn't back up the finance file before updating it: {e}")
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                for v in range(cur + 1, SCHEMA_VERSION + 1):
                    for sql in MIGRATIONS[v]:
                        self.conn.execute(sql)
                for k, val in DEFAULT_SETTINGS.items():
                    self.conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, val))
                self.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('schema_version', ?)",
                                  (str(SCHEMA_VERSION),))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            self._save()

    # -- transactions ----------------------------------------------------------
    @contextmanager
    def tx(self):
        """One write = one transaction. Rolled back on any error; saved on success."""
        if self.read_only:
            raise fm.FinanceError(self.load_error or "The finance records are read-only right now.")
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            try:
                self._save()
            except OSError as e:
                raise fm.FinanceError(f"Saved in memory, but writing the file failed ({e}). "
                                      "It will be saved with the next change.")

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()):
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    # -- settings --------------------------------------------------------------
    def setting(self, key: str, default: str = "") -> str:
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r[0] if r else DEFAULT_SETTINGS.get(key, default)

    def set_settings(self, **values) -> None:
        with self.tx() as c:
            for k, v in values.items():
                c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, str(v)))

    # -- export / import (passphrase, portable) --------------------------------
    def export_to(self, dest: Path, passphrase: str) -> Path:
        if len(passphrase or "") < 6:
            raise fm.FinanceError("Use a passphrase of at least 6 characters for the export.")
        f, salt = _fernet(passphrase)
        with self._lock:
            blob = bytes(self.conn.serialize())
        dest = Path(dest)
        dest.write_bytes(EXPORT_MAGIC + salt + f.encrypt(blob))
        return dest

    def import_from(self, src: Path, passphrase: str) -> None:
        raw = Path(src).read_bytes()
        if not raw.startswith(EXPORT_MAGIC):
            raise fm.FinanceError("That isn't a Habibi finance export file.")
        body = raw[len(EXPORT_MAGIC):]
        salt, token = body[:16], body[16:]
        f, _ = _fernet(passphrase, salt)
        try:
            blob = f.decrypt(token)
        except Exception:
            raise fm.FinanceError("Wrong passphrase, or the export file is damaged.")
        test = sqlite3.connect(":memory:")
        try:
            test.deserialize(blob)
            test.execute("SELECT key FROM settings LIMIT 1").fetchall()
        except Exception:
            raise fm.FinanceError("The export file doesn't hold finance records.")
        finally:
            test.close()
        with self._lock:
            if self.path.exists():
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.path, self.backup_dir / f"finance-before-import-{fm.utc_now()[:10]}.db")
            self.conn.deserialize(blob)
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.read_only, self.load_error = False, ""
            self._migrate(had_file=False)
            self._save()


def _fernet(passphrase: str, salt: Optional[bytes] = None):
    try:
        import base64
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError:
        raise fm.FinanceError("Encrypted export needs the 'cryptography' package.")
    salt = salt or os.urandom(16)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=390_000)
    key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
    return Fernet(key), salt
