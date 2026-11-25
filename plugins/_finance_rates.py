"""
plugins/_finance_rates.py — exchange rates for the finance plugin.

Order of use:
  1. a manual rate you set (it stays until you clear it)
  2. a fetched rate younger than 12 hours
  3. a fresh fetch from a free source (no API key, sends nothing about you:
     the request is just "latest rates for SAR")
  4. the last rate we ever had, marked as old
  5. nothing → the caller asks you for a manual rate. Saving never waits on this.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


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

FRESH_FOR = timedelta(hours=12)
SOURCES = (
    ("open.er-api.com", "https://open.er-api.com/v6/latest/{base}",
     lambda j, q: j.get("rates", {}).get(q)),
    ("fawazahmed0 currency-api", "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{base_l}.json",
     lambda j, q: j.get(next((k for k in j if k != "date"), ""), {}).get(q.lower())),
)


class Rate:
    def __init__(self, base: str, quote: str, micro: int, fetched_at: str, source: str,
                 manual: bool, stale: bool = False):
        self.base, self.quote, self.micro = base, quote, int(micro)
        self.fetched_at, self.source, self.manual, self.stale = fetched_at, source, manual, stale

    @property
    def value(self):
        return fm.micro_to_rate(self.micro)

    def describe(self) -> str:
        r = f"1 {self.base} = {self.value.normalize():f} {self.quote}"
        if self.manual:
            return r + " (your manual rate)"
        if self.stale:
            return r + f" (old rate from {self.fetched_at[:10]}, couldn't refresh)"
        return r + f" ({self.source}, {self.fetched_at[:16].replace('T', ' ')} UTC)"


def _age(ts: str) -> timedelta:
    try:
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - t
    except ValueError:
        return timedelta(days=9999)


def fetch_online(base: str, quote: str, timeout: float = 8.0) -> tuple[int, str]:
    """→ (rate_micro, source). Raises OSError/ValueError on failure."""
    last = None
    for name, url, pick in SOURCES:
        try:
            req = urllib.request.Request(url.format(base=base, base_l=base.lower()),
                                         headers={"User-Agent": "Habibi"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.loads(r.read().decode("utf-8"))
            v = pick(j, quote)
            if v is None or float(v) <= 0:
                raise ValueError("no rate in reply")
            return fm.rate_to_micro(v), name
        except Exception as e:
            last = e
    raise OSError(f"rate sources unreachable ({type(last).__name__})")


class Rates:
    def __init__(self, store, fetcher=fetch_online):
        self.store = store
        self.fetcher = fetcher

    def _latest(self, base: str, quote: str, manual: int | None = None):
        sql = "SELECT * FROM rates WHERE base=? AND quote=?"
        args: tuple = (base, quote)
        if manual is not None:
            sql += " AND is_manual=?"
            args += (manual,)
        return self.store.one(sql + " ORDER BY fetched_at DESC, id DESC LIMIT 1", args)

    def set_manual(self, base: str, quote: str, rate) -> Rate:
        base, quote = fm.norm_currency(base), fm.norm_currency(quote)
        micro = fm.rate_to_micro(rate)
        with self.store.tx() as c:
            c.execute("DELETE FROM rates WHERE is_manual=1 AND ((base=? AND quote=?) OR (base=? AND quote=?))",
                      (base, quote, quote, base))
            c.execute("INSERT INTO rates(base, quote, rate_micro, fetched_at, source, is_manual) "
                      "VALUES (?, ?, ?, ?, 'manual', 1)", (base, quote, micro, fm.utc_now()))
        return Rate(base, quote, micro, fm.utc_now(), "manual", True)

    def clear_manual(self, base: str, quote: str) -> None:
        with self.store.tx() as c:
            c.execute("DELETE FROM rates WHERE is_manual=1 AND ((base=? AND quote=?) OR (base=? AND quote=?))",
                      (base, quote, quote, base))

    def _from_row(self, row, invert: bool, stale: bool = False) -> Rate:
        micro = fm.invert_rate(row["rate_micro"]) if invert else row["rate_micro"]
        b, q = (row["quote"], row["base"]) if invert else (row["base"], row["quote"])
        return Rate(b, q, micro, row["fetched_at"], row["source"], bool(row["is_manual"]), stale)

    def get(self, base: str, quote: str, allow_fetch: bool = True) -> Rate | None:
        base, quote = fm.norm_currency(base), fm.norm_currency(quote)
        if base == quote:
            return Rate(base, quote, fm.RATE_SCALE, fm.utc_now(), "same currency", False)
        for b, q, inv in ((base, quote, False), (quote, base, True)):
            row = self._latest(b, q, manual=1)
            if row:
                return self._from_row(row, inv)
        for b, q, inv in ((base, quote, False), (quote, base, True)):
            row = self._latest(b, q, manual=0)
            if row and _age(row["fetched_at"]) < FRESH_FOR:
                return self._from_row(row, inv)
        if allow_fetch:
            try:
                try:
                    b, q = base, quote
                    micro, source = self.fetcher(base, quote)
                except Exception:
                    b, q = quote, base                     # some sources only list one direction
                    micro, source = self.fetcher(quote, base)
                if not self.store.read_only:
                    with self.store.tx() as c:
                        c.execute("INSERT INTO rates(base, quote, rate_micro, fetched_at, source, is_manual) "
                                  "VALUES (?, ?, ?, ?, ?, 0)", (b, q, micro, fm.utc_now(), source))
                        # keep the table small: last 60 fetched rates per pair
                        c.execute("DELETE FROM rates WHERE is_manual=0 AND base=? AND quote=? AND id NOT IN "
                                  "(SELECT id FROM rates WHERE is_manual=0 AND base=? AND quote=? "
                                  " ORDER BY fetched_at DESC LIMIT 60)", (b, q, b, q))
                if (b, q) != (base, quote):
                    micro = fm.invert_rate(micro)
                return Rate(base, quote, micro, fm.utc_now(), source, False)
            except Exception as e:
                print(f"[Finance] rate fetch failed: {type(e).__name__}")
        for b, q, inv in ((base, quote, False), (quote, base, True)):
            row = self._latest(b, q, manual=0)
            if row:
                return self._from_row(row, inv, stale=True)
        return None

    def convert(self, minor: int, src: str, dst: str, allow_fetch: bool = True):
        """→ (converted_minor, Rate) or (None, None) if no rate is known."""
        r = self.get(src, dst, allow_fetch)
        if r is None:
            return None, None
        return fm.convert(minor, src, dst, r.micro), r
