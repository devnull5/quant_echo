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


if __name__ == "__main__":
    unittest.main()
