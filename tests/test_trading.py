"""Trading package tests. No network, no real accounts: tastytrade is a fake HTTP server, SIM is local."""
import json
import queue
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from quantecho.chart import compute_series, find_signals
from quantecho.trading.broker import SimBroker, TastyBroker
from quantecho.trading.occ import OptionContract, occ_symbol, parse_occ
from quantecho.trading.pricing import bs_price, model_quote
from quantecho.trading.risk import DayState, RiskLimits, check_entry, size_position
from quantecho.trading.strategy import StrategyParams, choose_expiry, choose_strike, exit_reason
from quantecho.trading.tasty import (OrderUncertain, TastytradeClient, build_option_order, parse_nested_chain)

NY = ZoneInfo("America/New_York")


class TestOCC(unittest.TestCase):
    def test_format_matches_docs(self):
        self.assertEqual(occ_symbol("SPY", date(2022, 11, 18), "C", 400), "SPY   221118C00400000")
        self.assertEqual(occ_symbol("AAPL", date(2022, 6, 17), "P", 150), "AAPL  220617P00150000")
        self.assertEqual(occ_symbol("X", date(2026, 1, 2), "C", 1050.55)[-8:], "01050550")
        self.assertEqual(occ_symbol("X", date(2026, 1, 2), "C", 0.5)[-8:], "00000500")

    def test_roundtrip(self):
        c = OptionContract("NVDA", date(2026, 11, 27), "P", 185.5)
        self.assertEqual(parse_occ(c.symbol), c)
        with self.assertRaises(ValueError):
            parse_occ("not an option")


class TestOrderJson(unittest.TestCase):
    def test_buy_is_debit_sell_is_credit(self):
        sym = occ_symbol("SPY", date(2026, 11, 20), "C", 600)
        o = build_option_order(sym, "Buy to Open", 2, 1.234, "qe-1")
        self.assertEqual((o["order-type"], o["price"], o["price-effect"], o["external-identifier"]),
                         ("Limit", "1.23", "Debit", "qe-1"))
        self.assertEqual(o["legs"], [{"instrument-type": "Equity Option", "symbol": sym, "quantity": 2,
                                      "action": "Buy to Open"}])
        self.assertEqual(build_option_order(sym, "Sell to Close", 1, 2.0, "x")["price-effect"], "Credit")

    def test_long_only_and_validation(self):
        sym = occ_symbol("SPY", date(2026, 11, 20), "C", 600)
        for bad in ("Sell to Open", "Buy to Close"):
            with self.assertRaises(ValueError):
                build_option_order(sym, bad, 1, 1.0, "x")
        with self.assertRaises(ValueError):
            build_option_order(sym, "Buy to Open", 0, 1.0, "x")
        with self.assertRaises(ValueError):
            build_option_order(sym, "Buy to Open", 1, 0, "x")


class FakeResp:
    def __init__(self, status, payload=None):
        self.status_code, self._p = status, payload or {}
        self.text, self.content, self.headers = json.dumps(self._p), b"x", {}

    def json(self):
        return self._p


class FakeTasty:
    """Minimal fake of the tastytrade REST API."""

    def __init__(self):
        self.calls = []
        self.orders = {}
        self.submit_behaviour = "ok"     # ok | timeout_but_placed | timeout_lost
        self.dry_run_warnings = []
        self.token_requests = 0

    def post(self, url, json=None, headers=None):
        self.calls.append(("POST", url, json))
        self.token_requests += 1
        assert headers and headers.get("User-Agent"), "User-Agent is mandatory"
        assert json == {"grant_type": "refresh_token", "refresh_token": "RT", "client_secret": "CS"}
        return FakeResp(200, {"access_token": f"AT{self.token_requests}", "expires_in": 900})

    def request(self, method, url, json=None, params=None, headers=None):
        import httpx
        self.calls.append((method, url, json))
        assert headers["Authorization"].startswith("Bearer AT")
        path = url.split(".com", 1)[1]
        if path == "/customers/me/accounts":
            return FakeResp(200, {"data": {"items": [{"account": {"account-number": "5WT0001"}}]}})
        if path.endswith("/orders/dry-run"):
            return FakeResp(201, {"data": {"order": {"status": "Received"}, "warnings": self.dry_run_warnings,
                                           "errors": []}})
        if path.endswith("/orders") and method == "POST":
            oid = len(self.orders) + 1
            o = {"id": oid, "status": "Live", "external-identifier": json["external-identifier"], "legs": json["legs"]}
            if self.submit_behaviour != "timeout_lost":
                self.orders[oid] = o
            if self.submit_behaviour.startswith("timeout"):
                raise httpx.ReadTimeout("read timed out")
            return FakeResp(201, {"data": {"order": o}})
        if path.endswith("/orders/live") or (path.endswith("/orders") and method == "GET"):
            return FakeResp(200, {"data": {"items": list(self.orders.values())}})
        if "/option-chains/" in path:
            return FakeResp(200, {"data": {"items": [{"underlying-symbol": "SPY", "expirations": [
                {"expiration-date": "2026-11-18", "days-to-expiration": 7, "strikes": [
                    {"strike-price": "600.0", "call": "SPY   261118C00600000", "put": "SPY   261118P00600000"},
                    {"strike-price": "601.0", "call": "SPY   261118C00601000", "put": "SPY   261118P00601000"}]}]}]}})
        return FakeResp(404, {"error": {"code": "not_found", "message": path}})


class TestTastyClient(unittest.TestCase):
    def client(self, fake):
        return TastytradeClient("sandbox", "CS", "RT", http=fake)

    def test_token_and_account(self):
        f = FakeTasty()
        c = self.client(f)
        self.assertEqual(c.ensure_account(), "5WT0001")
        self.assertEqual(f.token_requests, 1)
        c.ensure_account(); c.option_chain("SPY")
        self.assertEqual(f.token_requests, 1)                       # token reused until near expiry
        self.assertTrue(f.calls[1][1].startswith("https://api.cert.tastyworks.com"))

    def test_chain_parse(self):
        ex = self.client(FakeTasty()).option_chain("SPY")
        self.assertEqual((ex[0].expiry, ex[0].dte, sorted(ex[0].strikes)), (date(2026, 11, 18), 7, [600.0, 601.0]))
        self.assertEqual(ex[0].strikes[600.0]["C"], "SPY   261118C00600000")
        self.assertEqual(parse_nested_chain({}), [])

    def test_timeout_is_never_resent(self):
        f = FakeTasty()
        f.submit_behaviour = "timeout_but_placed"
        c = self.client(f)
        order = build_option_order("SPY   261118C00600000", "Buy to Open", 1, 1.5, "qe-abc")
        got = c.place_order(order)
        self.assertEqual(got["external-identifier"], "qe-abc")      # found by its identifier
        posts = [x for x in f.calls if x[0] == "POST" and x[1].endswith("/orders")]
        self.assertEqual(len(posts), 1)                              # submitted exactly once

    def test_lost_order_raises_uncertain_not_resend(self):
        f = FakeTasty()
        f.submit_behaviour = "timeout_lost"
        c = self.client(f)
        import quantecho.trading.tasty as t
        orig = t.time.sleep
        t.time.sleep = lambda s: None
        try:
            with self.assertRaises(OrderUncertain):
                c.place_order(build_option_order("SPY   261118C00600000", "Buy to Open", 1, 1.5, "qe-lost"))
        finally:
            t.time.sleep = orig
        self.assertEqual(len([x for x in f.calls if x[0] == "POST" and x[1].endswith("/orders")]), 1)

    def test_dry_run_warning_blocks_submit(self):
        f = FakeTasty()
        f.dry_run_warnings = [{"code": "x", "message": "insufficient buying power"}]
        b = TastyBroker(self.client(f))
        r = b.submit(parse_occ("SPY   261118C00600000"), "Buy to Open", 1, 1.5, "qe-dr")
        self.assertEqual(r.status, "rejected")
        self.assertIn("insufficient buying power", r.message)
        self.assertFalse([x for x in f.calls if x[0] == "POST" and x[1].endswith("/orders")])  # never submitted

    def test_secrets_never_in_errors(self):
        f = FakeTasty()
        c = self.client(f)
        self.assertEqual(c._redact("bad CS and RT here"), "bad *** and *** here")


class TestPricingRiskStrategy(unittest.TestCase):
    def test_bs_put_call_parity_and_quote(self):
        import math
        S, K, T, iv, r = 100, 100, 30 / 365, 0.2, 0.04
        c, p = bs_price(S, K, T, iv, True, r), bs_price(S, K, T, iv, False, r)
        self.assertAlmostEqual(c - p, S - K * math.exp(-r * T), places=6)
        q = model_quote(OptionContract("SPY", date(2026, 11, 18), "C", 600), 600,
                        now=datetime(2026, 11, 11, 11, 0, tzinfo=NY))
        self.assertTrue(q.bid < q.mark < q.ask and q.modeled and q.spread_pct < 0.05)

    def test_risk_blocks(self):
        L, st = RiskLimits(), DayState()
        wed11 = datetime(2026, 11, 11, 11, 0, tzinfo=NY)
        self.assertEqual(check_entry(L, st, "SPY", [], 1.0, 0.02, 0, wed11), (True, []))
        ok, why = check_entry(L, st, "TSLA", [], 1.0, 0.02, 0, datetime(2026, 11, 11, 9, 35, tzinfo=NY))
        self.assertFalse(ok)
        self.assertTrue(any("allowlist" in w for w in why) and any("entry window" in w for w in why))
        self.assertFalse(check_entry(L, st, "SPY", ["SPY"], 1.0, 0.02, 0, wed11)[0])           # already holding
        self.assertFalse(check_entry(L, st, "SPY", [], 5.0, 0.02, 0, wed11)[0])                # $500 > $300
        self.assertFalse(check_entry(L, st, "SPY", [], 1.0, 0.20, 0, wed11)[0])                # wide spread
        self.assertFalse(check_entry(L, st, "SPY", [], 1.0, 0.02, -400, wed11)[0])             # daily loss
        self.assertEqual((size_position(L, 1.0), size_position(L, 1.6), size_position(L, 3.5)), (2, 1, 0))

    def test_strategy_choices_and_exits(self):
        p = StrategyParams()
        today = date(2026, 11, 11)
        exps = [date(2026, 11, d) for d in (11, 12, 13, 16, 17, 18, 19, 20, 25)]
        self.assertEqual(choose_expiry(exps, today, p), date(2026, 11, 18))
        self.assertEqual(choose_strike([598, 599, 600, 601, 602], 600.4, "C", p), 600)
        self.assertEqual(choose_strike([598, 599, 600, 601, 602], 600.4, "C", StrategyParams(strikes_otm=1)), 601)
        self.assertEqual(choose_strike([598, 599, 600, 601, 602], 600.4, "P", StrategyParams(strikes_otm=1)), 599)
        c = OptionContract("SPY", date(2026, 11, 18), "C", 600)
        t0, now = datetime(2026, 11, 11, 10, tzinfo=NY), datetime(2026, 11, 11, 11, tzinfo=NY)
        self.assertIn("take profit", exit_reason(1.0, 1.6, c, t0, now, None, p))
        self.assertIn("stop loss", exit_reason(1.0, 0.55, c, t0, now, None, p))
        self.assertIsNone(exit_reason(1.0, 1.1, c, t0, now, None, p))
        from quantecho.chart import Signal
        self.assertIn("opposite", exit_reason(1.0, 1.1, c, t0, now, Signal(5, "sell", "trend", "x"), p))
        c1 = OptionContract("SPY", date(2026, 11, 12), "C", 600)
        self.assertIn("flatten", exit_reason(1.0, 1.1, c1, t0, datetime(2026, 11, 11, 15, 50, tzinfo=NY), None, p))


def rth_bars(closes, day=date(2026, 11, 11)):
    t0 = int(datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY).timestamp() * 1000)
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append({"t": t0 + i * 60_000, "open": prev, "high": max(prev, c) + 0.05, "low": min(prev, c) - 0.05,
                    "close": c, "volume": 1000})
        prev = c
    return out


class FakeData:
    def __init__(self, bars):
        self.bars = {"SPY": bars, "NVDA": []} if isinstance(bars, list) else dict(bars)

    def get_session_minute_bars(self, sym):
        return list(self.bars.get(sym, []))


class TestBotEndToEnd(unittest.TestCase):
    def setUp(self):
        closes = [580.0] * 30 + [580 - (i + 1) * 0.2 for i in range(12)]
        closes += [closes[-1] + (i + 1) * 0.3 for i in range(30)]     # flat, dip, rally -> trend BUY above VWAP
        full = rth_bars(closes)
        sig = next(s for s in find_signals(full, compute_series(full)) if s.side == "buy" and s.kind == "trend")
        self.bars = full[: sig.i + 2]               # the signal bar is the one that JUST closed
        self.now = datetime(2026, 11, 11, 11, 0, tzinfo=NY)
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **kw):
        from quantecho.trading.bot import TradingBot
        self.data = FakeData(self.bars)
        limits = RiskLimits(max_premium_per_trade=1200)   # a 7-DTE ATM SPY call models at ~$5 (~$500/contract)
        return TradingBot(SimBroker(), self.data, limits, StrategyParams(), queue.Queue(),
                          journal_dir=Path(self.tmp.name), clock=lambda: self.now, bars_every_s=0, **kw)

    def test_signal_entry_fill_takeprofit_journal(self):
        bot = self.make()
        bot.step()                                         # signal -> entry order (working)
        self.assertEqual(len(bot.pending), 1)
        bot.step()                                         # limit >= mid -> filled
        self.assertIn("SPY", bot.positions)
        pos = bot.positions["SPY"]
        self.assertTrue(pos.contract.is_call and pos.qty >= 1 and pos.entry_price * pos.qty * 100 <= 1200)
        last = self.data.bars["SPY"][-1]
        self.data.bars["SPY"][-1] = {**last, "close": last["close"] * 1.03, "high": last["close"] * 1.03}
        bot.step()                                         # mark way up -> take-profit exit order
        bot.step()                                         # exit fills
        self.assertNotIn("SPY", bot.positions)
        self.assertGreater(bot.state.realized_pnl, 0)
        events = [json.loads(x)["event"] for x in (Path(self.tmp.name) / "2026-11-11.jsonl").read_text().splitlines()]
        for e in ("signal", "entry_order", "filled_entry", "exit_order", "filled_exit"):
            self.assertIn(e, events)
        bot.step()
        self.assertEqual(bot.state.entries, 1)            # same signal never trades twice

    def test_kill_switch_closes_and_halts(self):
        bot = self.make()
        bot.step(); bot.step()
        self.assertIn("SPY", bot.positions)
        bot.kill()
        bot.step()
        self.assertTrue(bot.state.halted)
        self.assertNotIn("SPY", bot.positions)

    def test_declined_confirmation_places_nothing(self):
        asked = []
        bot = self.make(confirm=lambda text: asked.append(text) or False)
        bot.step()
        self.assertEqual((len(asked), len(bot.pending), len(bot.positions)), (1, 0, 0))
        self.assertIn("BUY TO OPEN", asked[0])

    def test_live_requires_confirmation(self):
        from quantecho.trading.bot import TradingBot

        class LiveLike(SimBroker):
            real_money = True
        with self.assertRaises(ValueError):
            TradingBot(LiveLike(), FakeData(self.bars), RiskLimits(), StrategyParams(), queue.Queue())

    def test_default_budget_blocks_expensive_contract(self):
        from quantecho.trading.bot import TradingBot
        bot = TradingBot(SimBroker(), FakeData(self.bars), RiskLimits(), StrategyParams(), queue.Queue(),
                         journal_dir=Path(self.tmp.name), clock=lambda: self.now, bars_every_s=0)
        bot.step()
        self.assertEqual((len(bot.pending), len(bot.positions)), (0, 0))
        log = (Path(self.tmp.name) / "2026-11-11.jsonl").read_text()
        self.assertIn("exceeds max premium", log)

    def test_outside_window_blocked(self):
        self.now = datetime(2026, 11, 11, 15, 50, tzinfo=NY)
        bot = self.make()
        bot.step()
        self.assertEqual((len(bot.pending), len(bot.positions)), (0, 0))


class TestBotConfluence(unittest.TestCase):
    def test_confluence_buys_nvda_call_only(self):
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from test_confluence import scenario
        from quantecho.confluence import ConfluenceRule, evaluate
        from quantecho.trading.bot import TradingBot
        full = scenario()
        st = evaluate(full, ConfluenceRule())
        buy = next(s for s in st.signals if s.side == "buy")
        cut = st.timeline[buy.i + 1]                     # keep one forming bar after the signal bar
        data = FakeData({})
        data.bars = {sym: [b for b in bars if b["t"] <= cut] for sym, bars in full.items()}
        tmp = tempfile.TemporaryDirectory()
        try:
            bot = TradingBot(SimBroker(), data, RiskLimits(allowed_symbols=["SPY", "NVDA"], max_premium_per_trade=1500),
                             StrategyParams(signal_source="confluence"), queue.Queue(), journal_dir=Path(tmp.name),
                             clock=lambda: datetime(2026, 11, 11, 11, 0, tzinfo=NY), bars_every_s=0)
            bot.step()
            bot.step()
            self.assertIn("NVDA", bot.positions)
            self.assertTrue(bot.positions["NVDA"].contract.is_call)
            self.assertNotIn("SPY", bot.positions)        # only the confluence target trades
            log = (Path(tmp.name) / "2026-11-11.jsonl").read_text(encoding="utf-8")
            self.assertIn("confluence", log)
            bot.step()
            self.assertEqual(bot.state.entries, 1)        # same confluence signal never trades twice
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
