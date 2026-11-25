"""
plugins/trading.py — Habibi's trading tools, as a drop-in plugin.

"What's bitcoin at?", "how's the market today?", "sentiment on Tesla",
"where will gold go next week?", "long-term forecast for Apple",
"add Nvidia to my watchlist", "tell me when BTC goes above 120000".

Data, sentiment and forecasts come from plugins/_market_engine.py (free
sources, no API keys). The MARKETS card in the sidebar (plugins/_trading_ui.py,
shown through the plugin sidebar_card() hook) follows whatever is asked here.
Turn the whole feature off in the Plugin Manager like any other plugin.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


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


M = _load_sibling("_market_engine")


def _log(msg: str, player=None) -> None:
    print(f"[Market] {msg}")
    if player:
        try:
            player.write_log(f"HABIBI: {msg}")
        except Exception:
            pass


_hooked = False


def _hook_alerts(player) -> None:
    """Price alerts speak through the live session and show in the log."""
    global _hooked
    if _hooked or player is None:
        return
    _hooked = True

    def say(t):
        fn = getattr(player, "request_say", None)
        if callable(fn):
            fn(f"[MARKET ALERT] Tell the user briefly, in their language: {t}")

    M.add_notifier(say)
    M.add_notifier(lambda t: player.write_log(f"SYS: 🔔 {t}"))
    if M.alerts():
        M.start_alert_watcher()


def _sym(parameters: dict) -> str | None:
    raw = (parameters.get("symbol") or parameters.get("query") or "").strip()
    return M.resolve(raw) if raw else None


def run(parameters: dict, player=None) -> str:
    try:
        return _run(parameters or {}, player)
    except Exception as e:
        return f"Sir, the trading tools failed: {e}"


def _run(parameters: dict, player=None) -> str:
    action = (parameters.get("action") or "quote").strip().lower()
    _hook_alerts(player)

    if action in ("overview", "market", "summary"):
        o = M.overview()
        lines = []
        for g, qs in o["groups"].items():
            if qs:
                lines.append(g + ": " + ", ".join(
                    f"{q.name} {M.fmt_price(q.price, q.currency)} ({q.change_pct:+.2f}%)" for q in qs))
        if o["vix"]:
            lines.append(f"VIX fear gauge {o['vix'].price:.1f}")
        if o["fear_greed"]:
            fg = o["fear_greed"]
            lines.append(f"Crypto Fear & Greed {fg['value']} ({fg['label']})")
        M.emit("show", {"view": "watchlist"})
        return "Market overview — " + ". ".join(lines) + "." if lines else \
            "I couldn't reach the market data right now. Check the internet connection."

    if action in ("watchlist", "list"):
        wl = M.watchlist()
        qs = M.get_quotes(wl)
        M.emit("show", {"view": "watchlist"})
        if not qs:
            return "Your watchlist: " + ", ".join(wl) + ". (Prices unavailable right now.)"
        return "Watchlist — " + "; ".join(
            f"{qs[s].name} {M.fmt_price(qs[s].price, qs[s].currency)} ({qs[s].change_pct:+.2f}%)"
            for s in wl if s in qs) + "."

    if action == "alerts":
        al = M.alerts()
        if not al:
            return "No price alerts are set."
        return "Price alerts: " + "; ".join(
            f"{a['symbol']} {a['direction']} {M.fmt_price(a['price'])}" for a in al) + "."

    if action == "clear_alerts":
        s = _sym(parameters)
        n = M.clear_alerts(s)
        return f"Removed {n} price alert{'s' if n != 1 else ''}."

    sym = _sym(parameters)
    if not sym:
        return "Which stock, coin, index or currency? Say a name like Bitcoin, Apple or gold."

    if action in ("add", "watch"):
        added = M.add_to_watchlist(sym)
        return f"Added {sym} to your watchlist." if added else f"{sym} is already on your watchlist."

    if action in ("remove", "unwatch"):
        ok = M.remove_from_watchlist(sym)
        return f"Removed {sym} from your watchlist." if ok else f"{sym} wasn't on your watchlist."

    if action == "alert":
        try:
            price = float(str(parameters.get("price")).replace(",", "").replace("$", ""))
        except Exception:
            return "At what price should I alert you?"
        a = M.add_alert(sym, price, parameters.get("direction"))
        return (f"Done. I'll tell you when {sym} goes {a['direction']} {M.fmt_price(price)}. "
                "I check every minute while Habibi is running.")

    q = M.get_quote(sym)
    if action == "quote":
        if not q:
            return f"I couldn't get a price for {sym}. Check the name or the internet connection."
        M.emit("show", {"view": "detail", "symbol": sym})
        return M.quote_text(q)

    name = q.name if q else sym
    if action == "news":
        news = M.get_news(sym, name, limit=8)
        if not news:
            return f"No recent headlines found for {name}."
        return f"Latest on {name}: " + " | ".join(n["title"] for n in news[:6]) + "."

    hist = M.get_history(sym, "1y")
    closes = hist[1] if hist else None

    if action == "sentiment":
        s = M.sentiment(sym, name, closes)
        M.emit("show", {"view": "detail", "symbol": sym})
        return M.sentiment_text(s, name)

    if action in ("forecast", "predict", "prediction", "outlook", "report", "analysis"):
        s = M.sentiment(sym, name, closes)
        f = M.forecast(sym, s)
        M.emit("show", {"view": "detail", "symbol": sym})
        head = M.quote_text(q) + " " if q and action in ("report", "analysis") else ""
        return head + M.sentiment_text(s, name) + " " + M.forecast_text(f)

    return f"Unknown market action '{action}'."


def sidebar_card():
    """Optional plugin hook: the MARKETS card for Habibi's left sidebar."""
    return _load_sibling("_trading_ui").MarketsCard(M)


PLUGIN = {
    "name": "trading",
    "description": (
        "Trading and market tools with live data for stocks, indices, crypto, forex and "
        "commodities: live price, market overview, news headlines, sentiment analysis, "
        "next-week and long-term (3 months to 5 years) price forecasts, a watchlist shown in "
        "the sidebar, and price alerts. Use for any question about a price, a market, a coin, "
        "a share, 'should I buy', predictions or trends. Not for weather or product prices (use web_search for those). Forecasts are estimates, not advice — "
        "always say so when giving one."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "quote (live price) | overview (whole market today) | news | sentiment | "
                    "forecast (next week + long term) | report (price + sentiment + forecast) | "
                    "watchlist | add | remove (watchlist) | alert (notify at a price) | "
                    "alerts (list) | clear_alerts"
                ),
            },
            "symbol": {
                "type": "STRING",
                "description": ("Name or ticker in English: 'bitcoin', 'AAPL', 'Tesla', 'gold', "
                                "'S&P 500', 'EURUSD=X', 'Aramco'."),
            },
            "price": {"type": "NUMBER", "description": "Alert price level (for action=alert)."},
            "direction": {"type": "STRING", "description": "above | below (for action=alert; optional)."},
        },
        "required": ["action"],
    },
}
