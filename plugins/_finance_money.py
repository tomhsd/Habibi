"""
plugins/_finance_money.py — money, currencies, countries and validation for the
personal finance plugin.

Money is always an integer count of the currency's minor unit (paisa for BDT,
halala for SAR, fils for KWD …). Floats are only used when parsing what the
user typed or said, and are turned into integers straight away with
Decimal rounding (half up), so totals never drift.

Exchange rates are integers too: rate_micro = rate × 1,000,000.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

RATE_SCALE = 1_000_000

# code -> (minor-unit digits, symbol, spoken name)
CURRENCIES: dict[str, tuple[int, str, str]] = {
    "BDT": (2, "৳", "taka"),
    "SAR": (2, "SR", "riyal"),
    "AED": (2, "AED", "dirham"),
    "QAR": (2, "QR", "Qatari riyal"),
    "KWD": (3, "KD", "Kuwaiti dinar"),
    "OMR": (3, "OMR", "Omani rial"),
    "BHD": (3, "BD", "Bahraini dinar"),
    "MYR": (2, "RM", "ringgit"),
    "SGD": (2, "S$", "Singapore dollar"),
    "INR": (2, "₹", "rupee"),
    "PKR": (2, "Rs", "Pakistani rupee"),
    "NPR": (2, "Rs", "Nepali rupee"),
    "LKR": (2, "Rs", "Sri Lankan rupee"),
    "PHP": (2, "₱", "peso"),
    "IDR": (2, "Rp", "rupiah"),
    "USD": (2, "$", "dollar"),
    "EUR": (2, "€", "euro"),
    "GBP": (2, "£", "pound"),
    "JPY": (0, "¥", "yen"),
    "KRW": (0, "₩", "won"),
    "CAD": (2, "C$", "Canadian dollar"),
    "AUD": (2, "A$", "Australian dollar"),
    "JOD": (3, "JD", "Jordanian dinar"),
    "TRY": (2, "₺", "lira"),
}

COUNTRIES: dict[str, str] = {
    "Bangladesh": "BDT", "Saudi Arabia": "SAR", "United Arab Emirates": "AED",
    "Qatar": "QAR", "Kuwait": "KWD", "Oman": "OMR", "Bahrain": "BHD",
    "Malaysia": "MYR", "Singapore": "SGD", "India": "INR", "Pakistan": "PKR",
    "Nepal": "NPR", "Sri Lanka": "LKR", "Philippines": "PHP", "Indonesia": "IDR",
    "United States": "USD", "United Kingdom": "GBP", "Italy": "EUR",
    "Germany": "EUR", "France": "EUR", "Spain": "EUR", "Portugal": "EUR",
    "Greece": "EUR", "Japan": "JPY", "South Korea": "KRW", "Canada": "CAD",
    "Australia": "AUD", "Jordan": "JOD", "Turkey": "TRY",
}
_COUNTRY_ALIASES = {
    "ksa": "Saudi Arabia", "saudi": "Saudi Arabia", "uae": "United Arab Emirates",
    "dubai": "United Arab Emirates", "emirates": "United Arab Emirates",
    "usa": "United States", "us": "United States", "america": "United States",
    "uk": "United Kingdom", "england": "United Kingdom", "bd": "Bangladesh",
    "bangla": "Bangladesh", "korea": "South Korea",
}

# words people say → currency code
_CURRENCY_WORDS = {
    "taka": "BDT", "tk": "BDT", "bdt": "BDT", "৳": "BDT", "টাকা": "BDT",
    "riyal": "SAR", "riyals": "SAR", "rial": "SAR", "sar": "SAR", "sr": "SAR", "রিয়াল": "SAR",
    "dirham": "AED", "dirhams": "AED", "aed": "AED",
    "ringgit": "MYR", "rupee": "INR", "rupees": "INR", "dollar": "USD", "dollars": "USD",
    "$": "USD", "euro": "EUR", "euros": "EUR", "pound": "GBP", "pounds": "GBP",
    "dinar": "KWD", "dinars": "KWD",
}

EXPENSE_CATEGORIES = ["Food", "Transport", "Rent", "Phone", "Bills", "Shopping",
                      "Health", "Family", "Education", "Fun", "Other"]
_CATEGORY_WORDS = {
    "food": "Food", "lunch": "Food", "dinner": "Food", "breakfast": "Food",
    "grocery": "Food", "groceries": "Food", "restaurant": "Food", "tea": "Food",
    "coffee": "Food", "transport": "Transport", "taxi": "Transport", "bus": "Transport",
    "uber": "Transport", "careem": "Transport", "fuel": "Transport", "petrol": "Transport",
    "rent": "Rent", "room": "Rent", "phone": "Phone", "mobile": "Phone", "recharge": "Phone",
    "internet": "Phone", "sim": "Phone", "bill": "Bills", "bills": "Bills",
    "electricity": "Bills", "water": "Bills", "shopping": "Shopping", "clothes": "Shopping",
    "medicine": "Health", "doctor": "Health", "health": "Health", "pharmacy": "Health",
    "family": "Family", "gift": "Family", "school": "Education", "education": "Education",
    "course": "Education", "fun": "Fun", "movie": "Fun", "games": "Fun", "other": "Other",
}


class FinanceError(ValueError):
    """A validation problem, phrased so it can be spoken to the user as is."""


# ── currencies & countries ────────────────────────────────────────────────────

def norm_currency(code) -> str:
    c = str(code or "").strip()
    w = _CURRENCY_WORDS.get(c.lower())
    if w:
        return w
    c = c.upper()
    if c not in CURRENCIES:
        raise FinanceError(f"I don't know the currency '{code}'. Use a code like BDT or SAR.")
    return c


def try_currency(code) -> str | None:
    try:
        return norm_currency(code) if str(code or "").strip() else None
    except FinanceError:
        return None


def digits(code: str) -> int:
    return CURRENCIES[norm_currency(code)][0]


def norm_country(name) -> str:
    n = str(name or "").strip()
    if not n:
        raise FinanceError("Tell me the country name.")
    if n.lower() in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[n.lower()]
    for c in COUNTRIES:
        if c.lower() == n.lower():
            return c
    raise FinanceError(f"I don't have '{name}' in my country list yet. "
                       f"Pick one of: {', '.join(sorted(COUNTRIES))}.")


def country_currency(country: str) -> str:
    return COUNTRIES[norm_country(country)]


# ── amounts ───────────────────────────────────────────────────────────────────

def to_minor(amount, currency: str) -> int:
    """'50', 50, '1,234.5' → integer minor units (half-up). Rejects ≤ 0."""
    cur = norm_currency(currency)
    try:
        d = Decimal(str(amount).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise FinanceError(f"'{amount}' isn't a number I can use as an amount.")
    if not d.is_finite():
        raise FinanceError("That amount isn't a real number.")
    q = (d * (10 ** CURRENCIES[cur][0])).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    v = int(q)
    if v <= 0:
        raise FinanceError("The amount has to be more than zero.")
    if v > 10 ** 15:
        raise FinanceError("That amount is too large to be right.")
    return v


def to_minor_or_zero(amount, currency: str) -> int:
    if amount in (None, "", 0, "0"):
        return 0
    return to_minor(amount, currency)


def from_minor(minor: int, currency: str) -> Decimal:
    return Decimal(int(minor)) / (10 ** digits(currency))


def fmt(minor: int, currency: str, hide: bool = False) -> str:
    cur = norm_currency(currency)
    if hide:
        return f"{CURRENCIES[cur][1]} •••"
    d = CURRENCIES[cur][0]
    v = from_minor(minor, cur)
    s = f"{v:,.{d}f}"
    if d and s.endswith("." + "0" * d):
        s = s[: -(d + 1)]
    return f"{CURRENCIES[cur][1]} {s}"


def spoken(minor: int, currency: str) -> str:
    cur = norm_currency(currency)
    d = CURRENCIES[cur][0]
    v = from_minor(minor, cur)
    s = f"{v:,.{d}f}"
    if d and s.endswith("." + "0" * d):
        s = s[: -(d + 1)]
    return f"{s} {CURRENCIES[cur][2]}"


def rate_to_micro(rate) -> int:
    try:
        d = Decimal(str(rate).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise FinanceError(f"'{rate}' isn't a rate I can use.")
    if not d.is_finite() or d <= 0:
        raise FinanceError("The exchange rate must be more than zero.")
    v = int((d * RATE_SCALE).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if v <= 0:
        raise FinanceError("That exchange rate is too small.")
    return v


def micro_to_rate(micro: int) -> Decimal:
    return Decimal(int(micro)) / RATE_SCALE


def convert(minor: int, src: str, dst: str, rate_micro: int) -> int:
    """Convert minor units of src → minor units of dst. rate_micro is how many
    dst per 1 src, × 1e6. Pure integer maths, half-up rounding."""
    src, dst = norm_currency(src), norm_currency(dst)
    if src == dst:
        return int(minor)
    num = int(minor) * int(rate_micro) * (10 ** digits(dst))
    den = RATE_SCALE * (10 ** digits(src))
    q, r = divmod(num, den)
    return q + (1 if r * 2 >= den else 0)


def invert_rate(rate_micro: int) -> int:
    """1/rate in micro units, half-up."""
    num, den = RATE_SCALE * RATE_SCALE, int(rate_micro)
    q, r = divmod(num, den)
    return q + (1 if r * 2 >= den else 0)


# ── dates ─────────────────────────────────────────────────────────────────────

def today() -> date:
    return date.today()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value, default: date | None = None) -> date:
    if value in (None, ""):
        if default is not None:
            return default
        raise FinanceError("Tell me the date.")
    if isinstance(value, date):
        return value
    s = str(value).strip().lower()
    t = today()
    if s == "today":
        return t
    if s == "yesterday":
        return date.fromordinal(t.toordinal() - 1)
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise FinanceError(f"'{value}' isn't a date I understand. Use YYYY-MM-DD.")


def check_day(day) -> int:
    try:
        d = int(day)
    except (TypeError, ValueError):
        raise FinanceError(f"'{day}' isn't a day of the month.")
    if not 1 <= d <= 31:
        raise FinanceError("The day of the month must be between 1 and 31.")
    return d


def day_in_month(year: int, month: int, day: int) -> date:
    """Day 31 in a 30-day month (or 29–31 in February) → that month's last day."""
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(int(day), last))


def add_months(d: date, n: int) -> tuple[int, int]:
    m = d.month - 1 + n
    return d.year + m // 12, m % 12 + 1


def next_due(day: int, from_day: date) -> date:
    """The next date (from_day included) on which a monthly 'day' falls."""
    cand = day_in_month(from_day.year, from_day.month, day)
    if cand >= from_day:
        return cand
    y, m = add_months(from_day, 1)
    return day_in_month(y, m, day)


def month_bounds(d: date) -> tuple[str, str]:
    first = d.replace(day=1)
    last = d.replace(day=calendar.monthrange(d.year, d.month)[1])
    return first.isoformat(), last.isoformat()


def months_between(start: date, end: date) -> int:
    """Whole monthly instalments due from start up to and including end."""
    if end < start:
        return 0
    n = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day >= start.day or end.day == calendar.monthrange(end.year, end.month)[1]:
        n += 1
    return n


# ── text helpers for voice ────────────────────────────────────────────────────

def norm_category(value) -> str | None:
    s = str(value or "").strip().lower()
    if not s:
        return None
    if s in _CATEGORY_WORDS:
        return _CATEGORY_WORDS[s]
    for c in EXPENSE_CATEGORIES:
        if c.lower() == s:
            return c
    return s[:30].title()


_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


def parse_expense_text(text: str) -> dict:
    """'spent 50 riyal on food' → {'amount': '50', 'currency': 'SAR', 'category': 'Food'}.
    Missing pieces are simply absent so the caller can ask for them."""
    out: dict = {}
    s = str(text or "")
    m = _NUM.search(s)
    if m:
        out["amount"] = m.group(1)
    words = re.findall(r"[\w$৳ঀ-৿]+", s.lower())
    for w in words:
        if "currency" not in out and w in _CURRENCY_WORDS:
            out["currency"] = _CURRENCY_WORDS[w]
        if "category" not in out and w in _CATEGORY_WORDS:
            out["category"] = _CATEGORY_WORDS[w]
    return out
