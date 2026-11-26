"""
Saved info for offline mode — the last news headlines and exchange rates,
saved while Habibi is online so they can still be read out offline.

  • Refreshes every 30 minutes in the background while the internet works
    (free sources, no API keys: DuckDuckGo news, open.er-api.com).
  • Tool `saved_info`: "what were the headlines", "convert 100 dollars to taka",
    "what's the riyal rate". Always says how old the data is.
  • Other plugins can share the store:
        cache = _load_sibling("offline_cache")      # or importlib by path
        cache.put("markets:BTC", {...});  cache.get("markets:BTC") -> (value, saved_at)
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

PLUGIN = {
    "name": "saved_info",
    "description": (
        "Saved news headlines and currency exchange rates, kept for when Habibi is "
        "offline (they may be hours old — say how old). Use when offline and the user "
        "asks for the news/headlines, an exchange rate, or to convert money between "
        "currencies. kind: 'news' or 'rates'. For rates give currency codes, e.g. "
        "from_currency 'USD', to_currency 'BDT', and an optional amount."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "kind": {"type": "STRING", "description": "news | rates"},
            "from_currency": {"type": "STRING", "description": "ISO code, e.g. USD"},
            "to_currency": {"type": "STRING", "description": "ISO code, e.g. BDT"},
            "amount": {"type": "NUMBER", "description": "Amount to convert (default 1)"},
        },
        "required": [],
    },
}

REFRESH_EVERY = 30 * 60
RATES_URL = "https://open.er-api.com/v6/latest/USD"
NEWS_COUNT = 10


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CACHE_FILE = _base_dir() / "memory" / "offline_cache.json"
_lock = threading.Lock()


# ── shared store ────────────────────────────────────────────────────────────

def _read() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def put(key: str, value) -> None:
    """Save a JSON-able value under key, stamped with the time."""
    with _lock:
        data = _read()
        data[key] = {"saved_at": time.time(), "value": value}
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CACHE_FILE)
        except Exception as e:
            print(f"[SavedInfo] could not save: {e}")


def get(key: str):
    """(value, saved_at) or (None, None)."""
    with _lock:
        item = _read().get(key)
    if not isinstance(item, dict):
        return None, None
    return item.get("value"), item.get("saved_at")


def age_text(saved_at) -> str:
    if not saved_at:
        return "never saved"
    mins = int((time.time() - float(saved_at)) // 60)
    if mins < 2:
        return "saved just now"
    if mins < 60:
        return f"saved {mins} minutes ago"
    hours = mins // 60
    if hours < 48:
        return f"saved {hours} hour{'s' if hours != 1 else ''} ago"
    return f"saved {hours // 24} days ago"


# ── refreshing while online ─────────────────────────────────────────────────

def refresh_rates() -> bool:
    try:
        import requests
        d = requests.get(RATES_URL, timeout=10).json()
        rates = d.get("rates") if d.get("result") == "success" else None
        if isinstance(rates, dict) and rates:
            put("rates:USD", {k: float(v) for k, v in rates.items()})
            return True
    except Exception as e:
        print(f"[SavedInfo] rates refresh failed: {e}")
    return False


def refresh_news() -> bool:
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            items = list(ddgs.news("top news today", max_results=NEWS_COUNT))
        heads = [{"title": i.get("title", ""), "source": i.get("source", "")}
                 for i in items if i.get("title")]
        if heads:
            put("news:top", heads)
            return True
    except Exception as e:
        print(f"[SavedInfo] news refresh failed: {e}")
    return False


def _online() -> bool:
    try:
        import requests
        requests.head("https://open.er-api.com/", timeout=4)
        return True
    except Exception:
        return False


_started = False


def _refresher() -> None:
    time.sleep(20)                      # let Habibi finish starting up
    while True:
        try:
            if _online():
                refresh_rates()
                refresh_news()
        except Exception:
            pass
        time.sleep(REFRESH_EVERY)


def start_refresher() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_refresher, name="saved-info-refresh", daemon=True).start()


def setup_ui(player) -> None:
    """Runs once when Habibi's window is up: start the background refresh."""
    start_refresher()


# ── tool ────────────────────────────────────────────────────────────────────

def _fmt(x: float) -> str:
    return f"{x:,.2f}" if x >= 1 else f"{x:.4f}"


def _rates_answer(p: dict) -> str:
    rates, saved = get("rates:USD")
    if not rates:
        return "I don't have any saved exchange rates yet — they're saved while I'm online."
    src = str(p.get("from_currency") or "USD").upper().strip()
    dst = str(p.get("to_currency") or "").upper().strip()
    try:
        amount = float(p.get("amount") or 1)
    except (TypeError, ValueError):
        amount = 1.0
    if src not in rates or (dst and dst not in rates):
        bad = src if src not in rates else dst
        return f"I don't have a saved rate for {bad}."
    if not dst:
        dst = "USD" if src != "USD" else "EUR"
    value = amount / rates[src] * rates[dst]
    return (f"{_fmt(amount)} {src} is about {_fmt(value)} {dst} "
            f"(rate {age_text(saved)}, may have changed).")


def _news_answer() -> str:
    heads, saved = get("news:top")
    if not heads:
        return "I don't have any saved headlines yet — they're saved while I'm online."
    lines = [f"{i + 1}. {h['title']}" + (f" ({h['source']})" if h.get("source") else "")
             for i, h in enumerate(heads[:6])]
    return f"Headlines {age_text(saved)}:\n" + "\n".join(lines)


def run(parameters: dict, player=None, session_memory=None) -> str:
    p = parameters or {}
    kind = str(p.get("kind") or "").lower().strip()
    if not kind:
        kind = "rates" if (p.get("from_currency") or p.get("to_currency")) else "news"
    if kind.startswith("rate") or kind in ("currency", "convert", "exchange"):
        return _rates_answer(p)
    return _news_answer()
