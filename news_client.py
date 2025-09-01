# news_client.py
from __future__ import annotations

import os
import html
import re
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple

import requests

# --- add near top (below imports) ---
RATE_LIMIT_S = int(os.getenv("NEWS_RATE_LIMIT_S", "60"))  # min seconds between HTTP hits
_last_http_ts = 0.0

# Read API key from environment VARIABLE NAME (do not put the secret here)
#   export NEWSAPI_KEY="your-real-key"
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "").strip()

NEWS_EVERYTHING_URL = "https://newsapi.org/v2/everything"

# --- symbol keyword map (tickers are intentional; teach your spell-checker to ignore) ---
_SYMBOL_KEYWORDS: Dict[str, List[str]] = {
    "SPY": ["S&P 500", "stocks"],
    "QQQ": ["Nasdaq 100", "tech stocks"],
    "DIA": ["Dow Jones"],
    "IWM": ["Russell 2000"],
    "NVDA": ["Nvidia"],
    "AAPL": ["Apple"],
    "MSFT": ["Microsoft"],
    "GOOGL": ["Google", "Alphabet"],
    "META": ["Meta", "Facebook"],
    "AMZN": ["Amazon"],
    "TSLA": ["Tesla"],
    "XLF": ["financials", "banks"],
    "XLE": ["energy stocks", "oil"],
    "UUP": ["US dollar", "Dollar Index"],
    "IEF": ["Treasury yields"],
    "TLT": ["Treasury yields"],
}

_MACRO_DEFAULTS: List[str] = [
    "Federal Reserve", "Treasury yields", "inflation",
    "CPI", "PCE", "jobs report", "nonfarm payrolls", "ISM", "FOMC",
]


def map_symbols_to_keywords(symbols: List[str]) -> List[str]:
    out: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        s = s.upper().strip()
        for token in [s, *_SYMBOL_KEYWORDS.get(s, [])]:
            key = token.lower()
            if key not in seen:
                seen.add(key)
                out.append(token)
    for token in _MACRO_DEFAULTS:
        key = token.lower()
        if key not in seen:
            seen.add(key)
            out.append(token)
    return out


# --- small utilities ---
def _clean(txt: str) -> str:
    txt = re.sub(r"https?://\S+", "", txt or "")  # strip URLs
    txt = html.unescape(txt)
    return " ".join(txt.split())


def _iso_utc(hours_back: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    return dt.replace(microsecond=0).isoformat()


# --- session with simple retry/backoff for 429/5xx ---
_session = requests.Session()


def _http_get(url: str, *, params: dict, timeout: float = 10.0, retries: int = 2) -> requests.Response:
    global _last_http_ts
    # hard rate-limit: at most one outbound request each RATE_LIMIT_S seconds
    wait = _last_http_ts + RATE_LIMIT_S - time.time()
    if wait > 0:
        time.sleep(wait)

    backoff = 0.75
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(backoff); backoff *= 2; continue
            r.raise_for_status()
            _last_http_ts = time.time()
            return r
        except requests.RequestException as e:
            last_exc = e
            if attempt < retries:
                time.sleep(backoff); backoff *= 2; continue
            raise
    assert last_exc
    raise last_exc



def fetch_news(
    keywords: List[str],
    since_hours: int = 6,
    limit: int = 15,
    language: str = "en",
    sort_by: str = "publishedAt",
) -> List[Dict[str, Any]]:
    """Fetch recent articles mentioning ANY of the keywords. Returns a list of dicts."""
    if not NEWSAPI_KEY:
        return []

    q_terms = [k.strip() for k in keywords if k and k.strip()]
    q_terms = q_terms[:25]  # keep the top ~25 tokens to stay under URL limits
    if not q_terms:
        return []

    q = " OR ".join(sorted(set(q_terms)))
    params = {
        "apiKey": NEWSAPI_KEY,
        "q": q,
        "language": language,
        "from": _iso_utc(since_hours),
        "sortBy": sort_by,
        "pageSize": min(int(limit), 100),
        "searchIn": "title,description",
    }

    try:
        r = _http_get(NEWS_EVERYTHING_URL, params=params, timeout=10, retries=2)
        items = (r.json() or {}).get("articles", []) or []
    except requests.RequestException:
        return []

    out: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    for a in items:
        url = (a.get("url") or "").strip()
        title = _clean(a.get("title") or "")
        if not title and not url:
            continue

        url_key = url.lower()
        title_key = title.lower()
        if (url and url_key in seen_urls) or (title and title_key in seen_titles):
            continue
        if url:
            seen_urls.add(url_key)
        if title:
            seen_titles.add(title_key)

        def _quick_sent(text: str) -> float:
            t = text.lower()
            score = sum(1 for w in _POS if w in t) - sum(1 for w in _NEG if w in t)
            return max(-1.0, min(1.0, score / 3.0))  # clamp -1..1

        # ...

        sent = _quick_sent(f"{title} {(a.get('description') or '')}")
        out.append({
            "title": title,
            "source": ((a.get("source") or {}).get("name")) or "",
            "url": url,
            "published": a.get("publishedAt") or "",
            "sentiment": round(sent, 2),
        })

    return out


# Very simple sentiment-esque nudger; keep expectations low.
_POS = {"beats", "surge", "rally", "accelerat", "growth", "upbeat", "raises", "tops", "strong", "profit", "upgrade", "record"}
_NEG = {"miss", "plunge", "drop", "cuts", "downgrade", "probe", "lawsuit", "weak", "halts", "guidance cut", "investigation", "layoff", "strike", "recall"}


def news_bias_score(items: List[Dict[str, Any]]) -> float:
    score = 0
    for it in items:
        text = f"{it.get('title','')} {it.get('desc','')}".lower()
        score += sum(1 for w in _POS if w in text)
        score -= sum(1 for w in _NEG if w in text)
    # Clamp to [-1, 1] after a light normalization
    return max(-1.0, min(1.0, score / 10.0))


def compress_bullets(items: List[Dict[str, Any]], k: int = 5) -> List[str]:
    lines: List[str] = []
    for n in items[: max(0, int(k))]:
        when = (n.get("published") or "")
        src  = (n.get("source") or "").strip()
        t    = (n.get("title") or "").strip()
        note = f"- {t}" if t else "- (no title)"
        if src:
            note += f" ({src})"
        if when:
            note += f" — {when.replace('T',' ').replace('Z',' UTC')}"
        lines.append(note)
    return lines



# --- TTL cache keyed by request parameters so different keyword sets don't collide ---
class _TTLCache:
    def __init__(self, ttl_seconds: int = 900):
        self.ttl = ttl_seconds
        self._store: Dict[Tuple[Any, ...], Tuple[float, Any]] = {}

    def get(self, key: Tuple[Any, ...]):
        item = self._store.get(key)
        if not item:
            return None
        ts, val = item
        if time.time() - ts > self.ttl:
            self._store.pop(key, None)
            return None
        return val

    def set(self, key: Tuple[Any, ...], val: Any):
        self._store[key] = (time.time(), val)


class NewsFetcher:
    def __init__(self, ttl_seconds: int = int(os.getenv("NEWS_TTL", "300"))):  # 15 min default
        self.cache = _TTLCache(ttl_seconds)


    def get(self, keywords: List[str], since_hours: int = 6, limit: int = 15) -> List[Dict[str, Any]]:
        # Stable cache key (dedup + order-insensitive)
        key = (
            tuple(sorted({k.strip().lower() for k in keywords if k and k.strip()})),
            int(since_hours),
            int(limit),
        )
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        items = fetch_news(list(key[0]), since_hours=since_hours, limit=limit)
        self.cache.set(key, items)
        return items
