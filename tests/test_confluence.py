import unittest

from quantecho.confluence import (DOWN, FLAT, UP, ConfluenceRule, aligned_timeline, directions, evaluate,
                                  signals_on_target)

T0 = 1_793_900_000_000 // 60_000 * 60_000


def bars(closes, start=0):
    return [{"t": T0 + (start + i) * 60_000, "open": c, "high": c, "low": c, "close": c, "volume": 1000}
            for i, c in enumerate(closes)]


def noisy(base, n, seed, drift=0.0, wiggle=0.0005):
    import random
    r, out, p = random.Random(seed), [], base
    for _ in range(n):
        p *= 1 + drift + r.gauss(0, wiggle)
        out.append(p)
    return out


def scenario(nvda_up=True, spy_up=True, qqq_up=True, ym_up=True, dxy="flat", tnx="flat", n_quiet=80, n_move=12):
    """Quiet tape, then a move. dxy/tnx: 'up' | 'down' | 'flat' (TNX via IEF, inverted)."""
    def leg(base, seed, up, scale=1.0):
        quiet = noisy(base, n_quiet, seed, wiggle=0.0004 * scale)
        d = (0.0025 if up else -0.0025) * scale if up is not None else 0.0
        return quiet + noisy(quiet[-1], n_move, seed + 99, drift=d, wiggle=0.0001 * scale)
    m = {"up": True, "down": False, "flat": None}
    ief_dir = {"up": False, "down": True, "flat": None}[tnx]       # yields up => IEF down
    return {"NVDA": bars(leg(185, 1, nvda_up, 3)), "SPY": bars(leg(760, 2, spy_up)), "QQQ": bars(leg(690, 3, qqq_up)),
            "DIA": bars(leg(523, 4, ym_up)), "UUP": bars(leg(27, 5, m[dxy], 0.3)), "IEF": bars(leg(95, 6, ief_dir, 0.3))}


class TestConfluence(unittest.TestCase):
    def test_directions_vol_normalized(self):
        rule = ConfluenceRule()
        flat = noisy(100, 100, 7, wiggle=0.001)
        self.assertEqual(directions(flat, rule)[-1] in (UP, DOWN, FLAT), True)
        up = flat + [flat[-1] * (1 + 0.004 * i) for i in range(1, 8)]
        self.assertEqual(directions(up, rule)[-1], UP)
        down = flat + [flat[-1] * (1 - 0.004 * i) for i in range(1, 8)]
        self.assertEqual(directions(down, rule)[-1], DOWN)

    def test_all_required_up_with_dxy_tnx_flat_gives_buy(self):
        st = evaluate(scenario(), ConfluenceRule())
        self.assertTrue([s for s in st.signals if s.side == "buy"], "expected a BUY")
        self.assertFalse([s for s in st.signals if s.side == "sell"])
        self.assertIn("NVDA▲", st.signals[0].reason)

    def test_dxy_or_tnx_falling_is_fine_for_buy(self):
        self.assertTrue([s for s in evaluate(scenario(dxy="down", tnx="down"), ConfluenceRule()).signals
                         if s.side == "buy"])

    def test_rising_dollar_blocks_buy(self):
        st = evaluate(scenario(dxy="up"), ConfluenceRule())
        self.assertFalse([s for s in st.signals if s.side == "buy"])

    def test_rising_yields_block_buy(self):
        st = evaluate(scenario(tnx="up"), ConfluenceRule())
        self.assertEqual(st.latest()["TNX"], UP)          # IEF fell -> TNX shown rising
        self.assertFalse([s for s in st.signals if s.side == "buy"])

    def test_one_required_leg_disagreeing_blocks(self):
        for kw in ({"spy_up": False}, {"qqq_up": None}, {"ym_up": None}):
            st = evaluate(scenario(**kw), ConfluenceRule())
            self.assertFalse([s for s in st.signals if s.side == "buy"], kw)

    def test_sell_mirror(self):
        st = evaluate(scenario(False, False, False, False, dxy="up", tnx="flat"), ConfluenceRule())
        self.assertTrue([s for s in st.signals if s.side == "sell"])
        self.assertFalse([s for s in st.signals if s.side == "buy"])

    def test_fires_once_per_episode_and_closed_bars_only(self):
        data = scenario()
        st = evaluate(data, ConfluenceRule())
        buys = [s for s in st.signals if s.side == "buy"]
        self.assertEqual(len(buys), 1)                   # move persists for ~12 bars but signals once
        self.assertLess(buys[0].i, len(aligned_timeline(data, ConfluenceRule().symbols)) - 1)

    def test_alignment_and_reindex(self):
        data = scenario()
        data["DIA"] = data["DIA"][3:]                    # proxy missing the first minutes
        st = evaluate(data, ConfluenceRule())
        self.assertEqual(st.timeline[0], data["DIA"][0]["t"])
        on_nvda = signals_on_target(st, data["NVDA"])
        for s in on_nvda:
            self.assertEqual(data["NVDA"][s.i]["t"], st.timeline[[x.i for x in st.signals][0]])

    def test_missing_symbol_no_crash(self):
        data = scenario()
        del data["UUP"]
        st = evaluate(data, ConfluenceRule())
        self.assertEqual((st.timeline, st.signals), ([], []))


if __name__ == "__main__":
    unittest.main()
