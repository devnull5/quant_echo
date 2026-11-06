# news_client.py
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Tuple
import os, logging
from typing import Any, Dict, List

try:
    import requests  # type: ignore[import-not-found]
except ModuleNotFoundError as e:
    raise RuntimeError(
        "The 'requests' package is required for news fetching. "
        "Install it with: pip install requests"
    ) from e

def _get_api_key() -> str:
    return (os.getenv("NEWSAPI_KEY", "").strip()
            or os.getenv("NEWS_API_KEY", "").strip())



# Read API key from environment VARIABLE NAME (do not put the secret here)
#   export NEWSAPI_KEY="your-real-key"
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "").strip()

NEWS_EVERYTHING_URL = globals().get("NEWS_EVERYTHING_URL", "https://newsapi.org/v2/everything")
# --- HTTP constants (module scope) ---
_UA: dict[str, str] = {"User-Agent": "QuantEcho/1.0 (+local)"}
_TRANSIENT = frozenset({408, 429, 500, 502, 503, 504})
RATE_LIMIT_S = int(os.getenv("NEWS_RATE_LIMIT_S", "60"))
_last_http_ts = 0.0

_session = requests.Session()  # reuse TCP; faster + friendlier

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

TOP_HEADLINES_URL = "https://newsapi.org/v2/top-headlines"

def _fallback_top_headlines(limit: int, language: str) -> List[Dict[str, Any]]:
    """
    Conservative fallback to /v2/top-headlines (business, US).
    Some NewsAPI plans/keys allow language on this endpoint; most rely on country.
    We pass language only if explicitly allowed via env to avoid API errors.
    """
    api_key = _get_api_key()
    if not api_key:
        return []

    params = {
        "apiKey": api_key,
        "category": "business",
        "country": "us",
        "pageSize": min(int(limit), 100),
    }

    # Use language only if you opt in (to avoid 400s on some keys)
    if os.getenv("NEWS_TH_ALLOW_LANGUAGE", "0") == "1":
        params["language"] = (language or "en").strip()

    # --- HTTP ---
    try:
        resp = _http_get(TOP_HEADLINES_URL, params=params, timeout=10, retries=2)
    except RuntimeError as http_err:
        logging.warning("Top-headlines HTTP failed: %s", http_err)
        return []

    # --- JSON ---
    try:
        payload = resp.json() or {}
    except ValueError as json_err:
        logging.warning("Top-headlines JSON failed: %s", json_err)
        return []

    if isinstance(payload, dict) and payload.get("status") == "error":
        logging.warning("NewsAPI TH error: code=%s, message=%s", payload.get("code"), payload.get("message"))
        return []

    out: List[Dict[str, Any]] = []
    for a in (payload.get("articles") or []):
        if not isinstance(a, dict):
            continue
        title = _clean(a.get("title") or "")
        url = (a.get("url") or "").strip()
        if not title and not url:
            continue

        desc = a.get("description") or ""
        text = f"{title} {desc}".lower()
        score = sum(1 for w in _POS if w in text) - sum(1 for w in _NEG if w in text)
        sentiment = max(-1.0, min(1.0, score / 3.0))

        out.append({
            "title": title,
            "source": ((a.get("source") or {}).get("name")) or "",
            "url": url,
            "published": a.get("publishedAt") or "",
            "sentiment": round(float(sentiment), 2),
        })

    return out




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
def _iso_utc(hours_back: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours_back)))
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

def _clean(s: str) -> str:
    return " ".join(str(s).split()).strip()

_POS = {"beats","surge","growth","record","upgrade","profit","rally","soar","bull","strong"}
_NEG = {"miss","cut","downgrade","loss","drop","plunge","bear","weak","lawsuit","fraud"}





def _http_get(url, params=None, timeout=10, retries=2, backoff=0.5):
    """Return Response on 2xx; raise RuntimeError otherwise (after retries)."""
    global _last_http_ts
    params = params or {}
    last_exc: BaseException | None = None

    for attempt in range(retries + 1):
        # Global throttle (simple client-side rate limit)
        now = time.time()
        wait = RATE_LIMIT_S - (now - _last_http_ts)
        if wait > 0:
            time.sleep(wait)

        try:
            r = _session.get(url, params=params, timeout=timeout, headers=_UA)
            _last_http_ts = time.time()

            if 200 <= r.status_code < 300:
                return r

            # Transient: collect details, then back off (respect Retry-After if present)
            if r.status_code in _TRANSIENT:
                ra = r.headers.get("Retry-After")
                details = (
                    f"HTTP {r.status_code} {r.reason}; "
                    f"retry_after={ra!r}; "
                    f"rate_remaining={r.headers.get('X-RateLimit-Remaining')}; "
                    f"body={r.text[:160]!r}"
                )
                last_exc = RuntimeError(details)
                if ra:
                    # server told us how long to wait (seconds)
                    try:
                        time.sleep(max(0, int(ra)))
                    except ValueError:
                        time.sleep(backoff * (2 ** attempt))
                else:
                    time.sleep(backoff * (2 ** attempt))
                continue

            # Non-transient: fail fast with context
            r.raise_for_status()

        except requests.HTTPError as http_err:
            raise RuntimeError(f"GET {url} failed: {http_err}") from http_err

        except requests.RequestException as req_err:
            # network/timeout/etc. -> retry with backoff
            last_exc = req_err
            time.sleep(backoff * (2 ** attempt))

    raise RuntimeError(f"GET {url} failed after {retries + 1} attempts") from last_exc





def fetch_news(
    keywords: List[str],
    since_hours: int = 6,
    limit: int = 12,
    language: str = "en",
    sort_by: str = "publishedAt",
) -> List[Dict[str, Any]]:
    """
    Return items like:
      {"title": str, "source": str, "url": str, "published": str|int, "sentiment": float}
    Never raises; [] on any error.
    """
    api_key = _get_api_key()
    if not api_key:
        logging.warning("NEWSAPI_KEY not set; skipping news fetch.")
        return []

    # Build compact OR query (≤25 terms; quote phrases)
    q_terms: List[str] = []
    seen: set[str] = set()
    for k in (keywords or []):
        tok = (k or "").strip()
        if not tok:
            continue
        if " " in tok:
            tok = f"\"{tok}\""
        low = tok.lower()
        if low in seen:
            continue
        seen.add(low)
        q_terms.append(tok)
        if len(q_terms) >= 25:
            break

    if not q_terms:
        return []

    params = {
        "apiKey": api_key,
        "q": " OR ".join(q_terms),
        "language": language,
        "from": _iso_utc(since_hours),
        "sortBy": sort_by,
        "pageSize": min(int(limit), 100),
        "searchIn": "title,description",
    }

    # --- HTTP ---
    try:
        resp = _http_get(NEWS_EVERYTHING_URL, params=params, timeout=10, retries=2)
    except RuntimeError as http_err:
        logging.warning("News fetch HTTP failed: %s", http_err)
        return _fallback_top_headlines(int(limit), language)

    # --- JSON ---
    try:
        data = resp.json() or {}
    except ValueError as json_err:
        logging.warning("News fetch JSON failed: %s", json_err)
        return _fallback_top_headlines(int(limit), language)

    if isinstance(data, dict) and data.get("status") == "error":
        logging.warning("NewsAPI error: code=%s, message=%s", data.get("code"), data.get("message"))
        return _fallback_top_headlines(int(limit), language)

    articles = (data.get("articles") or []) if isinstance(data, dict) else []
    out: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    for a in articles:
        if not isinstance(a, dict):
            continue
        url = (a.get("url") or "").strip()
        title = _clean(a.get("title") or "")
        if not title and not url:
            continue

        # de-dupe by URL or title (case-insensitive)
        ukey = url.lower()
        tkey = title.lower()
        if (url and ukey in seen_urls) or (title and tkey in seen_titles):
            continue
        if url:
            seen_urls.add(ukey)
        if title:
            seen_titles.add(tkey)

        desc = a.get("description") or ""
        text = f"{title} {desc}".strip().lower()
        score = sum(1 for w in _POS if w in text) - sum(1 for w in _NEG if w in text)
        sentiment = max(-1.0, min(1.0, score / 3.0))

        out.append({
            "title": title,
            "source": ((a.get("source") or {}).get("name")) or "",
            "url": url,
            "published": a.get("publishedAt") or "",
            "sentiment": round(float(sentiment), 2),
        })

    # Top-headlines fallback only if Everything returned nothing
    if not out:
        logging.info(
            "No results from /v2/everything (q=%r, from=%s); trying /v2/top-headlines.",
            params.get("q"), params.get("from")
        )
        return _fallback_top_headlines(int(limit), language)

    return out









def news_bias_score(items: List[Dict[str, Any]]) -> float:
    """
    Aggregate bias in [-1, 1].
    Prefer per-item 'sentiment' if present; otherwise use keyword heuristics on title/description.
    """
    vals: List[float] = []

    for it in items:
        sent = it.get("sentiment")
        if isinstance(sent, (int, float)):
            vals.append(float(sent))
            continue

        # Heuristic fallback: look at title/description text
        text = f"{it.get('title','')} {it.get('description','')} {it.get('desc','')}".lower()
        score = sum(1 for w in _POS if w in text) - sum(1 for w in _NEG if w in text)
        vals.append(max(-1.0, min(1.0, score / 3.0)))

    if not vals:
        return 0.0

    avg = sum(vals) / len(vals)
    return max(-1.0, min(1.0, avg))



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
    def __init__(self, ttl_seconds: int = int(os.getenv("NEWS_TTL", "300"))):
        self.cache = _TTLCache(ttl_seconds)
        self._last_good: List[Dict[str, Any]] = []

    def get(self, keywords: List[str], since_hours: int = 6, limit: int = 15) -> List[Dict[str, Any]]:
        key = (
            tuple(sorted({k.strip().lower() for k in keywords if k and k.strip()})),
            int(since_hours),
            int(limit),
        )

        cached = self.cache.get(key)
        # Only return cached data if it’s non-empty
        if isinstance(cached, list) and len(cached) > 0:
            return cached

        # Fetch fresh
        items = fetch_news(list(key[0]), since_hours=since_hours, limit=limit)

        # Cache only when we actually got headlines
        if items:
            self.cache.set(key, items)
            self._last_good = items
            return items

        # Fallback to last known-good headlines so the UI & Ollama aren’t starved
        return list(self._last_good)

