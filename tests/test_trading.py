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
        ok, why = check_entry(L, st, "TSLA", [], 1.0, 0.02, 0, datetime(2026, 11, 11, 9, 29, tzinfo=NY))
        self.assertFalse(ok)
        self.assertTrue(any("allowlist" in w for w in why) and any("entry window" in w for w in why))
        self.assertTrue(check_entry(L, st, "SPY", [], 1.0, 0.02, 0, datetime(2026, 11, 11, 9, 30, tzinfo=NY))[0])  # open
        self.assertFalse(check_entry(L, st, "SPY", ["SPY"], 1.0, 0.02, 0, wed11)[0])           # already holding
        self.assertFalse(check_entry(L, st, "SPY", [], 5.0, 0.02, 0, wed11)[0])                # $500 > $300
        self.assertFalse(check_entry(L, st, "SPY", [], 1.0, 0.20, 0, wed11)[0])                # wide spread
        self.assertFalse(check_entry(L, st, "SPY", [], 1.0, 0.02, -400, wed11)[0])             # daily loss
        self.assertEqual((size_position(L, 1.0), size_position(L, 1.6), size_position(L, 3.5)), (2, 1, 0))

    def test_strategy_choices_and_exits(self):
        p = StrategyParams(take_profit_pct=0.5, stop_loss_pct=0.4)
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

    def make(self, params=None, bars=None, **kw):
        from quantecho.trading.bot import TradingBot
        self.data = FakeData(bars if bars is not None else self.bars)
        limits = RiskLimits(max_premium_per_trade=1200)   # a 7-DTE ATM SPY call models at ~$5 (~$500/contract)
        return TradingBot(SimBroker(), self.data, limits, params or StrategyParams(), queue.Queue(),
                          journal_dir=Path(self.tmp.name), clock=lambda: self.now, bars_every_s=0, **kw)

    def test_signal_entry_fill_takeprofit_journal(self):
        bot = self.make(params=StrategyParams(take_profit_pct=0.5))
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
        # P&L ledger: one row per closed trade; a restarted bot reloads today's and lifetime P&L
        import csv
        rows = list(csv.DictReader((Path(self.tmp.name) / "pnl_ledger.csv").open(encoding="utf-8")))
        self.assertEqual(len(rows), 1)
        self.assertIn("take profit", rows[0]["reason"])
        pnl = float(rows[0]["pnl"])
        self.assertAlmostEqual(bot.status().lifetime_realized, pnl)
        again = self.make()
        self.assertAlmostEqual(again.lifetime_pnl, pnl)
        self.assertAlmostEqual(again.state.realized_pnl, pnl)       # daily loss limit survives a restart
        self.now = datetime(2026, 11, 12, 11, 0, tzinfo=NY)
        tomorrow = self.make()
        self.assertAlmostEqual(tomorrow.lifetime_pnl, pnl)
        self.assertEqual(tomorrow.state.realized_pnl, 0)

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


class TestJoinTrendOnStart(unittest.TestCase):
    """Started after the crossover: the bot joins an established trend once, inside the entry window."""
    make, tearDown = TestBotEndToEnd.make, TestBotEndToEnd.tearDown

    def setUp(self):
        TestBotEndToEnd.setUp(self)
        closes = [580.0] * 30 + [580 - (i + 1) * 0.2 for i in range(12)]
        closes += [closes[-1] + (i + 1) * 0.3 for i in range(30)]
        self.late = rth_bars(closes)                 # crossover is ~10+ bars back; nothing fires on the last bar

    def test_joins_uptrend_once(self):
        bot = self.make(bars=self.late)
        bot.step()
        self.assertEqual(len(bot.pending), 1)
        self.assertTrue(next(iter(bot.pending.values())).contract.is_call)
        self.assertIn("already in an uptrend", bot._last_sig["SPY"].reason)
        bot.step()                                   # fills
        self.assertIn("SPY", bot.positions)
        # only one join per run:
        bot.positions.clear(); bot.pending.clear()
        bot.step()
        self.assertFalse(bot.pending)

    def test_off_waits_for_crossover(self):
        bot = self.make(params=StrategyParams(join_trend_on_start=False), bars=self.late)
        bot.step()
        self.assertFalse(bot.pending)

    def test_waits_for_entry_window(self):
        bot = self.make(bars=self.late)
        self.now = datetime(2026, 11, 11, 9, 0, tzinfo=NY)
        bot.step()
        self.assertFalse(bot.pending)                # pre-market: not used up
        self.now = datetime(2026, 11, 11, 11, 0, tzinfo=NY)
        bot.step()
        self.assertEqual(len(bot.pending), 1)


class TestDynamicExit(unittest.TestCase):
    def test_trail_and_fade_rules(self):
        from quantecho.trading.strategy import fade_reason, trail_armed, trail_reason
        p = StrategyParams(trail_arm_pct=0.15, trail_giveback_pct=0.05)
        self.assertFalse(trail_armed(1.0, 1.14, p))
        self.assertIsNone(trail_reason(1.0, 1.05, 1.14, p))     # never armed -> the plain stop handles it
        self.assertIsNone(trail_reason(1.0, 1.11, 1.15, p))     # +15% -> +11%: within 5 points
        self.assertIn("trailing stop", trail_reason(1.0, 1.10, 1.15, p))
        self.assertIn("trailing stop", trail_reason(1.0, 1.35, 1.40, p))   # ratchets with the peak
        self.assertIsNone(trail_reason(1.0, 1.0, 1.5, StrategyParams(trail_giveback_pct=0)))  # off
        self.assertIsNone(trail_reason(1.0, 1.0, 1.5, StrategyParams(trailing_stop=False)))   # box unticked
        d = StrategyParams()                                     # defaults: arm +16%, give back 3, stop -5%, no TP
        self.assertIsNone(trail_reason(1.0, 1.10, 1.15, d))      # +15% never armed
        self.assertIsNone(trail_reason(1.0, 1.14, 1.16, d))      # +16% -> +14%: 2 points
        self.assertIn("trailing stop", trail_reason(1.0, 1.13, 1.16, d))
        c0 = OptionContract("SPY", date(2026, 11, 18), "C", 600)
        t0, t1 = datetime(2026, 11, 11, 10, tzinfo=NY), datetime(2026, 11, 11, 11, tzinfo=NY)
        self.assertIsNone(exit_reason(1.0, 3.0, c0, t0, t1, None, d))              # no hard take-profit
        self.assertIsNone(exit_reason(1.0, 0.96, c0, t0, t1, None, d))
        self.assertIn("stop loss", exit_reason(1.0, 0.95, c0, t0, t1, None, d))
        call = OptionContract("SPY", date(2026, 11, 18), "C", 600)
        put = OptionContract("SPY", date(2026, 11, 18), "P", 600)
        self.assertIn("faded", fade_reason(call, 599.0, 600.0))
        self.assertIsNone(fade_reason(call, 601.0, 600.0))
        self.assertIn("faded", fade_reason(put, 601.0, 600.0))
        self.assertIsNone(fade_reason(put, 601.0, None))

    def test_bot_trails_after_two_confirmations(self):
        from types import SimpleNamespace
        t = TestBotEndToEnd()
        t.setUp()
        try:
            bot = t.make()
            bot.step(); bot.step()                               # enter + fill
            pos = bot.positions["SPY"]
            e = pos.entry_price
            mark = {"m": e * 1.20}
            bot.broker.quote = lambda c, spot: SimpleNamespace(bid=mark["m"] - 0.01, ask=mark["m"] + 0.01,
                                                               modeled=True)
            bot.step()                                           # +20%: armed, holding
            self.assertFalse(pos.closing)
            mark["m"] = e * 1.12
            bot.step()                                           # 8 points off the peak: 1st confirmation
            self.assertFalse(pos.closing)
            bot.step()                                           # 2nd -> sell
            self.assertTrue(pos.closing)
            lines = (Path(t.tmp.name) / "2026-11-11.jsonl").read_text().splitlines()
            ex = [json.loads(x) for x in lines if '"exit_order"' in x]
            self.assertIn("trailing stop", ex[-1]["reason"])
        finally:
            t.tearDown()


class TestExitSafety(unittest.TestCase):
    """Regressions from the 2026-11-12 sandbox run: an unfilled exit was cancelled and re-sent every ~27 s
    for hours (incl. after the close), each new order rejected because the old cancel hadn't landed."""
    def setUp(self):
        self.t = TestBotEndToEnd()
        self.t.setUp()
        self.bot = self.t.make()
        self.bot.step(); self.bot.step()                      # enter + fill
        self.pos = self.bot.positions["SPY"]

    def tearDown(self):
        self.t.tearDown()

    def events(self):
        lines = (Path(self.t.tmp.name) / "2026-11-11.jsonl").read_text().splitlines()
        return [json.loads(x) for x in lines]

    def test_waits_for_cancel_confirmation_before_resending(self):
        import quantecho.trading.bot as botmod
        b, sent = self.bot.broker, []
        real_submit = b.submit
        b.submit = lambda *a: (sent.append(a), real_submit(*a))[1]
        from quantecho.trading.broker import OrderResult
        b.status = lambda oid, c, spot: OrderResult("working", oid)
        b.cancel = lambda oid: None                            # broker never confirms the cancel
        self.pos.urgent_exit, self.pos.exit_why = True, "stop loss (-45%)"
        old = botmod.EXIT_TIMEOUT_S
        botmod.EXIT_TIMEOUT_S = 0
        try:
            for _ in range(6):
                self.bot.step()
        finally:
            botmod.EXIT_TIMEOUT_S = old
        self.assertEqual(len(sent), 1)                         # one exit order, no spam
        ev = self.events()
        self.assertTrue(any(e["event"] == "exit_timeout_cancel_requested" for e in ev))
        self.assertIn("stop loss", [e for e in ev if e["event"] == "exit_order"][-1]["reason"])

    def test_order_error_backs_off(self):
        calls = []

        def boom(*a):
            calls.append(a)
            raise RuntimeError("HTTP 422 cannot_close_against_more_than_existing_position")
        self.bot.broker.submit = boom
        self.pos.urgent_exit, self.pos.exit_why = True, "kill switch"
        for _ in range(5):
            self.bot.step()
        self.assertEqual(len(calls), 1)

    def test_no_orders_after_the_close(self):
        sent = []
        self.bot.broker.submit = lambda *a: sent.append(a)
        self.pos.urgent_exit = True
        self.t.now = datetime(2026, 11, 11, 16, 30, tzinfo=NY)
        for _ in range(3):
            self.bot.step()
        self.assertEqual(sent, [])
        self.assertEqual(sum(e["event"] == "market_closed" for e in self.events()), 1)

    def test_flatten_everything_at_1545(self):
        self.t.now = datetime(2026, 11, 11, 15, 46, tzinfo=NY)
        self.bot.step()
        ex = [e for e in self.events() if e["event"] == "exit_order"]
        self.assertIn("end of day", ex[-1]["reason"])

    def test_eod_rule(self):
        c = OptionContract("SPY", date(2026, 11, 20), "C", 600)   # 9 DTE
        t0, late = datetime(2026, 11, 11, 10, tzinfo=NY), datetime(2026, 11, 11, 15, 46, tzinfo=NY)
        self.assertIn("end of day", exit_reason(1.0, 1.0, c, t0, late, None, StrategyParams()))
        self.assertIsNone(exit_reason(1.0, 1.0, c, t0, late, None, StrategyParams(close_all_eod=False)))

    def test_adopts_broker_positions_on_start(self):
        from quantecho.trading.bot import TradingBot
        c = OptionContract("NVDA", date(2026, 11, 16), "C", 165)
        b = SimBroker()
        b.open_positions = lambda allowed: [(c, 2, 3.4)]
        bot = TradingBot(b, FakeData({"SPY": [], "NVDA": []}), RiskLimits(), StrategyParams(), queue.Queue(),
                         journal_dir=Path(self.t.tmp.name), clock=lambda: self.t.now, bars_every_s=0)
        bot._adopt_positions()
        self.assertEqual(bot.positions["NVDA"].qty, 2)
        self.assertEqual(bot.positions["NVDA"].entry_price, 3.4)


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


class TestTickSizes(unittest.TestCase):
    NVDA_RULES_JSON = {"tick-sizes": [{"threshold": "3.0", "value": "0.01"}, {"value": "0.05"}]}  # real sandbox data

    def test_parse_real_tick_sizes(self):
        from quantecho.trading.tasty import parse_tick_sizes
        self.assertEqual(parse_tick_sizes(self.NVDA_RULES_JSON), [(3.0, 0.01), (None, 0.05)])
        self.assertEqual(parse_tick_sizes({"tick-sizes": [{"value": "0.01"}]}), [(None, 0.01)])  # SPY

    def test_rounding(self):
        from quantecho.trading.tasty import parse_tick_sizes, round_to_tick
        r = parse_tick_sizes(self.NVDA_RULES_JSON)
        self.assertEqual(round_to_tick(4.62, r, up=True), 4.65)      # >= $3 -> $0.05 steps, buys round up
        self.assertEqual(round_to_tick(4.62, r, up=False), 4.60)     # sells round down
        self.assertEqual(round_to_tick(2.513, r, up=True), 2.52)     # < $3 -> pennies
        self.assertEqual(round_to_tick(2.999, r, up=True), 3.00)     # crossing the threshold stays valid
        self.assertEqual(round_to_tick(3.00, r, up=True), 3.00)
        self.assertEqual(round_to_tick(0.004, r, up=False), 0.01)    # never below one tick

    def test_brokers_round(self):
        from quantecho.trading.broker import SimBroker
        b = SimBroker()
        nv = OptionContract("NVDA", date(2026, 11, 18), "C", 185)
        spy = OptionContract("SPY", date(2026, 11, 18), "C", 600)
        self.assertEqual(b.round_price(nv, 4.62, buy=True), 4.65)
        self.assertEqual(b.round_price(spy, 4.62, buy=True), 4.62)   # SPY trades in pennies throughout

    def test_tasty_broker_uses_chain_ticks(self):
        f = FakeTasty()
        c = TastytradeClient("sandbox", "CS", "RT", http=f)
        orig = f.request

        def with_ticks(method, url, **kw):
            r = orig(method, url, **kw)
            if "/option-chains/" in url:
                r._p["data"]["items"][0]["tick-sizes"] = [{"value": "0.05"}]
            return r
        f.request = with_ticks
        b = TastyBroker(c)
        b.chain("SPY", 600)
        self.assertEqual(b.round_price(OptionContract("SPY", date(2026, 11, 18), "C", 600), 1.01, buy=True), 1.05)


if __name__ == "__main__":
    unittest.main()
