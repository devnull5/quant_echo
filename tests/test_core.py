import json
import queue
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import quantecho.config as _cfg
from quantecho import llm


class FakeKeyring:
    """In-memory stand-in so tests never touch the real OS credential store."""

    def __init__(self):
        self.store = {}

    def get_password(self, svc, name):
        return self.store.get((svc, name))

    def set_password(self, svc, name, value):
        self.store[(svc, name)] = value

    def delete_password(self, svc, name):
        self.store.pop((svc, name), None)


_cfg._keyring = FakeKeyring()
from quantecho.config import Settings, load_settings, normalize_host, save_settings
from quantecho.poller import Poller
from quantecho.providers import PROVIDERS, ProviderError, create
from quantecho.providers.base import NY, latest_session, parse_iso
from quantecho.providers.schwab import SchwabProvider, parse_candles, parse_quotes
from quantecho.providers.simulated import SimulatedProvider
from quantecho.ta import compute_ta, ema, macd_12269, rsi14, vwap_from_bars
from quantecho.telemetry import TelemetryStore

# Shape of GET /marketdata/v1/quotes
QUOTES = {
    "SPY": {
        "assetMainType": "EQUITY",
        "symbol": "SPY",
        "quote": {
            "lastPrice": 512.34, "bidPrice": 512.33, "askPrice": 512.35,
            "totalVolume": 41000000, "quoteTime": 1730900000000,
            "netChange": 2.1, "netPercentChange": 0.41,
        },
    },
    "NVDA": {"symbol": "NVDA", "quote": {"lastPrice": 140.0, "netPercentChange": -1.2}},
    "errors": {"invalidSymbols": ["ZZZZ"]},
}


def _ms(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=NY).timestamp() * 1000)


class FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, "boom"

    def json(self):
        return self._p


class FakeClient:
    def __init__(self, bars):
        self.bars = bars

    def get_quotes(self, symbols):
        return FakeResp(QUOTES)

    def get_price_history_every_minute(self, symbol, **kw):
        assert kw["need_extended_hours_data"] is True
        return FakeResp({"candles": self.bars, "symbol": symbol})


class FakeHttp:
    """Stands in for providers.base.Http: maps a path substring -> payload."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, path, params=None, headers=None):
        self.calls.append((path, params))
        for k, v in self.routes.items():
            if k in path or (params and k in json.dumps(params)):
                if isinstance(v, Exception):
                    raise v
                return v
        raise AssertionError(f"unrouted {path} {params}")


def candles(n, start_px=100.0, step=0.1, day=(2026, 11, 5)):
    out = []
    for i in range(n):
        px = start_px + i * step
        out.append({"datetime": _ms(*day, 9, 30) + i * 60_000, "open": px, "high": px + 0.05,
                    "low": px - 0.05, "close": px, "volume": 1000 + i})
    return out


# ============================================================ Schwab

class TestSchwab(unittest.TestCase):
    def test_quotes_nested_schwab_shape(self):
        q = parse_quotes(QUOTES)
        self.assertEqual(set(q), {"SPY", "NVDA"})
        self.assertEqual(q["SPY"].last, 512.34)
        self.assertEqual(q["SPY"].bid, 512.33)
        self.assertEqual(q["SPY"].timestamp_ms, 1730900000000)
        self.assertEqual(q["NVDA"].net_pct, -1.2)
        self.assertIsNone(q["NVDA"].bid)

    def test_candles_sorted_and_latest_session(self):
        rows = latest_session(parse_candles({"candles": candles(3) + candles(2, day=(2026, 11, 4))}))
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["t"] for r in rows], sorted(r["t"] for r in rows))

    def test_provider_decodes_httpx_response(self):
        p = SchwabProvider("", "", "", client=FakeClient(candles(40)))
        self.assertEqual(p.get_quotes(["SPY"])["SPY"].last, 512.34)
        self.assertEqual(len(p.get_session_minute_bars("spy")), 40)

    def test_http_error_raises(self):
        class Bad(FakeClient):
            def get_quotes(self, s):
                return FakeResp({}, status=401)
        with self.assertRaises(ProviderError):
            SchwabProvider("", "", "", client=Bad([])).get_quotes(["SPY"])


# ============================================================ other providers

T0 = _ms(2026, 11, 5, 9, 30)


class TestProviders(unittest.TestCase):
    def test_registry_and_missing_creds(self):
        self.assertGreaterEqual(len(PROVIDERS), 9)
        for pid, spec in PROVIDERS.items():
            if any(f.secret for f in spec.fields):
                with self.assertRaises(ProviderError, msg=pid):
                    create(pid, {})

    def test_yahoo(self):
        from quantecho.providers.yahoo import YahooProvider
        chart = {"chart": {"error": None, "result": [{
            "meta": {"symbol": "SPY", "regularMarketPrice": 740.65, "chartPreviousClose": 733.0,
                     "regularMarketVolume": 14234102, "regularMarketTime": 1793978820,
                     "regularMarketChangePercent": 1.044},
            "timestamp": [T0 // 1000, T0 // 1000 + 60, T0 // 1000 + 120],
            "indicators": {"quote": [{"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
                                      "close": [1.5, None, 3.5], "volume": [10, 20, 30]}]},
        }]}}
        spark = {"SPY": {"symbol": "SPY", "fulldayPrice": 740.65, "previousClose": 733.0,
                         "fulldayChangePercent": 1.044, "timestamp": [1793978700, 1793978820],
                         "close": [740.1, 740.65]}}
        YahooProvider._volume_cache.clear()
        p = YahooProvider(http=FakeHttp({"spark": spark, "chart/SPY": chart}))
        q = p.get_quotes(["spy"])["SPY"]
        self.assertEqual((q.last, q.net_pct, q.timestamp_ms, q.total_volume), (740.65, 1.044, 1793978820000, 14234102))
        bars = p.get_session_minute_bars("SPY")
        self.assertEqual([b["close"] for b in bars], [1.5, 3.5])  # None close dropped

    def test_yahoo_batches_and_class_shares(self):
        from quantecho.providers.yahoo import YahooProvider, to_yahoo
        from quantecho.watchlists import DEFAULT_EQUITY
        syms = DEFAULT_EQUITY.split()
        self.assertEqual(len(syms), 21)
        self.assertEqual(to_yahoo("BRK.B"), "BRK-B")

        class SparkHttp:
            def __init__(self):
                self.calls = []

            def get(self, path, params=None, headers=None):
                self.calls.append((path, params))
                if path == "spark":
                    asked = params["symbols"].split(",")
                    assert len(asked) <= 20, "Yahoo rejects >20 symbols per spark call"
                    return {y: {"symbol": y, "fulldayPrice": 100.0, "previousClose": 99.0, "timestamp": [1],
                                "close": [100.0]} for y in asked}
                y = path.split("/", 1)[1]
                return {"chart": {"result": [{"meta": {"symbol": y, "regularMarketPrice": 100.0,
                                                        "regularMarketVolume": 5000}}]}}

        YahooProvider._volume_cache.clear()
        http = SparkHttp()
        p = YahooProvider(http=http)
        q = p.get_quotes(syms)
        self.assertEqual(len(q), 21)
        self.assertIn("BRK.B", q)                                    # keyed by the user's spelling
        self.assertEqual(sum(c[0] == "spark" for c in http.calls), 2)  # 21 symbols -> 2 batch calls
        self.assertEqual(sum(c[0].startswith("chart/") for c in http.calls), 21)  # all volumes filled at once
        self.assertTrue(all(v.total_volume == 5000 for v in q.values()))         # ...on the FIRST poll

        http.calls.clear()
        p.get_quotes(syms)
        self.assertEqual(sum(c[0].startswith("chart/") for c in http.calls), 2)   # then gentle 2-per-poll refresh

        # restart (new provider, e.g. focus change): cache survives, no re-fill burst
        http2 = SparkHttp()
        q2 = YahooProvider(http=http2).get_quotes(syms)
        self.assertTrue(all(v.total_volume == 5000 for v in q2.values()))
        self.assertEqual(sum(c[0].startswith("chart/") for c in http2.calls), 2)
        YahooProvider._volume_cache.clear()

    def test_interval_scales_with_watchlist(self):
        self.assertEqual(PROVIDERS["yahoo"].interval_for(21, 20), 20)       # batched
        self.assertEqual(PROVIDERS["finnhub"].interval_for(21, 20), 26)     # 21 calls at 50/min
        self.assertEqual(PROVIDERS["twelvedata"].interval_for(3, 60), 60)
        self.assertEqual(PROVIDERS["twelvedata"].interval_for(21, 60), 158)

    def test_yahoo_error(self):
        from quantecho.providers.yahoo import YahooProvider
        with self.assertRaises(ProviderError):  # spark omits unknown symbols -> nothing returned
            YahooProvider(http=FakeHttp({"spark": {}})).get_quotes(["ZZZZ"])

    def test_alpaca(self):
        from quantecho.providers.alpaca import AlpacaProvider
        snaps = {"SPY": {
            "latestTrade": {"p": 512.5, "t": "2026-11-05T15:00:00.123456789Z"},
            "latestQuote": {"bp": 512.49, "ap": 512.51, "t": "2026-11-05T15:00:00Z"},
            "dailyBar": {"c": 512.4, "v": 900000}, "prevDailyBar": {"c": 500.0},
        }}
        bars = {"bars": [{"t": "2026-11-05T14:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}],
                "next_page_token": None}
        p = AlpacaProvider("k", "s", http=FakeHttp({"snapshots": snaps, "/bars": bars}))
        q = p.get_quotes(["SPY"])["SPY"]
        self.assertEqual((q.last, q.bid, q.ask, q.total_volume), (512.5, 512.49, 512.51, 900000))
        self.assertAlmostEqual(q.net_pct, 2.5)
        self.assertEqual(q.timestamp_ms, parse_iso("2026-11-05T15:00:00.123Z"))
        self.assertEqual(len(p.get_session_minute_bars("SPY")), 1)

    def test_polygon_snapshot_and_free_fallback(self):
        from quantecho.providers.polygon import PolygonProvider
        snap = {"tickers": [{"ticker": "SPY", "todaysChangePerc": 0.5, "todaysChange": 2.5,
                             "lastTrade": {"p": 505.0, "t": 1730900000000000000},
                             "lastQuote": {"p": 504.9, "P": 505.1}, "day": {"v": 1e6}, "prevDay": {"c": 502.5}}]}
        q = PolygonProvider("k", http=FakeHttp({"snapshot": snap})).get_quotes(["SPY"])["SPY"]
        self.assertEqual((q.last, q.bid, q.ask, q.timestamp_ms), (505.0, 504.9, 505.1, 1730900000000))

        prev = {"results": [{"t": T0, "o": 1, "h": 1, "l": 1, "c": 499.0, "v": 5}]}
        http = FakeHttp({"snapshot": ProviderError("HTTP 403: NOT_AUTHORIZED"), "/prev": prev})
        p = PolygonProvider("k", http=http)
        self.assertEqual(p.get_quotes(["SPY"])["SPY"].last, 499.0)
        p.get_quotes(["SPY"])
        self.assertEqual(sum("snapshot" in c[0] for c in http.calls), 1)  # doesn't retry paid endpoint

    def test_finnhub(self):
        from quantecho.providers.finnhub import FinnhubProvider
        http = FakeHttp({"quote": {"c": 261.74, "d": 1.2, "dp": 0.46, "pc": 260.5, "t": 1730900000},
                         "candle": ProviderError("HTTP 403: You don't have access to this resource.")})
        p = FinnhubProvider("k", http=http)
        q = p.get_quotes(["AAPL"])["AAPL"]
        self.assertEqual((q.last, q.net_pct, q.timestamp_ms), (261.74, 0.46, 1730900000000))
        with self.assertRaises(ProviderError):
            p.get_session_minute_bars("AAPL")
        self.assertEqual(p.get_session_minute_bars("AAPL"), [])  # remembers, stops asking

    def test_tiingo(self):
        from quantecho.providers.tiingo import TiingoProvider
        iex = [{"ticker": "SPY", "last": 510.0, "prevClose": 500.0, "bidPrice": 509.9, "askPrice": 510.1,
                "volume": 12345, "timestamp": "2026-11-05T15:00:00.000000-05:00"}]
        prices = [{"date": "2026-11-05T14:30:00.000Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
                   "volume": 10}]
        p = TiingoProvider("k", http=FakeHttp({"/prices": prices, "iex/": iex}))
        q = p.get_quotes(["SPY"])["SPY"]
        self.assertEqual((q.last, q.bid, q.net_pct), (510.0, 509.9, 2.0))
        self.assertEqual(len(p.get_session_minute_bars("SPY")), 1)

    def test_twelvedata(self):
        from quantecho.providers.twelvedata import TwelveDataProvider
        multi = {"SPY": {"symbol": "SPY", "close": "510.5", "volume": "100", "percent_change": "0.3",
                         "change": "1.5", "timestamp": 1730900000},
                 "BAD": {"status": "error", "code": 400, "message": "symbol not found"}}
        series = {"meta": {}, "status": "ok", "values": [
            {"datetime": "2026-11-05 09:31:00", "open": "1", "high": "2", "low": "0.5", "close": "1.6", "volume": "5"},
            {"datetime": "2026-11-05 09:30:00", "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "5"},
        ]}
        p = TwelveDataProvider("k", http=FakeHttp({"time_series": series, "quote": multi}))
        q = p.get_quotes(["SPY", "BAD"])
        self.assertEqual(list(q), ["SPY"])
        self.assertEqual(q["SPY"].last, 510.5)
        bars = p.get_session_minute_bars("SPY")
        self.assertEqual([b["close"] for b in bars], [1.5, 1.6])  # oldest first
        self.assertEqual(bars[0]["t"], T0)
        with self.assertRaises(ProviderError):
            TwelveDataProvider("k", http=FakeHttp({"quote": {"status": "error", "code": 401,
                                                             "message": "bad key"}})).get_quotes(["SPY"])

    def test_alphavantage(self):
        from quantecho.providers.alphavantage import AlphaVantageProvider
        gq = {"Global Quote": {"01. symbol": "IBM", "05. price": "250.10", "06. volume": "1000",
                               "09. change": "1.10", "10. change percent": "0.4418%"}}
        intraday = {"Meta Data": {"6. Time Zone": "US/Eastern"}, "Time Series (1min)": {
            "2026-11-05 09:30:00": {"1. open": "1", "2. high": "2", "3. low": "0.5", "4. close": "1.5",
                                    "5. volume": "9"}}}
        p = AlphaVantageProvider("k", http=FakeHttp({"GLOBAL_QUOTE": gq, "INTRADAY": intraday}))
        q = p.get_quotes(["IBM"])["IBM"]
        self.assertEqual((q.last, q.net_pct), (250.10, 0.4418))
        self.assertEqual(p.get_session_minute_bars("IBM")[0]["t"], T0)
        limited = AlphaVantageProvider("k", http=FakeHttp({"GLOBAL_QUOTE": {"Information": "rate limit 25/day"}}))
        from quantecho.providers import RateLimited
        with self.assertRaises(RateLimited):
            limited.get_quotes(["IBM"])

    def test_simulated_end_to_end(self):
        ev, tel = queue.Queue(), TelemetryStore()
        Poller(SimulatedProvider(seed=1), ["SPY", "TSLA"], "SPY", 1, ev, tel).poll_once()
        ctx = tel.context("SPY")
        self.assertIn("SYMBOL: SPY", ctx)
        self.assertNotIn("RSI14 n/a", ctx)
        self.assertIsNotNone(tel.quote("TSLA").last)

    def test_http_redacts_keys(self):
        from quantecho.providers.base import Http

        class Boom:
            def get(self, url, params=None, headers=None):
                import httpx
                raise httpx.ConnectError(f"failed {url}?apikey=SECRET123 token SECRET123")
        h = Http("https://x", secrets=("SECRET123",), client=Boom())
        with self.assertRaises(ProviderError) as cm:
            h.get("q")
        self.assertNotIn("SECRET123", str(cm.exception))

    def test_parse_iso_variants(self):
        a = parse_iso("2026-11-05T14:30:00Z")
        self.assertEqual(a, T0)
        self.assertEqual(parse_iso("2026-11-05T14:30:00.123456789Z"), a + 123)
        self.assertEqual(parse_iso("2026-11-05T09:30:00-05:00"), a)
        self.assertIsNone(parse_iso("garbage"))
        self.assertEqual(parse_iso("2026-11-05T14:30:00.1Z"), a + 100)       # short fractions (py3.10-safe)
        self.assertEqual(parse_iso("2026-11-05T14:30:00.1234Z"), a + 123)

    def test_html_error_bodies_cleaned(self):
        from quantecho.providers.base import http_error
        e = http_error(401, "<html><head><title>401 Authorization Required</title></head><body>x</body></html>")
        self.assertIn("401 Authorization Required", str(e))
        self.assertNotIn("<", str(e))


# ============================================================ TA

class TestTA(unittest.TestCase):
    def test_rsi_extremes(self):
        self.assertEqual(rsi14([float(i) for i in range(20)]), 100.0)
        self.assertLess(rsi14([float(20 - i) for i in range(20)]), 1.0)
        self.assertIsNone(rsi14([1.0] * 10))
        self.assertEqual(rsi14([5.0] * 20), 50.0)

    def test_macd_uptrend_positive(self):
        m, s, h = macd_12269([100 + i * 0.5 for i in range(60)])
        self.assertGreater(m, 0)
        self.assertAlmostEqual(h, m - s)

    def test_vwap_and_ema(self):
        bars = [{"high": 11, "low": 9, "close": 10, "volume": 100}, {"close": 20, "volume": 300}]
        self.assertAlmostEqual(vwap_from_bars(bars), (10 * 100 + 20 * 300) / 400)
        self.assertEqual(ema([1, 1, 1], 3), [1, 1, 1])
        self.assertIsNone(vwap_from_bars([]))

    def test_compute_ta_full(self):
        ta = compute_ta(parse_candles({"candles": candles(60)}))
        for v in (ta.vwap, ta.rsi14, ta.macd, ta.macd_signal):
            self.assertIsNotNone(v)


# ============================================================ poller

class TestPoller(unittest.TestCase):
    def test_poll_once_populates_context(self):
        ev, tel = queue.Queue(), TelemetryStore()
        p = Poller(SchwabProvider("", "", "", client=FakeClient(candles(60))), ["nvda", "spy"], "spy", 5, ev, tel)
        self.assertEqual(p.symbols, ["SPY", "NVDA"])
        p.poll_once()
        ctx = tel.context("SPY")
        self.assertIn("Last 512.34", ctx)
        self.assertNotIn("RSI14 n/a", ctx)

    def test_errors_do_not_raise(self):
        class Boom:
            def get_quotes(self, s):
                raise ConnectionError("down")

            def get_session_minute_bars(self, s):
                raise ConnectionError("down")
        ev = queue.Queue()
        Poller(Boom(), ["SPY"], "SPY", 5, ev, TelemetryStore()).poll_once()
        logs = " ".join(ev.get_nowait()[1] for _ in range(ev.qsize()))
        self.assertIn("Quotes error", logs)
        self.assertNotIn("Bars", logs)  # don't hammer a second endpoint while the source is failing

    def test_backoff_and_recovery(self):
        from quantecho.providers import AuthError, RateLimited

        class Flaky:
            def __init__(self):
                self.errors = [ConnectionError("down")] * 3 + [RateLimited("429", retry_after=42)] +                               [AuthError("HTTP 401: bad key")]
                self.ok = False

            def get_quotes(self, s):
                if self.errors and not self.ok:
                    raise self.errors.pop(0)
                return parse_quotes(QUOTES)

            def get_session_minute_bars(self, s):
                return []

        ev, prov = queue.Queue(), Flaky()
        p = Poller(prov, ["SPY"], "SPY", 5, ev, TelemetryStore())
        waits = []
        for _ in range(5):
            p.poll_once()
            waits.append(p._next_wait)
        self.assertEqual(waits[:3], [10, 20, 40])        # exponential
        self.assertEqual(waits[3], 42)                    # Retry-After honoured
        self.assertEqual(waits[4], p.max_backoff)         # auth errors back off fully
        logs = [ev.get_nowait()[1] for _ in range(ev.qsize())]
        self.assertEqual(sum("ConnectionError" in m for m in logs), 1)  # not logged every cycle
        prov.ok = True
        p.poll_once()
        self.assertEqual(p._next_wait, 5)
        self.assertTrue(any("Recovered after 5" in ev.get_nowait()[1] for _ in range(ev.qsize())))

    def test_http_error_mapping(self):
        from quantecho.providers.base import AuthError, RateLimited, http_error
        e = http_error(429, "slow down", "30")
        self.assertIsInstance(e, RateLimited)
        self.assertEqual(e.retry_after, 30.0)
        self.assertIsInstance(http_error(403, "nope"), AuthError)
        self.assertIn("HTTP 403", str(http_error(403, "nope")))  # polygon fallback relies on this text
        self.assertNotIsInstance(http_error(500, "x"), (RateLimited, AuthError))


# ============================================================ Ollama

class TestLLM(unittest.TestCase):
    def test_request_shape(self):
        body = llm.build_request("hi", "llama3.2")
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"][-1], {"role": "user", "content": "hi"})

    def test_extract_strips_think(self):
        self.assertEqual(llm.extract_text({"message": {"content": "<think>hmm</think>\nBullish."}}), "Bullish.")
        self.assertEqual(llm.extract_text({}), "(no answer)")

    def test_unreachable_is_friendly(self):
        out = llm.ask("hi", host="http://127.0.0.1:9", model="x")
        self.assertIn("Can't reach", out)
        self.assertEqual(llm.list_models("http://127.0.0.1:9"), [])

    def test_ask_roundtrip(self):
        with mock.patch.object(llm, "_post", return_value={"message": {"content": "Neutral."}}) as m:
            self.assertEqual(llm.ask("q", model="qwen3", host="http://h:1"), "Neutral.")
            self.assertEqual(m.call_args[0][0], "http://h:1/api/chat")


# ============================================================ config

class TestConfig(unittest.TestCase):
    def test_secrets_go_to_keyring_never_file(self):
        kr = _cfg._keyring = FakeKeyring()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            failed = save_settings(Settings(provider="alpaca", remember=True,
                                            creds={"alpaca_key_id": "AKX", "alpaca_secret": "SSS", "alpaca_feed": "sip"}),
                                   path)
            self.assertEqual(failed, [])
            text = path.read_text()
            self.assertNotIn("AKX", text)
            self.assertNotIn("SSS", text)
            self.assertEqual(kr.store[("quantecho", "alpaca_key_id")], "AKX")
            loaded = load_settings(path)
            self.assertEqual((loaded.creds["alpaca_key_id"], loaded.creds["alpaca_feed"]), ("AKX", "sip"))
            # turning Remember off wipes them from the keyring
            save_settings(Settings(remember=False, creds={"alpaca_key_id": "AKX"}), path)
            self.assertNotIn(("quantecho", "alpaca_key_id"), kr.store)

    def test_no_keyring_reports_unsaved(self):
        _cfg._keyring = False
        try:
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "s.json"
                failed = save_settings(Settings(remember=True, creds={"finnhub_token": "TOK"}), path)
                self.assertEqual(failed, ["finnhub_token"])
                self.assertNotIn("TOK", path.read_text())
        finally:
            _cfg._keyring = FakeKeyring()

    def test_legacy_plaintext_secret_scrubbed_on_save(self):
        _cfg._keyring = FakeKeyring()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            path.write_text(json.dumps({"schwab_app_key": "OLDKEY", "creds": {"finnhub_token": "OLDTOK"}}))
            s = load_settings(path)
            self.assertEqual(s.creds["finnhub_token"], "OLDTOK")
            save_settings(s, path)
            self.assertNotIn("OLD", path.read_text())
            self.assertEqual(_cfg._keyring.store[("quantecho", "schwab_app_key")], "OLDKEY")

    def test_secrets_not_saved_without_remember(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            s = Settings(provider="alpaca", remember=False, interval=7,
                         creds={"alpaca_key_id": "AKX", "alpaca_feed": "sip"})
            save_settings(s, path)
            self.assertNotIn("AKX", path.read_text())
            loaded = load_settings(path)
            self.assertEqual((loaded.provider, loaded.interval, loaded.remember), ("alpaca", 7, False))
            self.assertEqual(loaded.creds["alpaca_feed"], "sip")

    def test_legacy_flat_keys_migrate(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            path.write_text(json.dumps({"schwab_app_key": "OLD", "provider": "nope"}))
            s = load_settings(path)
            self.assertEqual(s.creds["schwab_app_key"], "OLD")
            self.assertEqual(s.provider, "simulated")  # unknown provider -> safe demo default

    def test_normalize_host(self):
        self.assertEqual(normalize_host("0.0.0.0"), "http://127.0.0.1:11434")
        self.assertEqual(normalize_host("localhost:11434"), "http://localhost:11434")
        self.assertEqual(normalize_host(""), "http://127.0.0.1:11434")


class TestStreamingAndContext(unittest.TestCase):
    def _lines(self, *msgs, done=None):
        out = [json.dumps({"message": m}).encode() + b"\n" for m in msgs]
        out.append(json.dumps({"message": {}, "done": True, **(done or {})}).encode() + b"\n")

        class Ctx(list):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return Ctx(out)

    def test_stream_thinking_field(self):
        lines = self._lines({"thinking": "hmm "}, {"thinking": "ok"}, {"content": " Bull"}, {"content": "ish."},
                            done={"eval_count": 100, "eval_duration": 2_000_000_000})
        toks, thinks = [], []
        with mock.patch.object(llm, "_open", return_value=lines):
            text, st = llm.ask_stream("q", toks.append, think=True, on_thinking=thinks.append)
        self.assertEqual(text, "Bullish.")
        self.assertEqual(thinks[-1], 6)
        self.assertEqual(st.tok_per_s, 50.0)

    def test_stream_inline_think_hidden(self):
        lines = self._lines({"content": "<think>secret"}, {"content": " stuff</think>Answer"}, {"content": "!"})
        toks = []
        with mock.patch.object(llm, "_open", return_value=lines):
            text, _ = llm.ask_stream("q", toks.append)
        self.assertEqual(text, "Answer!")
        self.assertNotIn("secret", "".join(toks))

    def test_stream_error_chunk(self):
        bad = self._lines()
        bad.insert(0, json.dumps({"error": "model 'x' not found"}).encode())
        with mock.patch.object(llm, "_open", return_value=bad):
            text, _ = llm.ask_stream("q", lambda t: None, model="x")
        self.assertIn("ollama pull x", text)

    def test_resample_and_rth_vwap(self):
        from quantecho.telemetry import resample, session_phase
        bars = parse_candles({"candles": candles(12)})
        b5 = resample(bars, 5)
        self.assertEqual(len(b5), 3)
        self.assertEqual(b5[0]["volume"], sum(1000 + i for i in range(5)))
        self.assertEqual(session_phase(T0), "regular session")
        self.assertEqual(session_phase(T0 - 3600_000), "pre-market")
        tel = TelemetryStore()
        pre = [{"t": T0 - 60_000 * (i + 1), "open": 1, "high": 1, "low": 1, "close": 1.0, "volume": 1e6}
               for i in range(5)]
        tel.update_bars("SPY", sorted(pre, key=lambda b: b["t"]) + bars)
        self.assertGreater(tel.ta("SPY").vwap, 99)  # pre-market junk at $1 excluded from VWAP


class TestSignalsAndHygiene(unittest.TestCase):
    def test_signal_transitions_only(self):
        from quantecho.signals import SignalTracker
        from quantecho.ta import TA
        t = SignalTracker("SPY")
        self.assertEqual(t.update(101, TA(vwap=100, rsi14=50)), [])           # first sight: no event
        self.assertEqual(t.update(101.5, TA(vwap=100, rsi14=50)), [])         # unchanged state
        ev = t.update(99, TA(vwap=100, rsi14=25))
        texts = " | ".join(e.text for e in ev)
        self.assertIn("BELOW VWAP", texts)
        self.assertIn("oversold", texts)
        self.assertEqual(t.update(99, TA(vwap=100, rsi14=25)), [])           # no repeat

    def test_session_extremes_ratchet(self):
        from quantecho.signals import SignalTracker
        from quantecho.ta import TA
        t = SignalTracker("SPY")
        t.update(100, TA(), 101, 99)
        self.assertIn("HIGH", t.update(102, TA(), 102, 99)[0].text)
        self.assertEqual(t.update(101, TA(), 101.5, 99), [])                 # narrower range: no re-trigger
        self.assertEqual(t.update(101, TA(), 102, 99), [])

    def test_volume_clip(self):
        from quantecho.telemetry import clip_volume_outliers
        bars = parse_candles({"candles": candles(40)})
        bars[10] = {**bars[10], "volume": 5_000_000}
        clean, n = clip_volume_outliers(bars)
        self.assertEqual(n, 1)
        self.assertLess(clean[10]["volume"], 50_000)
        self.assertEqual(bars[10]["volume"], 5_000_000)  # input untouched

    def test_journal(self):
        from quantecho import journal
        with tempfile.TemporaryDirectory() as d:
            p = journal.append("Analysis SPY", "**Bias:** neutral", context="ctx", base=Path(d))
            p2 = journal.append("Q: hi", "answer", base=Path(d))
            self.assertEqual(p, p2)
            txt = p.read_text(encoding="utf-8")
            self.assertEqual(txt.count("# QuantEcho journal"), 1)
            self.assertIn("**Bias:** neutral", txt)


class TestCryptoAndStoreSafety(unittest.TestCase):
    def test_coinbase_symbols(self):
        from quantecho.providers.coinbase import normalize_symbol
        self.assertEqual(normalize_symbol("btc"), "BTC-USD")
        self.assertEqual(normalize_symbol("eth/eur"), "ETH-EUR")
        self.assertEqual(normalize_symbol("SOLUSDT"), "SOL-USDT")
        self.assertEqual(normalize_symbol("BTC-USD"), "BTC-USD")

    def test_coinbase_provider(self):
        from quantecho.providers.coinbase import CoinbaseProvider
        t0 = 1793982960
        ticker = {"ask": "75599.66", "bid": "75599.65", "price": "75599.65", "volume": "11790.4",
                  "time": "2026-11-06T16:40:15.377583815Z"}
        stats = {"open": "76270.12", "high": "76437.85", "low": "74722", "last": "75627.43", "volume": "11793.8"}
        candles = [[t0, 75700.01, 75781.5, 75760.16, 75720.99, 11.08], [t0 - 60, 75761.31, 75850, 75801.98, 75761.36, 12.4]]
        http = FakeHttp({"BTC-USD/ticker": ticker, "BTC-USD/stats": stats, "BTC-USD/candles": candles,
                         "NOPE-USD": ProviderError('HTTP 404: {"message":"NotFound"}')})
        p = CoinbaseProvider(http=http)
        q = p.get_quotes(["BTC-USD", "NOPE"])
        self.assertEqual(list(q), ["BTC-USD"])                      # bad product skipped, good one kept
        b = q["BTC-USD"]
        self.assertEqual((b.last, b.bid, b.ask), (75599.65, 75599.65, 75599.66))
        self.assertAlmostEqual(b.net_pct, (75599.65 - 76270.12) / 76270.12 * 100)
        bars = p.get_session_minute_bars("btc")
        self.assertEqual([x["t"] for x in bars], [(t0 - 60) * 1000, t0 * 1000])   # oldest first, deduped
        self.assertEqual((bars[-1]["open"], bars[-1]["high"], bars[-1]["low"]), (75760.16, 75781.5, 75700.01))
        with self.assertRaises(ProviderError):
            CoinbaseProvider(http=FakeHttp({"NOPE-USD": ProviderError("HTTP 404: NotFound")})).get_quotes(["NOPE"])

    def test_crypto_session_is_utc_day(self):
        from quantecho.telemetry import session_bars
        late = int(datetime(2026, 11, 5, 23, 59, tzinfo=NY).timestamp() * 1000)   # = 04:59 UTC on 11-06
        bars = [{"t": late - 3600_000 * 6, "close": 1}, {"t": late, "close": 2}]
        self.assertEqual(len(session_bars(bars, "crypto")), 1)       # 6h earlier is the previous UTC day
        self.assertEqual(session_bars(bars, "equity"), [])           # both outside US regular hours

    def test_crypto_context_and_vwap(self):
        tel = TelemetryStore(market="crypto")
        tel.update_quotes(SimulatedProvider(seed=3).get_quotes(["BTC-USD"]))
        now = int(time_now_ms() // 60_000 * 60_000)
        tel.update_bars("BTC-USD", [{"t": now - i * 60_000, "open": 100, "high": 101, "low": 99, "close": 100 + i % 3,
                                     "volume": 10} for i in range(120, 0, -1)])
        ctx = tel.context("BTC-USD")
        self.assertIn("trades 24/7", ctx)
        self.assertIn("VWAP(UTC day)", ctx)
        self.assertNotIn("pre-market", ctx)
        self.assertIsNotNone(tel.ta("BTC-USD").vwap)

    def test_personal_sources_hidden_by_default(self):
        from quantecho.providers import DEFAULT_PROVIDER, PROVIDERS, visible_providers
        ids = [s.id for s in visible_providers(False)]
        self.assertNotIn("yahoo", ids)
        self.assertIn("coinbase", ids)
        self.assertIn("yahoo", [s.id for s in visible_providers(False, current="yahoo")])
        self.assertFalse(PROVIDERS[DEFAULT_PROVIDER].personal_use_only)
        self.assertFalse(PROVIDERS[DEFAULT_PROVIDER].fields)       # default works with zero setup

    def test_existing_yahoo_user_keeps_yahoo(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            path.write_text(json.dumps({"provider": "yahoo"}))
            s = load_settings(path)
            self.assertEqual(s.provider, "yahoo")
            self.assertTrue(s.show_personal_sources)
            self.assertTrue(s.first_run_done)            # existing user: no welcome picker

    def test_ask_source_on_launch_persists(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            self.assertTrue(load_settings(path).ask_source_on_launch)          # default: ask every launch
            save_settings(Settings(ask_source_on_launch=False, first_run_done=True), path)
            self.assertFalse(load_settings(path).ask_source_on_launch)

    def test_new_user_sees_picker(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"DATA_PROVIDER": ""}):
            s = load_settings(Path(d) / "missing.json")
            self.assertFalse(s.first_run_done)
            self.assertEqual(s.provider, "simulated")


def time_now_ms():
    import time as _t
    return _t.time() * 1000


class TestQuietBarsLog(unittest.TestCase):
    def test_bars_line_only_on_change(self):
        ev = queue.Queue()
        p = Poller(SimulatedProvider(seed=2), ["SPY"], "SPY", 1, ev, TelemetryStore())
        for _ in range(3):
            p._refresh_bars()
        logs = [ev.get_nowait()[1] for _ in range(ev.qsize()) if True]
        self.assertEqual(sum("Bars:" in str(m) for m in logs), 1)   # first load only, not every refresh

        class Empty(SimulatedProvider):
            def get_session_minute_bars(self, s):
                return []
        p.provider = Empty()
        p._refresh_bars()
        logs = [ev.get_nowait()[1] for _ in range(ev.qsize())]
        self.assertTrue(any("none returned" in str(m) for m in logs))  # state change IS reported


if __name__ == "__main__":
    unittest.main()
