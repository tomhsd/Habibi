"""
Self-test for the trading plugin (plugins/trading.py).

    python packaging/market_selftest.py            # live: real Yahoo / news / F&G
    python packaging/market_selftest.py --offline  # synthetic data, no network

Checks: the plugin loads through the real plugin loader, symbols resolve,
quotes / history / news / sentiment / forecasts come back sane, the voice
commands answer, watchlist + alerts persist and fire, and (if PyQt6 is
installed) the MARKETS card builds and renders. Writes a screenshot of the card
to packaging/markets-card.png.
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import tempfile
import time
import traceback
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
OFFLINE = "--offline" in sys.argv

results: list[tuple[str, bool, str]] = []


def check(name, fn):
    try:
        ok, info = fn()
    except Exception as e:
        ok, info = False, f"{type(e).__name__}: {e} | {traceback.format_exc()[-500:]}"
    results.append((name, ok, str(info)[:300]))
    print(("PASS " if ok else "FAIL ") + name + " — " + str(info)[:300].replace("\n", " | "), flush=True)
    return ok


# ── load the plugin exactly as Habibi does ───────────────────────────────────
from core.plugin_loader import discover_plugins  # noqa: E402

reg = discover_plugins(ROOT / "plugins", core_tool_names=set(), logger=lambda m: print("[loader]", m))
check("plugin discovered + valid", lambda: (reg.has("trading"), "trading in registry"))
check("sidebar card hook exposed",
      lambda: (any(n == "trading" for n, _ in reg.sidebar_cards()), reg.sidebar_cards()))
M = sys.modules["plugins._market_engine"]
P = sys.modules["plugins.trading"]
M.STORE_FILE = Path(tempfile.mkdtemp()) / "market.json"      # don't touch the real config


# ── synthetic network for --offline ──────────────────────────────────────────
def _fake_series(seed, n, start, drift, vol):
    rnd = random.Random(seed)
    out = [start]
    for _ in range(n - 1):
        out.append(out[-1] * math.exp(drift + vol * rnd.gauss(0, 1)))
    return out


def fake_get(url, ttl, as_json=True, timeout=10.0):
    now = int(time.time())
    if "/v8/finance/chart/" in url:
        sym = url.split("/chart/")[1].split("?")[0].replace("%5E", "^").replace("%3D", "=")
        seed = sum(map(ord, sym))
        start = 30000 if "BTC" in sym else 18 if "VIX" in sym else 150
        if "interval=1d" in url:
            n = 1250 if "range=5y" in url else 500
            c = _fake_series(seed, n, start, 0.0004, 0.02 if "BTC" in sym else 0.012)
            ts = [now - (n - i) * 86400 for i in range(n)]
        else:
            c = _fake_series(seed + 1, 78, start * 1.1, 0.0, 0.002)
            ts = [now - (78 - i) * 300 for i in range(78)]
        return {"chart": {"result": [{"meta": {
            "symbol": sym, "currency": "USD", "regularMarketPrice": c[-1],
            "chartPreviousClose": c[0], "regularMarketDayHigh": max(c[-78:]),
            "regularMarketDayLow": min(c[-78:]), "fiftyTwoWeekHigh": max(c) * 1.1,
            "fiftyTwoWeekLow": min(c) * 0.9, "shortName": f"{sym} Inc", "exchangeName": "NMS",
            "regularMarketTime": now}, "timestamp": ts,
            "indicators": {"quote": [{"close": c}]}}], "error": None}}
    if "/v1/finance/search" in url:
        hit = {"boeing": "BA", "tesla": "TSLA", "nvidia": "NVDA"}.get(url.split("q=")[1].split("&")[0].lower())
        return {"quotes": [{"symbol": hit, "quoteType": "EQUITY"}] if hit else []}
    if "alternative.me" in url:
        return {"data": [{"value": "71", "value_classification": "Greed"}]}
    if "rss" in url:
        heads = ["Shares surge after record quarterly profit beats estimates",
                 "Analysts upgrade stock on strong demand",
                 "Regulators open probe into accounting, shares fall",
                 "Company to hold annual meeting in March",
                 "Stock rallies as growth outlook improves"]
        items = "".join(f"<item><title>{h}</title><link>https://example.com/{i}</link>"
                        f"<pubDate>Tue, 25 Nov 2026 10:00:00 GMT</pubDate></item>"
                        for i, h in enumerate(heads))
        return f"<?xml version='1.0'?><rss><channel>{items}</channel></rss>"
    return None


if OFFLINE:
    M._get = fake_get


# ── engine ───────────────────────────────────────────────────────────────────
def t_resolve():
    got = {q: M.resolve(q) for q in ("bitcoin", "Apple stock", "gold", "S&P 500", "TSLA",
                                     "EURUSD=X", "Boeing")}
    exp = {"bitcoin": "BTC-USD", "Apple stock": "AAPL", "gold": "GC=F", "S&P 500": "^GSPC",
           "TSLA": "TSLA", "EURUSD=X": "EURUSD=X", "Boeing": "BA"}
    return got == exp, got


check("resolve names → tickers", t_resolve)


def t_quote(sym):
    def f():
        q = M.get_quote(sym)
        return bool(q and q.price > 0 and abs(q.change_pct) < 60), (M.quote_text(q) if q else None)
    return f


for s in ("AAPL", "BTC-USD", "^GSPC", "GC=F", "EURUSD=X"):
    check(f"live quote {s}", t_quote(s))


def t_hist():
    h = M.get_history("AAPL", "2y")
    return bool(h and len(h[1]) > 300 and h[0][-1] > time.time() - 10 * 86400), \
        f"{len(h[1]) if h else 0} daily closes"


check("daily history", t_hist)


def t_news():
    n = M.get_news("AAPL", "Apple")
    return len(n) >= 3, [x["title"][:60] for x in n[:3]]


check("news headlines", t_news)


def t_score():
    a = M.score_text("Shares surge to record high after earnings beat")
    b = M.score_text("Stock plunges after fraud probe, analysts downgrade")
    c = M.score_text("Company does not expect losses")
    return a > 0.3 and b < -0.3 and c > -0.5, (round(a, 2), round(b, 2), round(c, 2))


check("headline scoring", t_score)


def t_sent():
    h = M.get_history("BTC-USD", "1y")
    s = M.sentiment("BTC-USD", "Bitcoin", h[1] if h else None)
    return -1 <= s["score"] <= 1 and s["label"], M.sentiment_text(s, "Bitcoin")


check("sentiment", t_sent)


def t_fc(sym):
    def f():
        fc = M.forecast(sym)
        if fc.get("error"):
            return False, fc["error"]
        w = fc["week"]
        ok = (w["low_95"] < w["low_68"] < w["expected"] < w["high_68"] < w["high_95"]
              and 0 <= w["prob_up"] <= 1 and len(fc["long_term"]) == 6
              and all(x["low_68"] < x["median"] < x["high_68"] for x in fc["long_term"])
              and abs(w["change_pct"]) < 25)
        return ok, M.forecast_text(fc)
    return f


for s in ("AAPL", "BTC-USD", "GC=F"):
    check(f"forecast {s}", t_fc(s))


# ── voice commands ───────────────────────────────────────────────────────────
class FakePlayer:
    def __init__(self):
        self.logs, self.said = [], []

    def write_log(self, t):
        self.logs.append(t)

    def request_say(self, t):
        self.said.append(t)


pl = FakePlayer()
for params, must in (({"action": "quote", "symbol": "bitcoin"}, "%"),
                     ({"action": "overview"}, "Market overview"),
                     ({"action": "sentiment", "symbol": "Tesla"}, "Sentiment"),
                     ({"action": "forecast", "symbol": "gold"}, "not financial advice"),
                     ({"action": "report", "symbol": "AAPL"}, "Long term"),
                     ({"action": "news", "symbol": "NVDA"}, ""),
                     ({"action": "add", "symbol": "nvidia"}, "NVDA"),
                     ({"action": "watchlist"}, "Watchlist"),
                     ({"action": "remove", "symbol": "NVDA"}, "Removed"),
                     ({"action": "quote"}, "Which")):
    check(f"voice: {params}", lambda p=params, m=must: (
        (lambda r: (m in r, r))(P.run(dict(p), player=pl))))


def t_alert():
    q = M.get_quote("AAPL")
    P.run({"action": "alert", "symbol": "AAPL", "price": q.price * 0.5, "direction": "above"}, player=pl)
    P.run({"action": "alert", "symbol": "AAPL", "price": q.price * 2}, player=pl)
    saved = json.loads(M.STORE_FILE.read_text())["alerts"]
    M.check_alerts_once()                 # (the background watcher may have fired it already)
    left = M.alerts()
    ok = (len(saved) >= 1 and len(left) == 1
          and left[0]["direction"] == "above" and any("Price alert" in s for s in pl.said))
    M.clear_alerts()
    return ok, f"left={left} said={pl.said[-1:]}"


check("price alerts fire + speak", t_alert)


# ── sidebar card ─────────────────────────────────────────────────────────────
def t_card():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QEventLoop, QTimer
    app = QApplication.instance() or QApplication(sys.argv)
    card = dict(reg.sidebar_cards())["trading"]()
    card.setFixedWidth(290)
    card.resize(290, 900)
    card.show()

    def spin(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    card.refresh()
    for _ in range(60):
        spin(250)
        if all(r.price.text() not in ("…", "") for r in card.rows.values()):
            break
    M.emit("show", {"view": "detail", "symbol": "BTC-USD"})
    for _ in range(80):
        spin(250)
        if card.l_lbl.text():
            break
    card.resize(290, card.sizeHint().height())
    spin(300)
    shot = ROOT / "packaging" / "markets-card.png"
    card.grab().save(str(shot))
    prices = {s: r.price.text() for s, r in card.rows.items()}
    ok = bool(card.l_lbl.text()) and all(v not in ("…", "n/a") for v in prices.values())
    return ok, f"rows={prices} detail={'yes' if card.l_lbl.text() else 'no'} → {shot.name}"


try:
    import PyQt6  # noqa: F401
    check("MARKETS card builds + renders", t_card)
except ImportError:
    print("SKIP card test (PyQt6 not installed)")

bad = [r for r in results if not r[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (" (offline)" if OFFLINE else " (live)"))
sys.exit(1 if bad else 0)
