import queue
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from quantecho import ai
from quantecho.config import Settings, load_settings, save_settings
from quantecho.poller import Poller
from quantecho.schwab_provider import NY, SchwabProvider, latest_session, parse_candles, parse_quotes
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

    def raise_for_status(self):
        pass

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


def candles(n, start_px=100.0, step=0.1, day=(2026, 11, 5)):
    out = []
    for i in range(n):
        px = start_px + i * step
        out.append({"datetime": _ms(*day, 9, 30) + i * 60_000, "open": px, "high": px + 0.05,
                    "low": px - 0.05, "close": px, "volume": 1000 + i})
    return out


class TestParsing(unittest.TestCase):
    def test_quotes_nested_schwab_shape(self):
        q = parse_quotes(QUOTES)
        self.assertEqual(set(q), {"SPY", "NVDA"})
        self.assertEqual(q["SPY"].last, 512.34)
        self.assertEqual(q["SPY"].bid, 512.33)
        self.assertEqual(q["SPY"].timestamp_ms, 1730900000000)
        self.assertEqual(q["NVDA"].net_pct, -1.2)
        self.assertIsNone(q["NVDA"].bid)

    def test_candles_sorted_and_latest_session(self):
        rows = parse_candles({"candles": candles(3, day=(2026, 11, 5)) + candles(2, day=(2026, 11, 4))})
        self.assertEqual(len(rows), 5)
        self.assertEqual([r["t"] for r in rows], sorted(r["t"] for r in rows))
        self.assertEqual(len(latest_session(rows)), 3)

    def test_provider_decodes_httpx_response(self):
        p = SchwabProvider("", "", "", client=FakeClient(candles(40)))
        self.assertEqual(p.get_quotes(["SPY"])["SPY"].last, 512.34)
        self.assertEqual(len(p.get_session_minute_bars("spy")), 40)

    def test_http_error_raises(self):
        class Bad(FakeClient):
            def get_quotes(self, s):
                return FakeResp({}, status=401)
        with self.assertRaises(RuntimeError):
            SchwabProvider("", "", "", client=Bad([])).get_quotes(["SPY"])


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
        rows = parse_candles({"candles": candles(60)})
        ta = compute_ta(rows)
        for v in (ta.vwap, ta.rsi14, ta.macd, ta.macd_signal):
            self.assertIsNotNone(v)


class TestPollerAndTelemetry(unittest.TestCase):
    def test_poll_once_populates_context(self):
        ev = queue.Queue()
        tel = TelemetryStore()
        p = Poller(SchwabProvider("", "", "", client=FakeClient(candles(60))), ["nvda", "spy"], "spy",
                   5, ev, tel)
        self.assertEqual(p.symbols, ["SPY", "NVDA"])
        p.poll_once()
        ctx = tel.context("SPY")
        self.assertIn("FOCUS: SPY", ctx)
        self.assertIn("Last 512.34", ctx)
        self.assertIn("RSI14", ctx)
        self.assertNotIn("RSI14 n/a", ctx)
        kinds = [ev.get_nowait()[0] for _ in range(ev.qsize())]
        self.assertIn("quotes", kinds)

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
        self.assertIn("Bars error", logs)


class TestAI(unittest.TestCase):
    def test_reasoning_budget(self):
        self.assertEqual(ai.build_request("hi", "gpt-5")["reasoning"], {"effort": "low"})
        self.assertNotIn("reasoning", ai.build_request("hi", "gpt-4.1"))

    def test_extract_text(self):
        resp = {"output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": "Bullish."}]},
        ]}
        self.assertEqual(ai.extract_text(resp), "Bullish.")
        self.assertIn("incomplete", ai.extract_text(
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}))


class TestConfig(unittest.TestCase):
    def test_secrets_not_saved_without_remember(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            save_settings(Settings(openai_key="sk-x", schwab_app_key="k", remember=False, interval=7), path)
            text = path.read_text()
            self.assertNotIn("sk-x", text)
            s = load_settings(path)
            self.assertEqual(s.interval, 7)
            self.assertFalse(s.remember)


if __name__ == "__main__":
    unittest.main()
