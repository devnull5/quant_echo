"""GUI smoke tests: open the real window, drive it with the offline Simulated source, and check what a
user would SEE — not just that data structures are filled. Skipped automatically when no display is
available (CI runs these on Windows/macOS directly and on Linux under xvfb).

These would have caught: the watchlist hidden behind its scrollbar frame, and Start silently doing
nothing while already running.
"""
import time
import unittest
from unittest import mock

import quantecho.config as cfg

try:
    import tkinter as tk

    _root = tk.Tk()
    _root.destroy()
    HAVE_DISPLAY = True
except Exception:  # TclError: no display
    HAVE_DISPLAY = False


class _NullKeyring:
    def get_password(self, *a):
        return None

    def set_password(self, *a):
        pass

    def delete_password(self, *a):
        pass


@unittest.skipUnless(HAVE_DISPLAY, "no display available for Tk")
class TestGuiSmoke(unittest.TestCase):
    def setUp(self):
        cfg._keyring = _NullKeyring()
        from quantecho import gui

        self.gui = gui
        s = cfg.Settings(provider="simulated", symbols="SPY AAPL NVDA MSFT AMZN", focus="SPY",
                         interval=1, first_run_done=True, ask_source_on_launch=False, ai_backend="ollama")
        self.patches = [
            mock.patch.object(gui, "load_settings", return_value=s),
            mock.patch.object(gui, "save_settings", return_value=[]),
            mock.patch.object(gui.llm, "preload"),
            mock.patch.object(gui.llm, "list_models", return_value=[]),
            mock.patch.object(gui.engine.ENGINE, "ensure_running",   # never start a real AI server in tests
                              side_effect=RuntimeError("engine disabled in tests")),
        ]
        for p in self.patches:
            p.start()
        self.app = gui.App()
        self.app.geometry("1220x820+40+40")
        self.pump(0.3)

    def tearDown(self):
        import gc
        import threading
        try:
            self.app._on_close()
        except Exception:
            pass
        # Let this test's polling threads finish, then free its Tk objects HERE on the main thread.
        # Otherwise leftover Tk Variables/Images get garbage-collected later on some background thread;
        # on Python 3.10 each such __del__ makes Tcl wait for the main loop ("main thread is not in main
        # loop"), which can stall the next test's poller for seconds.
        for t in threading.enumerate():
            if t.name == "quantecho-poller":
                t.join(timeout=5)
        del self.app
        gc.collect()
        for p in self.patches:
            p.stop()

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.update()
            time.sleep(0.02)

    def wait_for(self, cond, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.update()
            if cond():
                return True
            time.sleep(0.05)
        return False

    def assert_on_top(self, widget, name):
        """The widget must be what's actually drawn at its centre (nothing stacked over it)."""
        self.app.update_idletasks()
        self.assertTrue(widget.winfo_viewable(), f"{name} is not viewable")
        x = widget.winfo_rootx() + widget.winfo_width() // 2
        y = widget.winfo_rooty() + min(60, widget.winfo_height() // 2)
        hit = self.app.winfo_containing(x, y)
        if hit is None:  # headless desktop without hit-testing: can't verify stacking here
            return
        w = hit
        while w is not None and w is not widget:
            w = w.master
        self.assertIs(w, widget, f"{name} is covered by {hit.winfo_class()} {hit}")

    def test_watchlist_visible_and_populated(self):
        self.app.on_start_clicked()
        self.assertTrue(self.wait_for(lambda: len(self.app.tree.get_children()) == 5), "watchlist never filled")
        self.assert_on_top(self.app.tree, "watchlist table")
        self.assert_on_top(self.app.text, "log panel")
        self.assertGreater(self.app.tree.winfo_height(), 100)
        first = self.app.tree.item(self.app.tree.get_children()[0])
        self.assertEqual(first["text"], "SPY")
        self.assertNotEqual(first["values"][0], "—")  # has a price
        self.assertTrue(self.wait_for(lambda: "updated" in self.app.v_status.get()), "status never updated")

    def test_start_twice_restarts_cleanly(self):
        self.app.on_start_clicked()
        self.assertTrue(self.wait_for(lambda: self.app.tree.get_children()))
        first = self.app.poller
        self.app.on_start_clicked()
        self.pump(0.5)
        self.assertIsNot(self.app.poller, first)
        self.assertTrue(first._stop_evt.is_set())
        self.assertIn("restarting", self.app.text.get("1.0", "end"))
        import threading
        self.assertTrue(self.wait_for(lambda: sum(t.name == "quantecho-poller" and t.is_alive()
                                                  for t in threading.enumerate()) == 1, timeout=5))

    def test_ta_strip_fills(self):
        self.app.on_start_clicked()
        self.assertTrue(self.wait_for(lambda: "VWAP" in self.app.ta_label.cget("text")
                                      and "VWAP —" not in self.app.ta_label.cget("text")), "TA never computed")
        self.assert_on_top(self.app.ta_label, "TA strip")

    def test_source_picker_shows_and_starts(self):
        self.app.source_picker()
        self.pump(0.3)
        dlgs = [w for w in self.app.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertEqual(len(dlgs), 1)

        def walk(w):
            for c in w.winfo_children():
                yield c
                yield from walk(c)
        buttons = [c for c in walk(dlgs[0]) if c.winfo_class() == "TButton"]
        labels = [b.cget("text") for b in buttons]
        self.assertTrue(any("Coinbase" in t for t in labels))
        self.assertFalse(any("Yahoo" in t for t in labels), "personal-use source shown without opt-in")
        demo = next(b for b in buttons if "Simulated" in b.cget("text"))
        demo.invoke()
        self.assertTrue(self.wait_for(lambda: self.app.tree.get_children()), "picker choice didn't start")

    def test_ai_answer_renders_bold_and_separates(self):
        from quantecho import llm

        def fake(prompt, on_token, **kw):
            for t in ["**Bias:** neutral", "\n- ok"]:
                on_token(t)
            return "x", llm.Stats(tok_per_s=30)
        with mock.patch.object(self.gui.llm, "ask_stream", fake), mock.patch.object(self.gui.journal, "append"):
            self.app.v_with_ctx.set(False)
            self.app.on_ask()
            self.assertTrue(self.wait_for(lambda: "tok/s" in self.app.text.get("1.0", "end")))
        body = self.app.text.get("1.0", "end")
        self.assertIn("Bias: neutral", body)
        self.assertNotIn("**", body)
        self.assertRegex(body, r"tok/s[^\n]*\)\n\n")  # blank line after the answer footer


@unittest.skipUnless(HAVE_DISPLAY, "no display available for Tk")
class TestGuiBuiltinAI(TestGuiSmoke):
    """Same window, but the AI backend is the bundled engine (faked)."""

    def test_builtin_backend_is_used_and_reports_status(self):
        from quantecho import llm
        self.app.v_backend.set("Built-in")
        calls = []

        def fake_builtin(prompt, on_token, **kw):
            calls.append(kw)
            kw["on_status"]("loading model…")
            on_token("**Bias:** bullish")
            return "x", llm.Stats(model="Qwen3-4B-Q4_K_M · test GPU", tok_per_s=55)
        with mock.patch.object(self.gui.llm, "ask_stream_builtin", fake_builtin),                 mock.patch.object(self.gui.llm, "ask_stream", side_effect=AssertionError("Ollama used")),                 mock.patch.object(self.gui.journal, "append"):
            self.app.v_with_ctx.set(False)
            self.app.on_ask()
            self.assertTrue(self.wait_for(lambda: "tok/s" in self.app.text.get("1.0", "end")))
        self.assertEqual(len(calls), 1)
        self.assertIn("built-in AI", self.app.text.get("1.0", "end"))
        self.assertIn("55 tok/s", self.app.text.get("1.0", "end"))

    def test_builtin_failure_is_reported_not_crashing(self):
        self.app.v_backend.set("Built-in")
        with mock.patch.object(self.gui.journal, "append") as j:
            self.app.v_with_ctx.set(False)
            self.app.on_ask()
            self.assertTrue(self.wait_for(lambda: "Built-in AI" in self.app.text.get("1.0", "end")))
            self.pump(0.3)
            j.assert_not_called()                                  # errors aren't journaled
        self.assertEqual(str(self.app.btn_ask.cget("state")), "normal")  # buttons re-enabled


if __name__ == "__main__":
    unittest.main()
