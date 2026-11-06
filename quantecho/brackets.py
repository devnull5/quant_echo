# quantecho/brackets.py
class BracketManager:
    def __init__(self, entry_price: float, qty: int):
        self.p = float(entry_price)
        self.q_base, self.q_sec, self.q_run = self._split_qty(int(qty))
        self.tp1 = self.p + (0.05 if self.p < 0.50 else 0.10)
        self.tp2 = self.p + (0.30 if self.p < 1.00 else 0.35)
        self.stop_init = self.p - 0.07  # tighten/loosen later if you want
        self.promoted_sec = False
        self.max_unrl = 0.0
        self.stop_run = self.p - 0.02  # BE - 0.02 after TP1
        self.mode = "scalp"
        self.on_sell = None  # set by your LivePosition

    @staticmethod
    def _split_qty(qty: int):
        if qty >= 3:
            q_base = max(1, round(qty * 0.60))
            q_sec  = max(1, round(qty * 0.30))
            q_run  = qty - q_base - q_sec
        elif qty == 2:
            q_base, q_sec, q_run = 1, 0, 1
        else:
            q_base, q_sec, q_run = 1, 0, 0
        return int(q_base), int(q_sec), int(q_run)

    def on_tick(self, last_price, price_below_vwap, ema9, ema20, macd5m_lt_signal, bb_lower, vol_rising):
        self.max_unrl = max(self.max_unrl, float(last_price) - self.p)

        # TP1: scalp chunk
        if self.q_base > 0 and last_price >= self.tp1:
            self._sell("base", self.q_base, self.tp1)
            self.q_base = 0
            self.stop_run = max(self.stop_run, self.p - 0.02)

        # Promote to trend when structure earns it
        if price_below_vwap and (ema9 is not None and ema20 is not None and ema9 < ema20) and macd5m_lt_signal:
            self.mode = "trend"

        # Surge → convert secondary to trend too
        surge = (last_price >= self.tp1 + 0.15) or bool(vol_rising)
        if surge and not self.promoted_sec:
            self.promoted_sec = True

        # TP2 for secondary (if not promoted)
        if self.q_sec > 0 and not self.promoted_sec and last_price >= self.tp2:
            self._sell("secondary", self.q_sec, self.tp2)
            self.q_sec = 0

        # Trailing for runner (and secondary if promoted)
        if self.mode == "trend":
            trail_min = self.p + self.max_unrl * 0.50
            self.stop_run = max(self.stop_run, trail_min)

    def _sell(self, bucket, qty, px):
        if qty <= 0:
            return
        if callable(self.on_sell):
            self.on_sell(bucket, int(qty), float(px))
        else:
            print(f"[{bucket.upper()}] SELL {qty} @ {px}")
