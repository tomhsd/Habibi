"""
plugins/_market_engine.py — Habibi's market engine: live prices, news sentiment and
forecasts for stocks, indices, crypto, forex and commodities.

Free sources only, no API keys:
  • Yahoo Finance chart + search API   prices, intraday and daily history
  • Yahoo Finance RSS, Google News RSS headlines for sentiment
  • alternative.me                     crypto Fear & Greed index
  • CoinGecko / Stooq                  fallbacks when Yahoo is unreachable

Forecasts are statistical estimates built from the price history (trend,
momentum, volatility) nudged by the sentiment score. They are NOT financial
advice: every forecast carries its own backtested hit rate so the user can see
how often the method has been right on that instrument.

Used by plugins/trading.py (voice) and plugins/_trading_ui.py (sidebar card).
The leading underscore keeps the plugin loader from treating it as a plugin.
Pure Python + requests; every network call is cached and never raises out of
the public functions — they return None / [] / an "error" key instead.
"""
from __future__ import annotations

import json
import math
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus

try:
    import requests
except Exception:                                  # pragma: no cover
    requests = None

DISCLAIMER = "Estimates from past prices and news, not financial advice."

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


STORE_FILE = _base_dir() / "config" / "market.json"
DEFAULT_WATCHLIST = ["BTC-USD", "ETH-USD", "^GSPC", "^IXIC", "GC=F", "AAPL"]


# ══════════════════════════════════════════════════════════════════════════════
#  Names → symbols
# ══════════════════════════════════════════════════════════════════════════════

ALIASES = {
    # crypto
    "bitcoin": "BTC-USD", "btc": "BTC-USD", "ethereum": "ETH-USD", "eth": "ETH-USD",
    "ether": "ETH-USD", "solana": "SOL-USD", "sol": "SOL-USD", "xrp": "XRP-USD",
    "ripple": "XRP-USD", "dogecoin": "DOGE-USD", "doge": "DOGE-USD", "bnb": "BNB-USD",
    "binance coin": "BNB-USD", "cardano": "ADA-USD", "ada": "ADA-USD", "tron": "TRX-USD",
    "toncoin": "TON11419-USD", "ton": "TON11419-USD", "litecoin": "LTC-USD", "ltc": "LTC-USD",
    "shiba": "SHIB-USD", "shiba inu": "SHIB-USD", "polkadot": "DOT-USD", "avalanche": "AVAX-USD",
    "chainlink": "LINK-USD", "pepe": "PEPE24478-USD", "usdt": "USDT-USD", "tether": "USDT-USD",
    # indices
    "s&p 500": "^GSPC", "s and p 500": "^GSPC", "sp500": "^GSPC", "s&p": "^GSPC",
    "spx": "^GSPC", "nasdaq": "^IXIC", "nasdaq 100": "^NDX", "dow": "^DJI",
    "dow jones": "^DJI", "russell 2000": "^RUT", "vix": "^VIX", "ftse": "^FTSE",
    "ftse 100": "^FTSE", "dax": "^GDAXI", "nikkei": "^N225", "hang seng": "^HSI",
    "sensex": "^BSESN", "nifty": "^NSEI", "nifty 50": "^NSEI", "tasi": "^TASI.SR",
    "saudi market": "^TASI.SR", "tadawul": "^TASI.SR", "cac 40": "^FCHI",
    "dse": "^DSEX", "dsex": "^DSEX",
    # commodities
    "gold": "GC=F", "silver": "SI=F", "oil": "CL=F", "crude": "CL=F", "crude oil": "CL=F",
    "wti": "CL=F", "brent": "BZ=F", "natural gas": "NG=F", "gas": "NG=F", "copper": "HG=F",
    "platinum": "PL=F", "wheat": "ZW=F", "corn": "ZC=F",
    # forex
    "dollar index": "DX-Y.NYB", "dxy": "DX-Y.NYB", "euro": "EURUSD=X", "eur usd": "EURUSD=X",
    "eurusd": "EURUSD=X", "pound": "GBPUSD=X", "gbp usd": "GBPUSD=X", "yen": "JPY=X",
    "usd jpy": "JPY=X", "usd bdt": "BDT=X", "taka": "BDT=X", "dollar to taka": "BDT=X",
    "usd sar": "SAR=X", "riyal": "SAR=X", "usd inr": "INR=X", "rupee": "INR=X",
    # popular stocks
    "apple": "AAPL", "microsoft": "MSFT", "google": "GOOGL", "alphabet": "GOOGL",
    "amazon": "AMZN", "meta": "META", "facebook": "META", "tesla": "TSLA", "nvidia": "NVDA",
    "netflix": "NFLX", "amd": "AMD", "intel": "INTC", "aramco": "2222.SR",
    "saudi aramco": "2222.SR", "berkshire": "BRK-B", "coinbase": "COIN",
    "microstrategy": "MSTR", "strategy": "MSTR", "palantir": "PLTR", "broadcom": "AVGO",
    "tsmc": "TSM", "samsung": "005930.KS", "spy": "SPY", "qqq": "QQQ",
}

COINGECKO_IDS = {
    "BTC-USD": "bitcoin", "ETH-USD": "ethereum", "SOL-USD": "solana", "XRP-USD": "ripple",
    "DOGE-USD": "dogecoin", "BNB-USD": "binancecoin", "ADA-USD": "cardano", "TRX-USD": "tron",
    "LTC-USD": "litecoin", "SHIB-USD": "shiba-inu", "DOT-USD": "polkadot",
    "AVAX-USD": "avalanche-2", "LINK-USD": "chainlink", "USDT-USD": "tether",
    "TON11419-USD": "the-open-network",
}

_SYMBOL_RE = re.compile(r"^[\^A-Z0-9][A-Z0-9.\-=^]{0,19}$")


def kind_of(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith("-USD") or s.endswith("-USDT") or s in COINGECKO_IDS:
        return "crypto"
    if s.startswith("^"):
        return "index"
    if s.endswith("=X") or s == "DX-Y.NYB":
        return "forex"
    if s.endswith("=F"):
        return "commodity"
    return "stock"


# ══════════════════════════════════════════════════════════════════════════════
#  HTTP + cache
# ══════════════════════════════════════════════════════════════════════════════

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()
_session = None


def _http():
    global _session
    if _session is None and requests is not None:
        s = requests.Session()
        s.headers.update({"User-Agent": _UA, "Accept": "*/*",
                          "Accept-Language": "en-US,en;q=0.9"})
        _session = s
    return _session


def _get(url: str, ttl: float, as_json: bool = True, timeout: float = 10.0):
    """GET with an in-memory cache. Returns None on any failure."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    s = _http()
    if s is None:
        return None
    try:
        r = s.get(url, timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json() if as_json else r.text
    except Exception:
        return None
    with _cache_lock:
        _cache[url] = (now, data)
        if len(_cache) > 400:                         # keep it small
            for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:100]:
                _cache.pop(k, None)
    return data


# ══════════════════════════════════════════════════════════════════════════════
#  Resolve
# ══════════════════════════════════════════════════════════════════════════════

def resolve(query: str) -> str | None:
    """'bitcoin' → 'BTC-USD', 'apple stock' → 'AAPL', 'TSLA' → 'TSLA'."""
    if not query:
        return None
    q = query.strip()
    low = re.sub(r"\b(stock|stocks|share|shares|price|coin|crypto|index|of|the|today)\b",
                 " ", q.lower())
    low = re.sub(r"\s+", " ", low).strip(" ?.!,")
    if low in ALIASES:
        return ALIASES[low]
    up = q.strip().upper().replace(" ", "")
    if _SYMBOL_RE.match(up) and (any(c in up for c in "^=.-") or q.strip() == up):
        return up                                    # clearly a ticker: ^GSPC, GC=F, BTC-USD, TSLA
    data = _get("https://query2.finance.yahoo.com/v1/finance/search?q="
                f"{quote_plus(low or q)}&quotesCount=6&newsCount=0", ttl=86400)
    try:
        quotes = [x for x in data.get("quotes", []) if x.get("symbol")]
    except Exception:
        quotes = []
    if any(str(x["symbol"]).upper() == up for x in quotes):
        return up
    if quotes:                                       # Yahoo's own relevance order
        return quotes[0]["symbol"].upper()
    if _SYMBOL_RE.match(up) and len(up) <= 6:
        return up                                    # search offline: trust a short ticker
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  Prices
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Quote:
    symbol: str
    name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    currency: str = "USD"
    day_high: float | None = None
    day_low: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    exchange: str = ""
    kind: str = "stock"
    time: float = 0.0                  # unix seconds of the last price
    intraday: list = field(default_factory=list)
    source: str = "yahoo"

    def to_dict(self) -> dict:
        return asdict(self)


def _chart(symbol: str, rng: str, interval: str, ttl: float) -> dict | None:
    for host in ("query1", "query2"):
        data = _get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
                    f"?range={rng}&interval={interval}&includePrePost=false", ttl=ttl)
        try:
            res = data["chart"]["result"][0]
            if res and res.get("meta"):
                return res
        except Exception:
            continue
    return None


def _closes(res: dict) -> tuple[list[float], list[float]]:
    try:
        ts = res.get("timestamp") or []
        cl = res["indicators"]["quote"][0].get("close") or []
    except Exception:
        return [], []
    t_out, c_out = [], []
    for t, c in zip(ts, cl):
        if c is not None and c == c and c > 0:
            t_out.append(float(t)); c_out.append(float(c))
    return t_out, c_out


def get_quote(symbol: str) -> Quote | None:
    """Live price (Yahoo refreshes every few seconds; we cache 15 s)."""
    symbol = symbol.upper()
    res = _chart(symbol, "1d", "5m", ttl=15)
    if res:
        m = res["meta"]
        _, intr = _closes(res)
        price = float(m.get("regularMarketPrice") or (intr[-1] if intr else 0) or 0)
        prev = float(m.get("chartPreviousClose") or m.get("previousClose") or 0)
        if price > 0:
            if not prev:
                prev = intr[0] if intr else price
            q = Quote(symbol=symbol,
                      name=m.get("shortName") or m.get("longName") or symbol,
                      price=price, prev_close=prev, change=price - prev,
                      change_pct=(price - prev) / prev * 100 if prev else 0.0,
                      currency=m.get("currency") or "USD",
                      day_high=m.get("regularMarketDayHigh"),
                      day_low=m.get("regularMarketDayLow"),
                      high_52w=m.get("fiftyTwoWeekHigh"), low_52w=m.get("fiftyTwoWeekLow"),
                      exchange=m.get("fullExchangeName") or m.get("exchangeName") or "",
                      kind=kind_of(symbol), time=float(m.get("regularMarketTime") or time.time()),
                      intraday=intr[-96:])
            return q
    # fallbacks
    if kind_of(symbol) == "crypto" and symbol in COINGECKO_IDS:
        cid = COINGECKO_IDS[symbol]
        d = _get("https://api.coingecko.com/api/v3/simple/price?ids="
                 f"{cid}&vs_currencies=usd&include_24hr_change=true", ttl=30)
        try:
            p = float(d[cid]["usd"]); ch = float(d[cid].get("usd_24h_change") or 0)
            prev = p / (1 + ch / 100) if ch > -100 else p
            return Quote(symbol=symbol, name=cid.replace("-", " ").title(), price=p,
                         prev_close=prev, change=p - prev, change_pct=ch, kind="crypto",
                         time=time.time(), source="coingecko")
        except Exception:
            pass
    hist = get_history(symbol, "1y")
    if hist and len(hist[1]) >= 2:
        t, c = hist
        return Quote(symbol=symbol, name=symbol, price=c[-1], prev_close=c[-2],
                     change=c[-1] - c[-2], change_pct=(c[-1] - c[-2]) / c[-2] * 100,
                     kind=kind_of(symbol), time=t[-1], source="daily")
    return None


def get_quotes(symbols: list[str]) -> dict[str, Quote]:
    """Several quotes in parallel (the sidebar's watchlist)."""
    out: dict[str, Quote] = {}
    lock = threading.Lock()

    def one(s):
        q = get_quote(s)
        if q:
            with lock:
                out[s.upper()] = q

    threads = [threading.Thread(target=one, args=(s,), daemon=True) for s in symbols[:20]]
    for th in threads:
        th.start()
    for th in threads:
        th.join(15)
    return out


def get_history(symbol: str, rng: str = "2y") -> tuple[list[float], list[float]] | None:
    """Daily closes: (unix_times, closes), oldest first."""
    symbol = symbol.upper()
    res = _chart(symbol, rng, "1d", ttl=600)
    if res:
        t, c = _closes(res)
        if len(c) >= 30:
            return t, c
    days = {"1mo": 31, "3mo": 92, "6mo": 183, "1y": 366, "2y": 731, "5y": 1826}.get(rng, 731)
    if kind_of(symbol) == "crypto" and symbol in COINGECKO_IDS:
        d = _get(f"https://api.coingecko.com/api/v3/coins/{COINGECKO_IDS[symbol]}/market_chart"
                 f"?vs_currency=usd&days={min(days, 365)}&interval=daily", ttl=900)
        try:
            pts = [(p[0] / 1000.0, float(p[1])) for p in d["prices"] if p[1]]
            if len(pts) >= 30:
                return [p[0] for p in pts], [p[1] for p in pts]
        except Exception:
            pass
    if kind_of(symbol) == "stock" and re.match(r"^[A-Z.\-]{1,6}$", symbol):
        txt = _get(f"https://stooq.com/q/d/l/?s={symbol.lower().replace('-', '.')}.us&i=d",
                   ttl=900, as_json=False)
        try:
            rows = [r.split(",") for r in txt.strip().splitlines()[1:]]
            pts = [(datetime.strptime(r[0], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp(),
                    float(r[4])) for r in rows if len(r) >= 5 and r[4] not in ("", "null")]
            pts = [p for p in pts if p[0] >= time.time() - days * 86400]
            if len(pts) >= 30:
                return [p[0] for p in pts], [p[1] for p in pts]
        except Exception:
            pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  News + sentiment
# ══════════════════════════════════════════════════════════════════════════════

_POS = {
    "beat", "beats", "surge", "surges", "surged", "soar", "soars", "soared", "rally", "rallies",
    "rallied", "jump", "jumps", "jumped", "gain", "gains", "gained", "rise", "rises", "rose",
    "climb", "climbs", "climbed", "record", "high", "highs", "upgrade", "upgrades", "upgraded",
    "outperform", "outperforms", "bullish", "bull", "strong", "stronger", "growth", "grow",
    "grows", "profit", "profits", "profitable", "boost", "boosts", "boosted", "buy", "buys",
    "approval", "approved", "approves", "win", "wins", "won", "optimism", "optimistic",
    "rebound", "rebounds", "recover", "recovers", "recovery", "breakthrough", "expands",
    "expansion", "upside", "inflows", "inflow", "accumulate", "accumulation", "adoption",
    "partnership", "launch", "launches", "raises", "raised", "tops", "exceeds", "exceeded",
    "positive", "momentum", "demand", "dividend", "buyback", "etf", "beats", "best", "lifts",
    "higher", "advance", "advances", "spikes", "booming", "boom", "robust", "resilient",
}
_NEG = {
    "miss", "misses", "missed", "plunge", "plunges", "plunged", "plummet", "plummets", "fall",
    "falls", "fell", "drop", "drops", "dropped", "slump", "slumps", "slide", "slides", "sink",
    "sinks", "sank", "tumble", "tumbles", "tumbled", "crash", "crashes", "crashed", "decline",
    "declines", "declined", "downgrade", "downgrades", "downgraded", "underperform", "bearish",
    "bear", "weak", "weaker", "weakness", "loss", "losses", "lose", "loses", "lawsuit", "sued",
    "sues", "probe", "investigation", "fraud", "scandal", "fine", "fined", "penalty", "ban",
    "bans", "banned", "hack", "hacked", "exploit", "layoffs", "layoff", "cuts", "cut", "warning",
    "warns", "warned", "recall", "risk", "risks", "fear", "fears", "concern", "concerns",
    "selloff", "sell-off", "outflows", "outflow", "liquidation", "liquidations", "bankruptcy",
    "default", "recession", "inflation", "tariff", "tariffs", "lower", "lowest", "low", "slows",
    "slowdown", "volatile", "volatility", "uncertainty", "delay", "delays", "halt", "halts",
    "shortfall", "negative", "dump", "dumps", "worst", "crisis", "collapse", "pressure",
}
_NEGATORS = {"not", "no", "never", "without", "fails", "failed", "despite"}


def score_text(text: str) -> float:
    """-1 … +1 for one headline, from a finance word list with simple negation."""
    words = re.findall(r"[a-z][a-z\-']*", text.lower())
    s, hits = 0.0, 0
    for i, w in enumerate(words):
        v = 1 if w in _POS else -1 if w in _NEG else 0
        if not v:
            continue
        if any(p in _NEGATORS for p in words[max(0, i - 3):i]):
            v = -v * 0.7
        s += v; hits += 1
    if not hits:
        return 0.0
    return max(-1.0, min(1.0, s / (hits + 1) * 1.4))


def _rss(url: str, ttl: float = 600) -> list[dict]:
    txt = _get(url, ttl=ttl, as_json=False)
    if not txt:
        return []
    try:
        root = ET.fromstring(txt.encode("utf-8") if isinstance(txt, str) else txt)
    except Exception:
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        src = it.find("source")
        out.append({"title": title, "link": (it.findtext("link") or "").strip(),
                    "published": (it.findtext("pubDate") or "").strip(),
                    "source": (src.text or "").strip() if src is not None and src.text else ""})
    return out


def get_news(symbol: str, name: str = "", limit: int = 15) -> list[dict]:
    symbol = symbol.upper()
    items = _rss("https://feeds.finance.yahoo.com/rss/2.0/headline?s="
                 f"{quote(symbol, safe='')}&region=US&lang=en-US")
    if len(items) < 5:
        q = name or symbol.replace("-USD", "").replace("^", "")
        extra = {"crypto": "crypto", "commodity": "price", "forex": "exchange rate",
                 "index": "market"}.get(kind_of(symbol), "stock")
        items += _rss("https://news.google.com/rss/search?q="
                      f"{quote_plus(q + ' ' + extra + ' when:7d')}&hl=en-US&gl=US&ceid=US:en")
    seen, out = set(), []
    for it in items:
        key = re.sub(r"\W+", "", it["title"].lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        it["score"] = round(score_text(it["title"]), 2)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def fear_greed() -> dict | None:
    """Crypto Fear & Greed index (0 = extreme fear, 100 = extreme greed)."""
    d = _get("https://api.alternative.me/fng/?limit=1", ttl=1800)
    try:
        x = d["data"][0]
        return {"value": int(x["value"]), "label": x["value_classification"]}
    except Exception:
        return None


def _label(score: float) -> str:
    if score >= 0.35: return "very bullish"
    if score >= 0.12: return "bullish"
    if score > -0.12: return "neutral"
    if score > -0.35: return "bearish"
    return "very bearish"


def sentiment(symbol: str, name: str = "", closes: list[float] | None = None) -> dict:
    """News tone + market mood (+ price action if closes given) → one score."""
    symbol = symbol.upper()
    news = get_news(symbol, name)
    scored = [n["score"] for n in news if n["score"] != 0]
    news_score = sum(scored) / len(scored) if scored else 0.0
    parts = {"news": round(news_score, 3)}
    weights = {"news": 0.5 if scored else 0.0}

    mood = None
    if kind_of(symbol) == "crypto":
        fg = fear_greed()
        if fg:
            mood = {"name": "Crypto Fear & Greed", "value": fg["value"], "label": fg["label"]}
            parts["mood"] = round((fg["value"] - 50) / 50, 3)
            weights["mood"] = 0.2
    elif kind_of(symbol) in ("stock", "index"):
        vq = get_quote("^VIX")
        if vq:
            v = vq.price
            lab = ("calm" if v < 15 else "normal" if v < 20 else "nervous" if v < 30 else "fearful")
            mood = {"name": "VIX fear gauge", "value": round(v, 1), "label": lab}
            parts["mood"] = round(max(-1, min(1, (18 - v) / 12)), 3)
            weights["mood"] = 0.2

    if closes and len(closes) >= 60:
        t = technicals(closes)
        parts["price_action"] = t["score"]
        weights["price_action"] = 0.3

    tot = sum(weights.values())
    score = sum(parts[k] * weights[k] for k in weights) / tot if tot else 0.0
    pos = sum(1 for s in scored if s > 0); neg = sum(1 for s in scored if s < 0)
    return {"symbol": symbol, "score": round(score, 3), "label": _label(score),
            "parts": parts, "mood": mood, "headlines": news[:8],
            "positive": pos, "negative": neg, "neutral": len(news) - pos - neg}


# ══════════════════════════════════════════════════════════════════════════════
#  Technicals + forecast
# ══════════════════════════════════════════════════════════════════════════════

def _sma(c: list[float], n: int) -> float | None:
    return sum(c[-n:]) / n if len(c) >= n else None


def _ema_series(c: list[float], n: int) -> list[float]:
    k = 2 / (n + 1); out = [c[0]]
    for x in c[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def _rsi(c: list[float], n: int = 14) -> float | None:
    if len(c) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def _logret(c: list[float]) -> list[float]:
    return [math.log(c[i] / c[i - 1]) for i in range(1, len(c)) if c[i - 1] > 0 and c[i] > 0]


def _std(x: list[float]) -> float:
    if len(x) < 2:
        return 0.0
    m = sum(x) / len(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))


def _slope(y: list[float]) -> float:
    n = len(y)
    if n < 3:
        return 0.0
    mx = (n - 1) / 2; my = sum(y) / n
    num = sum((i - mx) * (v - my) for i, v in enumerate(y))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


def technicals(c: list[float]) -> dict:
    """Trend / momentum signals and a combined -1…+1 score."""
    price = c[-1]
    s20, s50, s200 = _sma(c, 20), _sma(c, 50), _sma(c, 200)
    rsi = _rsi(c[-250:])
    e12, e26 = _ema_series(c[-200:], 12), _ema_series(c[-200:], 26)
    macd = [a - b for a, b in zip(e12, e26)]
    sig = _ema_series(macd, 9)
    hist = macd[-1] - sig[-1]
    mom20 = price / c[-21] - 1 if len(c) > 21 else 0.0
    vol = _std(_logret(c[-61:])) or 0.01

    sigs = []
    if s20: sigs.append(1 if price > s20 else -1)
    if s50: sigs.append(1 if price > s50 else -1)
    if s200: sigs.append(1 if price > s200 else -1)
    if s50 and s200: sigs.append(1 if s50 > s200 else -1)
    sigs.append(max(-1, min(1, hist / (price * vol * 0.5))))
    sigs.append(max(-1, min(1, mom20 / (vol * math.sqrt(20)))))
    score = sum(sigs) / len(sigs)
    if rsi is not None:                       # stretched → lean against the move
        if rsi > 75: score -= 0.25
        elif rsi < 25: score += 0.25
    return {"price": price, "sma20": s20, "sma50": s50, "sma200": s200,
            "rsi": round(rsi, 1) if rsi is not None else None,
            "macd_hist": hist, "momentum_20d": mom20, "daily_vol": vol,
            "score": round(max(-1, min(1, score)), 3),
            "trend": ("uptrend" if s50 and s200 and price > s50 > s200 else
                      "downtrend" if s50 and s200 and price < s50 < s200 else "sideways")}


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# long-run yearly drift we shrink towards (a sober prior per asset class)
_PRIOR = {"stock": 0.07, "index": 0.07, "crypto": 0.10, "commodity": 0.03, "forex": 0.0}
_CAP = {"stock": 0.35, "index": 0.20, "crypto": 0.60, "commodity": 0.20, "forex": 0.08}


def _week_drift(c: list[float], h: int, sent_score: float = 0.0) -> tuple[float, float]:
    """Expected log return and stdev over h periods, from closes `c`."""
    r = _logret(c[-121:])
    vol = _std(r[-60:]) or 0.01
    tr = _slope([math.log(x) for x in c[-20:]]) * 0.4     # damped short trend
    tech = technicals(c)["score"]
    mu = (tr + tech * vol * 0.08 + sent_score * vol * 0.06) * h
    return mu, vol * math.sqrt(h)


def backtest_week(c: list[float], h: int, lookback: int = 260) -> dict | None:
    """How often the next-week direction call was right over the last year."""
    n = len(c)
    start = max(200, n - lookback - h)
    hits = tries = inside = 0
    for i in range(start, n - h, h):
        mu, sd = _week_drift(c[:i + 1], h)
        actual = math.log(c[i + h] / c[i])
        if abs(mu) > 1e-9:
            tries += 1
            hits += (mu > 0) == (actual > 0)
        inside += abs(actual - mu) <= sd
    total = len(range(start, n - h, h))
    if total < 8 or tries < 5:
        return None
    ups = sum(1 for i in range(start, n - h, h) if c[i + h] > c[i])
    return {"direction_hit_rate": round(hits / tries, 2), "calls": tries,
            "range_hit_rate": round(inside / total, 2),
            "base_rate_up": round(ups / total, 2)}


def forecast(symbol: str, sent: dict | None = None) -> dict:
    """Next-week and long-term outlook. Every number is an estimate."""
    symbol = symbol.upper()
    hist = get_history(symbol, "5y") or get_history(symbol, "2y")
    if not hist or len(hist[1]) < 60:
        return {"symbol": symbol, "error": "Not enough price history for a forecast."}
    t, c = hist
    k = kind_of(symbol)
    q = get_quote(symbol)
    price = q.price if q else c[-1]
    if q and abs(price / c[-1] - 1) < 0.5:
        c = c[:-1] + [price] if (time.time() - t[-1]) < 86400 * 1.5 else c + [price]
    per_year = 365 if k == "crypto" else 252
    week_h = 7 if k == "crypto" else 5
    tech = technicals(c)
    sscore = (sent or {}).get("score", 0.0)

    mu_w, sd_w = _week_drift(c, week_h, sscore)
    week = {
        "horizon": "next 7 days" if k == "crypto" else "next 5 trading days",
        "expected": price * math.exp(mu_w),
        "low_68": price * math.exp(mu_w - sd_w), "high_68": price * math.exp(mu_w + sd_w),
        "low_95": price * math.exp(mu_w - 1.96 * sd_w), "high_95": price * math.exp(mu_w + 1.96 * sd_w),
        "prob_up": round(_ncdf(mu_w / sd_w) if sd_w else 0.5, 2),
        "change_pct": (math.exp(mu_w) - 1) * 100,
    }
    week["bias"] = ("up" if week["prob_up"] >= 0.55 else "down" if week["prob_up"] <= 0.45
                    else "flat")
    week["backtest"] = backtest_week(c, week_h)

    # long term: shrink the historical growth rate toward a sober prior
    r_all = _logret(c)
    years = len(r_all) / per_year
    hist_mu = (sum(r_all) / years) if years > 0.5 else _PRIOR[k]
    w = min(0.5, years / 10)                          # 5y of data → 50 % weight
    mu_y = w * hist_mu + (1 - w) * _PRIOR[k] + tech["score"] * 0.02 + sscore * 0.01
    mu_y = max(-_CAP[k], min(_CAP[k], mu_y))
    vol_y = (_std(r_all[-per_year * 2:]) or 0.01) * math.sqrt(per_year)
    long = []
    for label, yrs in (("1 month", 1 / 12), ("3 months", 0.25), ("6 months", 0.5),
                       ("1 year", 1.0), ("3 years", 3.0), ("5 years", 5.0)):
        m = mu_y * yrs                                       # median (log) path
        s = vol_y * math.sqrt(yrs)
        long.append({"horizon": label, "median": price * math.exp(m),
                     "low_68": price * math.exp(m - s), "high_68": price * math.exp(m + s),
                     "change_pct": (math.exp(m) - 1) * 100,
                     "prob_up": round(_ncdf(m / s) if s else 0.5, 2)})

    return {"symbol": symbol, "kind": k, "price": price,
            "currency": (q.currency if q else "USD"),
            "name": (q.name if q else symbol), "technicals": tech,
            "week": week, "long_term": long,
            "yearly_drift_pct": round(mu_y * 100, 1), "yearly_vol_pct": round(vol_y * 100, 1),
            "history_years": round(years, 1), "history": c[-180:],
            "disclaimer": DISCLAIMER}


# ══════════════════════════════════════════════════════════════════════════════
#  Text for speaking
# ══════════════════════════════════════════════════════════════════════════════

def fmt_price(p: float | None, cur: str = "USD") -> str:
    if p is None:
        return "—"
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "INR": "₹"}.get(cur, "")
    if abs(p) >= 1000: s = f"{p:,.0f}" if abs(p) >= 100000 else f"{p:,.2f}"
    elif abs(p) >= 1: s = f"{p:,.2f}"
    elif abs(p) >= 0.01: s = f"{p:.4f}"
    else: s = f"{p:.8f}".rstrip("0")
    return f"{sym}{s}" if sym else f"{s} {cur}".strip()


def quote_text(q: Quote) -> str:
    arrow = "up" if q.change >= 0 else "down"
    txt = (f"{q.name} ({q.symbol}) is at {fmt_price(q.price, q.currency)}, {arrow} "
           f"{abs(q.change_pct):.2f}% today")
    if q.day_low and q.day_high:
        txt += f" (day range {fmt_price(q.day_low, q.currency)}–{fmt_price(q.day_high, q.currency)})"
    if q.high_52w and q.price >= q.high_52w * 0.98:
        txt += ", near its 52-week high"
    elif q.low_52w and q.price <= q.low_52w * 1.02:
        txt += ", near its 52-week low"
    return txt + "."


def sentiment_text(s: dict, name: str = "") -> str:
    who = name or s["symbol"]
    txt = (f"Sentiment for {who} is {s['label']} (score {s['score']:+.2f}). "
           f"Headlines: {s['positive']} positive, {s['negative']} negative, "
           f"{s['neutral']} neutral.")
    if s.get("mood"):
        m = s["mood"]
        txt += f" {m['name']}: {m['value']} ({m['label']})."
    tops = [h["title"] for h in s.get("headlines", []) if h["score"] != 0][:3]
    if tops:
        txt += " Top headlines: " + " | ".join(tops) + "."
    return txt


def forecast_text(f: dict) -> str:
    if f.get("error"):
        return f["error"]
    cur = f["currency"]; w = f["week"]
    txt = (f"{f['name']} at {fmt_price(f['price'], cur)}. Trend: {f['technicals']['trend']}, "
           f"RSI {f['technicals']['rsi']}. {w['horizon'].capitalize()}: leaning {w['bias']} "
           f"({int(w['prob_up'] * 100)}% chance higher), likely range "
           f"{fmt_price(w['low_68'], cur)} to {fmt_price(w['high_68'], cur)}, "
           f"midpoint {fmt_price(w['expected'], cur)}.")
    bt = w.get("backtest")
    if bt:
        txt += (f" This weekly call was right {int(bt['direction_hit_rate'] * 100)}% of the time "
                f"over the last year (price going up was {int(bt['base_rate_up'] * 100)}%).")
    lt = {x["horizon"]: x for x in f["long_term"]}
    parts = []
    for h in ("3 months", "1 year", "5 years"):
        x = lt[h]
        parts.append(f"{h}: about {fmt_price(x['median'], cur)} "
                     f"(range {fmt_price(x['low_68'], cur)}–{fmt_price(x['high_68'], cur)})")
    txt += " Long term — " + "; ".join(parts) + "."
    return txt + " " + DISCLAIMER


# ══════════════════════════════════════════════════════════════════════════════
#  Watchlist + price alerts (persisted in config/market.json)
# ══════════════════════════════════════════════════════════════════════════════

_store_lock = threading.RLock()
_listeners: list = []          # callables(event: str, data: dict) — the sidebar card


def on_event(cb) -> None:
    if cb not in _listeners:
        _listeners.append(cb)


def emit(event: str, data: dict | None = None) -> None:
    for cb in list(_listeners):
        try:
            cb(event, data or {})
        except Exception:
            pass


def _load() -> dict:
    with _store_lock:
        try:
            d = json.loads(STORE_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("watchlist", list(DEFAULT_WATCHLIST))
                d.setdefault("alerts", [])
                return d
        except Exception:
            pass
        return {"watchlist": list(DEFAULT_WATCHLIST), "alerts": []}


def _save(d: dict) -> None:
    with _store_lock:
        try:
            STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STORE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
            tmp.replace(STORE_FILE)
        except Exception as e:
            print(f"[Market] could not save {STORE_FILE}: {e}")


def watchlist() -> list[str]:
    return list(_load()["watchlist"])


def add_to_watchlist(symbol: str) -> bool:
    with _store_lock:
        d = _load(); s = symbol.upper()
        if s in d["watchlist"]:
            return False
        d["watchlist"].append(s); _save(d)
    emit("watchlist")
    return True


def remove_from_watchlist(symbol: str) -> bool:
    with _store_lock:
        d = _load(); s = symbol.upper()
        if s not in d["watchlist"]:
            return False
        d["watchlist"].remove(s); _save(d)
    emit("watchlist")
    return True


def alerts() -> list[dict]:
    return list(_load()["alerts"])


def add_alert(symbol: str, price: float, direction: str | None = None) -> dict:
    s = symbol.upper()
    if direction not in ("above", "below"):
        q = get_quote(s)
        direction = "above" if (not q or price >= q.price) else "below"
    a = {"symbol": s, "price": float(price), "direction": direction, "created": time.time()}
    with _store_lock:
        d = _load(); d["alerts"].append(a); _save(d)
    emit("alerts")
    start_alert_watcher()
    return a


def clear_alerts(symbol: str | None = None) -> int:
    with _store_lock:
        d = _load(); before = len(d["alerts"])
        d["alerts"] = [a for a in d["alerts"] if symbol and a["symbol"] != symbol.upper()]
        _save(d)
    emit("alerts")
    return before - len(d["alerts"])


_notifiers: list = []          # callables(text) — speak / log / toast
_watcher: threading.Thread | None = None


def add_notifier(cb) -> None:
    if cb not in _notifiers:
        _notifiers.append(cb)


def check_alerts_once() -> list[dict]:
    """Fire (and remove) alerts whose price was crossed. Returns those fired."""
    al = alerts()
    if not al:
        return []
    qs = get_quotes(sorted({a["symbol"] for a in al}))
    fired, keep = [], []
    for a in al:
        q = qs.get(a["symbol"])
        hit = q and ((a["direction"] == "above" and q.price >= a["price"]) or
                     (a["direction"] == "below" and q.price <= a["price"]))
        (fired if hit else keep).append(a)
        if hit:
            a["hit_price"] = q.price; a["name"] = q.name; a["currency"] = q.currency
    if fired:
        with _store_lock:
            d = _load()
            d["alerts"] = [x for x in d["alerts"]
                           if not any(x.get("created") == f.get("created") and
                                      x["symbol"] == f["symbol"] for f in fired)]
            _save(d)
        for f in fired:
            msg = (f"Price alert: {f.get('name') or f['symbol']} is now "
                   f"{fmt_price(f['hit_price'], f.get('currency', 'USD'))}, "
                   f"{f['direction']} your {fmt_price(f['price'], f.get('currency', 'USD'))} level.")
            for cb in list(_notifiers):
                try:
                    cb(msg)
                except Exception:
                    pass
        emit("alerts")
    return fired


def start_alert_watcher(interval: float = 60.0) -> None:
    global _watcher
    if _watcher and _watcher.is_alive():
        return

    def loop():
        while True:
            try:
                if not alerts():
                    return                      # nothing to watch — thread ends
                check_alerts_once()
            except Exception as e:
                print(f"[Market] alert check failed: {e}")
            time.sleep(interval)

    _watcher = threading.Thread(target=loop, name="market-alerts", daemon=True)
    _watcher.start()


def overview() -> dict:
    """Big picture: major indices, crypto, gold, oil, dollar + fear gauges."""
    groups = {
        "Stocks": ["^GSPC", "^IXIC", "^DJI", "^FTSE", "^N225", "^TASI.SR"],
        "Crypto": ["BTC-USD", "ETH-USD", "SOL-USD"],
        "Commodities & FX": ["GC=F", "CL=F", "DX-Y.NYB", "EURUSD=X"],
    }
    allsyms = [s for g in groups.values() for s in g] + ["^VIX"]
    qs = get_quotes(allsyms)
    return {"groups": {g: [qs[s] for s in syms if s in qs] for g, syms in groups.items()},
            "vix": qs.get("^VIX"), "fear_greed": fear_greed()}
