"""
plugins/personal_finance.py — Habibi's personal finance tracker, as a plugin.

For people working abroad who send money home: daily expenses, remittances,
salary-day reminders, DPS instalments and loans, in the home currency (BDT by
default) and the working-country currency (SAR by default) side by side.

  "spent 50 riyal on food"            "how much did I spend this month"
  "I sent 1000 riyal home, fee 15"    "when is my salary" / "salary received"
  "DPS status" / "I paid the DPS"     "loan balance" / "I paid the loan"
  "convert 500 riyal to taka"         "open finance"

Everything stays on this PC in config/finance/finance.db (encrypted with
Windows DPAPI). The only network call is the exchange-rate fetch, which sends
no personal data. The activity log records only which action ran
(PRIVATE_LOG below). All figures are the user's own records and estimates;
Habibi gives no financial advice.

Pieces: _finance_money (amounts, currencies), _finance_db (encrypted SQLite,
migrations, backups), _finance_rates, _finance_core (records + reminders),
_finance_ui (white FINANCE card and window).
"""
from __future__ import annotations

import importlib.util
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

PRIVATE_LOG = True    # core: log only the action name, never amounts or names


def _load_sibling(stem: str):
    """Import plugins/<stem>.py by path (works from source and from the .exe,
    where plugins/ is shipped as plain files)."""
    name = f"plugins.{stem}"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return mod


fm = _load_sibling("_finance_money")
fcore = _load_sibling("_finance_core")
FinanceError = fm.FinanceError

_F = None                 # the Finance object, created on first use
_F_lock = threading.Lock()
_player = None
_watcher = None
_card_added = False


def get_finance():
    global _F
    with _F_lock:
        if _F is None:
            _F = fcore.Finance()
        return _F


# ── speech helpers ────────────────────────────────────────────────────────────

def _s(minor, cur) -> str:
    return fm.spoken(minor, cur)


def _both(f, minor: int, cur: str) -> str:
    """'50 riyal (about 1,625 taka)' — the amount plus the other currency."""
    b = f.both(minor, cur)["amounts"]
    others = [_s(v, c) for c, v in b.items() if c != cur]
    return _s(minor, cur) + (f" (about {', '.join(others)})" if others else "")


def _total_line(f, t: dict) -> str:
    if not t["count"]:
        return "nothing recorded"
    if t["main"] is not None and t["second"] is not None:
        return f"{_s(t['main'], f.main)}, that's {_s(t['second'], f.second)}"
    return " plus ".join(_s(v, c) for c, v in t["by_currency"].items())


def _locked_reply(f) -> str | None:
    if f.locked():
        return ("Your finance records are locked with a PIN. Say the PIN or unlock the FINANCE card "
                "first, then ask again.")
    return None


_NOTE = " These are your own records; I don't give financial advice."


# ── actions ───────────────────────────────────────────────────────────────────

def _add_expense(f, p) -> str:
    parsed = fm.parse_expense_text(p.get("text", ""))
    amount = p.get("amount") or parsed.get("amount")
    currency = p.get("currency") or parsed.get("currency")
    category = p.get("category") or parsed.get("category")
    if not amount:
        return "How much did you spend, and in which currency?"
    e = f.add_expense(amount, currency, category, note=p.get("note", ""), spent_on=p.get("date"))
    if f.locked():
        return f"Saved the {e['category'].lower()} expense."
    tm = f.today_and_month()
    when = "" if e["spent_on"] == f.clock().isoformat() else f" on {e['spent_on']}"
    return (f"Saved {_both(f, e['amount_minor'], e['currency'])} for {e['category']}{when}. "
            f"Today so far: {_total_line(f, tm['today'])}. This month: {_total_line(f, tm['month'])}.")


def _spending(f, p) -> str:
    period = str(p.get("period") or "month").lower()
    t = f.clock()
    if period in ("today", "day"):
        start = end = t.isoformat()
        label = "Today"
    elif period in ("yesterday",):
        start = end = (t - timedelta(days=1)).isoformat()
        label = "Yesterday"
    elif period in ("week", "this week"):
        start, end = (t - timedelta(days=t.weekday())).isoformat(), t.isoformat()
        label = "This week"
    elif period in ("last month", "last_month"):
        y, m = fm.add_months(t, -1)
        start, end = fm.month_bounds(date(y, m, 1))
        label = "Last month"
    else:
        start, end = fm.month_bounds(t)
        label = "This month"
    tot = f.expense_totals(start, end)
    if not tot["count"]:
        return f"{label}: no expenses recorded."
    out = f"{label} you spent {_total_line(f, tot)}, over {tot['count']} entries."
    if tot["by_category_main"]:
        top = sorted(tot["by_category_main"].items(), key=lambda kv: -kv[1])[:3]
        out += " Biggest: " + ", ".join(f"{k} {_s(v, f.main)}" for k, v in top) + "."
    return out


def _add_remittance(f, p) -> str:
    if not p.get("amount"):
        return f"How much did you send, in {f.second}?"
    r = f.add_remittance(p.get("amount"), rate=p.get("rate"), fee=p.get("fee"), received=p.get("received"),
                         recipient=p.get("recipient", ""), method=p.get("method", ""), note=p.get("note", ""),
                         sent_on=p.get("date"), currency=p.get("currency"))
    if f.locked():
        return "Saved the transfer."
    out = (f"Saved: sent {_s(r['sent'], r['src'])}"
           + (f" plus {_s(r['fee'], r['src'])} fee" if r["fee"] else "")
           + f", received {_s(r['received'], r['dst'])} at 1 {r['src']} = "
           + f"{fm.micro_to_rate(r['rate_micro']).normalize():f} {r['dst']}.")
    if r["rate_note"]:
        out += f" I used today's rate ({r['rate_note']}); tell me the exact amount received if it was different."
    m = f.remittance_month()
    out += f" This month you've sent {', '.join(_s(v, c) for c, v in m['sent'].items())} in {m['count']} transfers."
    return out


def _remittance_status(f, p) -> str:
    m = f.remittance_month()
    if not m["count"]:
        return "No money sent home recorded this month."
    fees = sum(m["fees"].values())
    return (f"This month: {m['count']} transfers, sent {', '.join(_s(v, c) for c, v in m['sent'].items())}, "
            f"received {', '.join(_s(v, c) for c, v in m['received'].items())}"
            + (f", fees {', '.join(_s(v, c) for c, v in m['fees'].items())}" if fees else "") + ".")


def _salary_status(f, p) -> str:
    s = f.salary()
    if not s:
        return "I don't have your salary saved. Tell me the amount and the pay day, like '2500 riyal on the 28th'."
    days = s["days_left"]
    when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days, on {s['next']}"
    return f"Your pay day is the {s['pay_day']}. Next salary {when}: {_s(s['amount_minor'], s['currency'])}."


def _split_text(f, sp: dict) -> str:
    cur = sp["currency"]
    out = (f"From your records: DPS and loan instalments this month {_s(sp['commitments'], cur)}"
           f", average monthly spending {_s(sp['avg_spend'], cur)}"
           + (" (no past months recorded yet)" if not sp["months_of_history"] else "")
           + f". That leaves {_s(max(sp['left'], 0), cur)}")
    if sp["left_in_main"] is not None and cur != f.main and sp["left"] > 0:
        out += f", about {_s(sp['left_in_main'], f.main)} if you send it home"
    if sp["left"] < 0:
        out += f" — your commitments and usual spending are {_s(-sp['left'], cur)} more than the salary"
    return out + "."


def _log_salary(f, p) -> str:
    r = f.log_salary(p.get("amount"), p.get("currency"), p.get("date"))
    if f.locked():
        return "Salary saved."
    return f"Salary of {_s(r['amount_minor'], r['currency'])} saved for {r['paid_on']}. " + \
        _split_text(f, r["split"]) + _NOTE


def _dps_status(f, p) -> str:
    lst = f.dps_list()
    if not lst:
        return "You have no DPS saved. Tell me the monthly instalment and the term to add one."
    parts = []
    for d in lst:
        c = d["currency"]
        t = (f"{d['name']}: {d['paid_count']} of {d['term_months']} instalments paid "
             f"({_s(d['paid_minor'], c)}), {d['remaining_count']} left ({_s(d['remaining_minor'], c)}). "
             f"Matures {d['maturity_on']}")
        if d["maturity_minor"]:
            t += f" at about {_s(d['maturity_minor'], c)}"
        if d["next_due"]:
            t += f". Next instalment {d['next_due']}"
        if d["behind"]:
            t += f". {d['behind']} instalment(s) due but not marked paid"
        parts.append(t + ".")
    return " ".join(parts)


def _loan_status(f, p) -> str:
    lst = f.loan_list()
    if not lst:
        return "You have no loans saved."
    parts = []
    for l in lst:
        c = l["currency"]
        who = f"Loan from {l['lender']}" if l["lender"] else f"Loan #{l['id']}"
        t = f"{who}: {_s(l['remaining'], c)} left of {_s(l['principal'], c)}"
        if l["monthly_minor"]:
            t += f", {_s(l['monthly_minor'], c)} a month, about {l['months_left']} months to go"
        if l["next_due"]:
            t += f", next payment {l['next_due']}"
        parts.append(t + ".")
    total = {}
    for l in lst:
        total[l["currency"]] = total.get(l["currency"], 0) + l["remaining"]
    if len(lst) > 1:
        parts.append("Total left: " + ", ".join(_s(v, c) for c, v in total.items()) + ".")
    return " ".join(parts)


def _convert(f, p) -> str:
    cur = fm.norm_currency(p.get("currency") or f.second)
    to = fm.norm_currency(p.get("to_currency")) if p.get("to_currency") else (f.main if cur != f.main else f.second)
    minor = fm.to_minor(p.get("amount") or 1, cur)
    v, r = f.rates.convert(minor, cur, to)
    if v is None:
        return f"I couldn't get a {cur} to {to} rate. Tell me a rate to use, like 'set rate 32.5'."
    return f"{_s(minor, cur)} is about {_s(v, to)}. Rate: {r.describe()}."


def _set_rate(f, p) -> str:
    if str(p.get("rate", "")).lower() in ("auto", "clear", "online"):
        f.rates.clear_manual(f.second, f.main)
        return "I'll use the online rate again."
    if not p.get("rate"):
        r = f.rates.get(f.second, f.main)
        return r.describe() if r else "I don't have a rate yet. Tell me one, like 'set rate 32.5'."
    base = fm.norm_currency(p.get("currency") or f.second)
    quote = fm.norm_currency(p.get("to_currency") or (f.main if base != f.main else f.second))
    r = f.rates.set_manual(base, quote, p["rate"])
    f.changed()
    return f"Okay, I'll use {r.describe()} until you say 'use online rate'."


def _settings(f, p) -> str:
    hide = p.get("hide_amounts")
    s = f.update_settings(home_country=p.get("home_country"), work_country=p.get("work_country"),
                          main_currency=p.get("main_currency"), second_currency=p.get("second_currency"),
                          alert_days_before=p.get("alert_days_before"),
                          hide_amounts=None if hide in (None, "") else str(hide).lower() in ("true", "1", "yes", "on"))
    return (f"Home country {s['home_country']} ({s['main_currency']}), working in {s['work_country']} "
            f"({s['second_currency']}). Reminders start {s['alert_days_before']} days before. "
            f"Amounts on screen are {'hidden' if s['hide_amounts'] else 'shown'}.")


def _open(f, p) -> str:
    if _player is None or not hasattr(_player, "run_in_ui"):
        return "The finance window needs the Habibi window."
    ui = _load_sibling("_finance_ui")
    _player.run_in_ui(lambda: ui.open_window(f, p.get("tab") or ""))
    return "Opened your finance records."


_READ_ACTIONS = {"spending", "remittance_status", "salary_status", "dps_status", "loan_status",
                 "convert", "summary"}


def _summary(f, p) -> str:
    out = [_spending(f, {"period": "month"}), _remittance_status(f, p), _salary_status(f, p)]
    if f.dps_list():
        out.append(_dps_status(f, p))
    if f.loan_list():
        out.append(_loan_status(f, p))
    return " ".join(out)


def _delete_last(f, p) -> str:
    e = f.last_expense()
    if not e:
        return "There are no expenses to remove."
    f.delete_expense(e["id"])
    return f"Removed the last expense ({e['category']}, {e['spent_on']})."


ACTIONS = {
    "add_expense": _add_expense,
    "delete_last_expense": _delete_last,
    "spending": _spending,
    "add_remittance": _add_remittance,
    "remittance_status": _remittance_status,
    "set_salary": lambda f, p: (f.set_salary(p.get("amount"), p.get("pay_day"), p.get("currency")),
                                _salary_status(f, p))[1],
    "salary_status": _salary_status,
    "log_salary": _log_salary,
    "add_dps": lambda f, p: (f.add_dps(p.get("amount"), p.get("term_months"), p.get("date"), p.get("due_day"),
                                       p.get("maturity"), p.get("name") or "DPS", p.get("currency")),
                             "DPS saved. " + _dps_status(f, p))[1],
    "pay_dps": lambda f, p: (f.pay_dps(p.get("name"), p.get("amount"), p.get("date")),
                             "Marked the DPS instalment as paid. " + _dps_status(f, p))[1],
    "dps_status": _dps_status,
    "add_loan": lambda f, p: (f.add_loan(p.get("amount"), p.get("lender", ""), p.get("monthly"), p.get("due_day"),
                                         p.get("interest_pct"), p.get("date"), p.get("currency")),
                              "Loan saved. " + _loan_status(f, p))[1],
    "pay_loan": lambda f, p: (f.pay_loan(p.get("lender"), p.get("amount"), p.get("date")),
                              "Payment saved. " + _loan_status(f, p))[1],
    "loan_status": _loan_status,
    "convert": _convert,
    "rate": _set_rate,
    "settings": _settings,
    "summary": _summary,
    "open": _open,
}


# ── plugin interface ──────────────────────────────────────────────────────────

PLUGIN = {
    "name": "personal_finance",
    "description": (
        "The user's personal money records, kept privately on this PC: daily expenses, money sent "
        "home (remittances), salary day, DPS savings instalments and loans, in the home currency "
        "(default BDT/taka) and the working-country currency (default SAR/riyal). Use for 'I spent "
        "50 riyal on food', 'how much did I spend this month', 'I sent money home', 'when is my "
        "salary', 'salary received', 'DPS status', 'I paid the DPS', 'loan balance', 'I paid the "
        "loan', 'convert riyal to taka', 'open finance'. If the amount, currency or category is "
        "unclear, ASK the user instead of guessing. For stock/crypto prices use trading instead. "
        "Figures are the user's own records; never give financial advice."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description":
                       "add_expense | delete_last_expense | spending | add_remittance | remittance_status | "
                       "set_salary | salary_status | log_salary | add_dps | pay_dps | dps_status | add_loan | "
                       "pay_loan | loan_status | convert | rate | settings | summary | open | unlock | lock"},
            "text": {"type": "STRING", "description": "add_expense: the user's words, e.g. 'spent 50 riyal on food'"},
            "amount": {"type": "STRING", "description": "amount as said, e.g. '50' or '1250.5'"},
            "currency": {"type": "STRING", "description": "currency code or word: SAR, BDT, riyal, taka…"},
            "to_currency": {"type": "STRING", "description": "convert/rate: target currency"},
            "category": {"type": "STRING", "description": "add_expense: food, transport, rent, phone, bills, shopping, health, family, education, fun, other"},
            "note": {"type": "STRING", "description": "optional short note"},
            "date": {"type": "STRING", "description": "YYYY-MM-DD, 'today' or 'yesterday' (default today)"},
            "period": {"type": "STRING", "description": "spending: today | yesterday | week | month | last month"},
            "fee": {"type": "STRING", "description": "add_remittance: transfer fee"},
            "rate": {"type": "STRING", "description": "add_remittance: rate used; rate action: manual rate, or 'auto'"},
            "received": {"type": "STRING", "description": "add_remittance: amount received at home"},
            "recipient": {"type": "STRING", "description": "add_remittance: optional, who received it"},
            "method": {"type": "STRING", "description": "add_remittance: optional, bank/bKash/exchange house"},
            "pay_day": {"type": "STRING", "description": "set_salary: day of month 1-31"},
            "term_months": {"type": "STRING", "description": "add_dps: term in months"},
            "due_day": {"type": "STRING", "description": "add_dps/add_loan: day of month the payment is due"},
            "maturity": {"type": "STRING", "description": "add_dps: maturity amount, if known"},
            "name": {"type": "STRING", "description": "add_dps/pay_dps: which DPS"},
            "lender": {"type": "STRING", "description": "add_loan/pay_loan: lender (optional)"},
            "monthly": {"type": "STRING", "description": "add_loan: monthly payment"},
            "interest_pct": {"type": "STRING", "description": "add_loan: yearly interest %, optional"},
            "home_country": {"type": "STRING", "description": "settings: home country"},
            "work_country": {"type": "STRING", "description": "settings: working country"},
            "alert_days_before": {"type": "STRING", "description": "settings: reminder days before due dates"},
            "hide_amounts": {"type": "STRING", "description": "settings: true/false, mask figures on screen"},
            "pin": {"type": "STRING", "description": "unlock: the user's finance PIN"},
            "tab": {"type": "STRING", "description": "open: expenses | remittances | salary | dps | loans | settings"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None) -> str:
    global _player
    if player is not None:
        _player = player
        _start_reminders(player)
    p = {k: v for k, v in (parameters or {}).items() if v not in (None, "")}
    action = str(p.get("action") or "summary").strip().lower()
    try:
        f = get_finance()
        if f.store.load_error and action not in ("open",):
            note = f" ({f.store.load_error})"
        else:
            note = ""
        if action == "unlock":
            return ("Finance unlocked for 10 minutes." if f.check_pin(p.get("pin", ""))
                    else "That PIN is wrong.")
        if action == "lock":
            f.lock()
            return "Finance locked."
        fn = ACTIONS.get(action)
        if fn is None:
            return f"Unknown finance action '{action}'."
        if action in _READ_ACTIONS:
            msg = _locked_reply(f)
            if msg:
                return msg
        print(f"[Finance] {action}")       # event type only, never figures
        return fn(f, p) + note
    except FinanceError as e:
        return str(e)
    except Exception as e:
        print(f"[Finance] {action} failed: {type(e).__name__}")
        return f"The finance tracker hit a problem ({type(e).__name__}). Nothing was changed."


# ── reminders and the sidebar card ────────────────────────────────────────────

def _start_reminders(player) -> None:
    global _watcher
    if _watcher is not None:
        return

    def notify(msg: str) -> None:
        try:
            say = getattr(player, "request_say", None)
            if callable(say):
                say(f"[FINANCE REMINDER] Tell the user briefly, in their language: {msg}")
            player.write_log(f"SYS: 🔔 {msg}")
        except Exception:
            pass

    try:
        _watcher = fcore.AlertWatcher(get_finance(), notify)
        _watcher.start()
    except Exception as e:
        print(f"[Finance] reminders unavailable: {type(e).__name__}")


def sidebar_card():
    """Optional plugin hook (core's sidebar_card()): the white FINANCE card."""
    global _card_added
    if _card_added:
        return None
    _card_added = True
    return _load_sibling("_finance_ui").FinanceCard(get_finance())


def setup_ui(player) -> None:
    """Runs once on the Qt thread. Starts the reminders and, on a Habibi without
    the sidebar_card() hook, puts the FINANCE card under the media player."""
    global _player, _card_added
    _player = player
    _start_reminders(player)
    if hasattr(player, "add_sidebar_card") or _card_added:
        return                         # core adds the card through sidebar_card()
    anchor = getattr(player, "media_card", None)
    lay = anchor.parentWidget().layout() if anchor is not None and anchor.parentWidget() else None
    if lay is None:
        print("[Finance] no sidebar to put the card in; voice commands still work")
        return
    try:
        card = sidebar_card()
        if card is not None:
            lay.insertWidget(lay.indexOf(anchor) + 1, card)
    except Exception as e:
        print(f"[Finance] card failed: {type(e).__name__}: {e}")
