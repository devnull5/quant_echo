import unittest

from quantecho.chart import (COOLDOWN, WARMUP, compute_series, find_signals, merge_live_quote, rsi_series,
                             vwap_series)

T0 = 1_793_900_000_000 // 60_000 * 60_000


def bars_from(closes, vol=1000):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append({"t": T0 + i * 60_000, "open": prev, "high": max(prev, c) + 0.05, "low": min(prev, c) - 0.05,
                    "close": c, "volume": vol})
        prev = c
    return out


class TestChartSignals(unittest.TestCase):
    def test_trend_buy_on_golden_cross_above_vwap(self):
        closes = [100 - i * 0.2 for i in range(40)] + [92 + i * 0.6 for i in range(40)]   # down, then strongly up
        b = bars_from(closes)
        sigs = find_signals(b, compute_series(b))
        buys = [s for s in sigs if s.side == "buy" and s.kind == "trend"]
        self.assertTrue(buys, "expected a trend BUY after the turn up")
        s = buys[0]
        ser = compute_series(b)
        self.assertGreater(ser.ema9[s.i], ser.ema20[s.i])
        self.assertLessEqual(ser.ema9[s.i - 1], ser.ema20[s.i - 1])
        self.assertGreater(b[s.i]["close"], ser.vwap[s.i])          # confirmed above VWAP
        self.assertFalse([x for x in sigs if x.side == "sell" and x.kind == "trend" and x.i > s.i])

    def test_trend_sell_on_death_cross_below_vwap(self):
        closes = [100 + i * 0.2 for i in range(40)] + [108 - i * 0.6 for i in range(40)]
        b = bars_from(closes)
        self.assertTrue([s for s in find_signals(b, compute_series(b)) if s.side == "sell" and s.kind == "trend"])

    def test_cross_not_confirmed_by_vwap_is_ignored(self):
        # EMA golden cross happens, but price is still far below a VWAP anchored at a high, heavy-volume open
        closes = [120] * 5 + [100 - i * 0.2 for i in range(35)] + [93 + i * 0.25 for i in range(30)]
        b = bars_from(closes)
        for k in range(5):
            b[k]["volume"] = 1_000_000
        sigs = find_signals(b, compute_series(b))
        self.assertFalse([s for s in sigs if s.side == "buy" and s.kind == "trend"])

    def test_rsi_reversal_buy(self):
        closes = [100.0] * 30 + [100 - i for i in range(1, 16)] + [86 + i * 1.5 for i in range(1, 10)]
        b = bars_from(closes)
        ser = compute_series(b)
        rev = [s for s in find_signals(b, ser) if s.kind == "reversal" and s.side == "buy"]
        self.assertTrue(rev)
        self.assertLess(ser.rsi[rev[0].i - 1], 30)
        self.assertGreaterEqual(ser.rsi[rev[0].i], 30)

    def test_forming_bar_never_marked_and_warmup_respected(self):
        closes = [100 - i * 0.2 for i in range(40)] + [92 + i * 0.6 for i in range(40)]
        b = bars_from(closes)
        sigs = find_signals(b, compute_series(b))
        self.assertTrue(all(WARMUP <= s.i < len(b) - 1 for s in sigs))

    def test_cooldown(self):
        import math
        closes = [100 + 3 * math.sin(i / 2.0) for i in range(200)]   # fast chop -> many raw crosses
        b = bars_from(closes)
        sigs = find_signals(b, compute_series(b))
        for side in ("buy", "sell"):
            idx = [s.i for s in sigs if s.side == side]
            self.assertTrue(all(b2 - a >= COOLDOWN for a, b2 in zip(idx, idx[1:], strict=False)))

    def test_vwap_starts_at_session(self):
        b = bars_from([10, 10, 20, 20])
        v = vwap_series(b, [False, False, True, True])
        self.assertEqual(v[:2], [None, None])
        self.assertAlmostEqual(v[3], (20 + 20 + 0.05 - 0.05 + 20) / 3, delta=5)

    def test_rsi_series_shape(self):
        r = rsi_series([float(i) for i in range(30)])
        self.assertIsNone(r[13])
        self.assertEqual(r[29], 100.0)

    def test_merge_live_quote(self):
        b = bars_from([100, 101])
        same = merge_live_quote(b, 103.0, b[-1]["t"] + 30_000)          # same minute: update forming candle
        self.assertEqual((len(same), same[-1]["close"], same[-1]["high"]), (2, 103.0, 103.0))
        nxt = merge_live_quote(b, 99.0, b[-1]["t"] + 61_000)            # next minute: new candle
        self.assertEqual((len(nxt), nxt[-1]["open"], nxt[-1]["close"]), (3, 101, 99.0))
        self.assertEqual(b[-1]["close"], 101)                          # input untouched
        self.assertEqual(len(merge_live_quote(b, 99.0, b[-1]["t"] + 3_600_000)), 2)  # stale gap: don't invent


class TestTicksAndResponsiveness(unittest.TestCase):
    def test_apply_tick(self):
        from quantecho.chart import apply_tick
        b = bars_from([100, 101])
        t_last = b[-1]["t"]
        u = apply_tick(b, 103.0, t_last + 10_000, size=2.5)
        self.assertEqual((len(u), u[-1]["close"], u[-1]["high"], u[-1]["volume"]), (2, 103.0, 103.0, 1002.5))
        n = apply_tick(u, 99.0, t_last + 61_000, size=1.0)
        self.assertEqual((len(n), n[-1]["open"], n[-1]["low"], n[-1]["volume"]), (3, 103.0, 99.0, 1.0))
        self.assertEqual(len(apply_tick(b, 99.0, t_last - 120_000)), 2)          # stale tick ignored
        self.assertEqual(len(apply_tick(b, 99.0, t_last + 3_600_000)), 2)        # big gap: no invented candles
        self.assertEqual(b[-1]["close"], 101)                                    # input untouched

    def test_coalescer_sums_sizes(self):
        import queue
        import time
        from quantecho.stream import Tick, _Coalescer
        q = queue.Queue()
        c = _Coalescer(q, min_gap_s=0.2)
        for k in range(10):
            c.add(Tick("BTC-USD", 100 + k, 1000 + k, size=0.5))
        self.assertEqual(q.qsize(), 1)                    # first tick out immediately, rest held
        c.flush()
        ticks = [q.get_nowait()[1] for _ in range(q.qsize())]
        self.assertEqual(ticks[-1].price, 109)            # latest price wins
        self.assertAlmostEqual(sum(t.size for t in ticks), 5.0)  # no volume lost
        time.sleep(0.01)

    def test_parse_coinbase(self):
        from quantecho.stream import parse_coinbase
        t = parse_coinbase({"type": "ticker", "product_id": "BTC-USD", "price": "73530.01", "best_bid": "73530.0",
                            "best_ask": "73530.02", "last_size": "0.25", "time": "2026-11-11T15:15:31.689481Z",
                            "open_24h": "73130.01"})
        self.assertEqual((t.symbol, t.price, t.size, t.bid, t.open_24h), ("BTC-USD", 73530.01, 0.25, 73530.0, 73130.01))
        self.assertIsNone(parse_coinbase({"type": "heartbeat"}))

    def test_update_tick_moves_quote(self):
        from quantecho.models import Quote
        from quantecho.telemetry import TelemetryStore
        tel = TelemetryStore()
        tel.update_quotes({"SPY": Quote("SPY", 100.0, None, None, 1, 5e6, net_change=2.0, net_pct=2.04)})
        tel.update_tick("SPY", 101.0, 2, bid=100.99, ask=101.01)
        q = tel.quote("SPY")
        self.assertEqual((q.last, q.bid, q.timestamp_ms, q.total_volume), (101.0, 100.99, 2, 5e6))
        self.assertAlmostEqual(q.net_change, 3.0)                 # vs the same previous close (98)
        tel.update_tick("SPY", 99.0, 3, open_24h=90.0)             # crypto-style 24h reference
        self.assertAlmostEqual(tel.quote("SPY").net_pct, 10.0)


try:
    import tkinter as _tk
    _r = _tk.Tk(); _r.destroy()
    _DISPLAY = True
except Exception:
    _DISPLAY = False


@unittest.skipUnless(_DISPLAY, "no display")
class TestChartWidget(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from quantecho.chart import ChartCanvas
        self.root = tk.Tk()
        self.root.geometry("800x420+30+30")
        self.c = ChartCanvas(self.root, max_fps=1000)
        self.c.min_full_s = 0
        self.c.pack(fill="both", expand=True)
        self.root.update()
        closes = [100 + (i % 7) * 0.3 for i in range(150)]
        self.bars = bars_from(closes)
        self.c.set_data("SPY", self.bars)
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def test_in_range_tick_is_incremental(self):
        from quantecho.chart import apply_tick
        full0, inc0 = self.c.stats["full"], self.c.stats["inc"]
        mid = (self.c._geom["hi"] + self.c._geom["lo"]) / 2
        live = apply_tick(self.bars, mid, self.bars[-1]["t"] + 20_000, size=1)
        self.c.set_data("SPY", live)
        self.assertEqual(self.c.stats["full"], full0)                    # no full redraw
        self.assertEqual(self.c.stats["inc"], inc0 + 1)
        x1 = self.c.coords("last_text")
        self.assertEqual(self.c.itemcget("last_text", "text"), f"{mid:,.2f}")
        self.assertTrue(x1)

    def test_out_of_range_or_new_bar_redraws(self):
        from quantecho.chart import apply_tick
        full0 = self.c.stats["full"]
        spike = apply_tick(self.bars, self.c._geom["hi"] * 1.05, self.bars[-1]["t"] + 20_000)
        self.c.set_data("SPY", spike)
        self.assertEqual(self.c.stats["full"], full0 + 1)               # rescaled
        nxt = apply_tick(spike, 100.0, spike[-1]["t"] + 60_000)
        self.c.set_data("SPY", nxt)
        self.assertEqual(self.c.stats["full"], full0 + 2)               # new candle -> full redraw

    def test_zoom_pan_live(self):
        self.c._zoom(1 / 1.15)
        self.assertLess(self.c.bars_shown, 120)
        self.c.bars_shown = 60
        self.c._full(force=True)
        self.c._drag = (400, 0)

        class E:
            x = 400 + 20 * self.c._geom["step"]
        self.c._drag_move(E)
        self.assertEqual(self.c.offset, 20)                              # dragged 20 bars back in time
        self.assertTrue([i for i in self.c.find_all() if "LIVE" in (self.c.itemcget(i, "text") if self.c.type(i) == "text" else "")])
        self.c.go_live()
        self.assertEqual((self.c.offset, self.c.bars_shown), (0, 120))


if __name__ == "__main__":
    unittest.main()
