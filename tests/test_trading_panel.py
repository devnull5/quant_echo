"""Trading panel GUI tests (SIM only; no network, no real accounts, never LIVE)."""
import threading
import time
import unittest
from unittest import mock

import quantecho.config as cfg

try:
    import tkinter as tk

    _r = tk.Tk()
    _r.destroy()
    HAVE_DISPLAY = True
except Exception:
    HAVE_DISPLAY = False


class _MemKeyring:
    def __init__(self):
        self.d = {}

    def get_password(self, s, n):
        return self.d.get((s, n))

    def set_password(self, s, n, v):
        self.d[(s, n)] = v

    def delete_password(self, s, n):
        self.d.pop((s, n), None)


@unittest.skipUnless(HAVE_DISPLAY, "no display available for Tk")
class TestTradingPanel(unittest.TestCase):
    def setUp(self):
        cfg._keyring = _MemKeyring()
        import tempfile
        import quantecho.trading.bot as botmod
        self._tmp = tempfile.TemporaryDirectory()
        self._journal = mock.patch.object(botmod, "JOURNAL_DIR", __import__("pathlib").Path(self._tmp.name))
        self._journal.start()                                   # never write to the real trade journal
        from quantecho import gui
        self.gui = gui
        self.settings = cfg.Settings(provider="simulated", symbols="SPY NVDA", focus="SPY", interval=1,
                                     first_run_done=True, ask_source_on_launch=False, ai_backend="ollama")
        self.patches = [mock.patch.object(gui, "load_settings", return_value=self.settings),
                        mock.patch.object(gui, "save_settings", return_value=[]),
                        mock.patch.object(gui.llm, "preload"),
                        mock.patch.object(gui.llm, "list_models", return_value=[])]
        for p in self.patches:
            p.start()
        self.app = gui.App()
        self.pump(0.2)

    def tearDown(self):
        import gc
        panel = self.app.trading_panel
        if panel is not None and panel.bot is not None:
            panel.bot.stop()
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            try:
                self.app._on_close()
            except Exception:
                pass
        for t in threading.enumerate():
            if t.name in ("quantecho-poller", "quantecho-ticks", "quantecho-bot"):
                t.join(timeout=8)
        del self.app
        gc.collect()
        for p in self.patches:
            p.stop()
        self._journal.stop()
        self._tmp.cleanup()

    def pump(self, s):
        end = time.monotonic() + s
        while time.monotonic() < end:
            self.app.update()
            time.sleep(0.02)

    def open(self):
        self.app.open_trading()
        self.pump(0.2)
        return self.app.trading_panel

    def test_button_and_panel(self):
        self.assertTrue(self.gui._trading_available())
        p = self.open()
        self.assertTrue(p.winfo_viewable())
        self.assertEqual(p.v["mode"].get(), "sim")
        L, P = p.limits_and_params()
        self.assertEqual((L.allowed_symbols, L.max_premium_per_trade, P.target_dte), (["SPY", "NVDA"], 300.0, 7))
        p.v["min_dte"].set("10")
        with self.assertRaises(ValueError):
            p.limits_and_params()                              # Min <= Target violated

    def test_sim_start_status_stop(self):
        p = self.open()
        p.v["confirm_each"].set(False)
        p.on_start()
        self.assertTrue(p.bot and p.bot.thread.is_alive())
        self.assertTrue(self.wait(lambda: "SIM" in p.v_status.get() and "RUNNING" in p.v_status.get(), 10))
        self.assertIn("Bot started", p.text.get("1.0", "end"))
        p.on_stop()
        p.bot.thread.join(timeout=8)
        self.assertFalse(p.bot.thread.is_alive())

    def wait(self, cond, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.update()
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_confirmation_roundtrip_and_timeout(self):
        import quantecho.trading.panel as panel_mod
        p = self.open()
        confirm = p._confirm_fn()
        result = {}
        with mock.patch("tkinter.messagebox.askyesno", return_value=True) as ask:
            th = threading.Thread(target=lambda: result.setdefault("a", confirm("BUY TO OPEN 1x SPY test")))
            th.start()
            self.assertTrue(self.wait(lambda: "a" in result, 5))
            self.assertTrue(result["a"])
            self.assertIn("BUY TO OPEN", ask.call_args[0][1])
        old = panel_mod.CONFIRM_TIMEOUT_S
        panel_mod.CONFIRM_TIMEOUT_S = 0.3
        try:
            self.app.trading_panel = None                     # no window to answer -> declined
            self.assertFalse(confirm("BUY TO OPEN"))
        finally:
            panel_mod.CONFIRM_TIMEOUT_S = old
            self.app.trading_panel = p

    def test_live_guards(self):
        p = self.open()
        p.v["mode"].set("live")
        with mock.patch("tkinter.messagebox.showerror") as err:
            p.on_start()                                       # no live credentials
            self.assertIn("required", err.call_args[0][1])
        self.assertIsNone(p.bot)
        p.v_secret["tasty_live_secret"].set("CS")
        p.v_secret["tasty_live_refresh"].set("RT")
        with mock.patch("tkinter.simpledialog.askstring", return_value="yes please"):
            p.on_start()                                       # wrong phrase
        self.assertIsNone(p.bot)
        self.assertIn("LIVE start cancelled", p.text.get("1.0", "end"))

    def test_refuses_crypto_source(self):
        self.app.v_provider.set("Coinbase (crypto, no key)")
        p = self.open()
        with mock.patch("tkinter.messagebox.showerror") as err:
            p.on_start()
        self.assertIn("STOCK data source", err.call_args[0][1])
        self.assertIsNone(p.bot)

    def test_close_with_open_positions_asks(self):
        p = self.open()

        class FakeBot:
            positions = {"SPY": object()}
            thread = None

            def stop(self):
                pass
        p.bot = FakeBot()
        with mock.patch("tkinter.messagebox.askyesno", return_value=False) as ask:
            self.app._on_close()
        self.assertIn("open position", ask.call_args[0][1])
        self.assertTrue(self.app.winfo_exists())               # user said no -> app stays open
        p.bot = None

    def test_credentials_go_to_keyring_only(self):
        p = self.open()
        p.v_secret["tasty_sandbox_secret"].set("SANDBOX-SECRET")
        p.save()
        self.assertEqual(cfg._keyring.d[("quantecho", "tasty_sandbox_secret")], "SANDBOX-SECRET")
        self.assertNotIn("SANDBOX-SECRET", str(self.app.settings.trading))

    def test_saved_trading_settings_survive_main_save(self):
        p = self.open()
        p.v["max_premium"].set("5000")
        p.v["trailing"].set(False)
        p.save()
        self.app.on_save()                       # the main window's own save used to drop them
        self.assertEqual(str(self.app.settings.trading["max_premium"]), "5000")
        self.assertFalse(self.app.settings.trading["trailing"])


if __name__ == "__main__":
    unittest.main()
