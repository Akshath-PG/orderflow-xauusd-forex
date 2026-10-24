"""Order-flow engine.

Consumes the raw trade tape tick by tick and produces, at the close of every
footprint bar, a BarFeatures snapshot:

  * delta / cumulative delta / rolling delta  (aggressive buy - sell volume)
  * large prints     — block trades well above normal size, with exchange
                       fills at the same timestamp merged into ONE print
                       (a 40-lot sweep arrives as several fills — it's one order)
  * stacked imbalance — diagonal footprint imbalances on consecutive prices
  * absorption        — heavy aggression at a bar extreme that failed to move price
  * session VWAP from REAL traded volume (not MT5 tick volume)

The engine never looks ahead: a bar's features are emitted only when the
first print of the NEXT bar arrives.
"""
from collections import deque
from dataclasses import dataclass

from .events import Tick, NS


@dataclass(slots=True)
class BarFeatures:
    ts_open: int
    ts_close: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    buy_vol: int
    sell_vol: int
    delta: int            # this bar
    cum_delta: int        # since session reset
    roll_delta: int       # over flow_window_sec
    large_buy: int        # contracts in large BUY prints, rolling window
    large_sell: int
    large_net: int        # large_buy - large_sell
    bar_large_buy: int
    bar_large_sell: int
    stacked_buy: int      # longest run of buy-side diagonal imbalances
    stacked_sell: int
    absorb_bull: bool     # sellers hammered the low, price held -> passive buyer
    absorb_bear: bool     # buyers hammered the high, price held -> passive seller
    vwap: float
    atr: float


class _Bar:
    __slots__ = ("ts_open", "ts_end", "o", "h", "l", "c", "vol", "buy", "sell",
                 "lbuy", "lsell", "levels")

    def __init__(self, ts_open, ts_end, price):
        self.ts_open, self.ts_end = ts_open, ts_end
        self.o = self.h = self.l = self.c = price
        self.vol = self.buy = self.sell = self.lbuy = self.lsell = 0
        self.levels = {}   # tick index -> [sell_aggr_vol (bid), buy_aggr_vol (ask)]

    def add(self, k, price, size, side, is_large):
        if price > self.h: self.h = price
        if price < self.l: self.l = price
        self.c = price
        self.vol += size
        lv = self.levels.get(k)
        if lv is None:
            lv = self.levels[k] = [0, 0]
        if side > 0:
            self.buy += size; lv[1] += size
            if is_large: self.lbuy += size
        elif side < 0:
            self.sell += size; lv[0] += size
            if is_large: self.lsell += size


class OrderFlowEngine:
    def __init__(self, of_cfg: dict, tick_size: float, day_of=None):
        self.tick = tick_size
        self.bar_ns = int(of_cfg["bar_seconds"] * NS)
        self.size_win = int(of_cfg["size_window"])
        self.large_mult = float(of_cfg["large_print_mult"])
        self.large_min = int(of_cfg["large_print_min"])
        self.win_ns = int(of_cfg["flow_window_sec"] * NS)
        self.imb_ratio = float(of_cfg["imbalance_ratio"])
        self.imb_min = int(of_cfg["imbalance_min_vol"])
        self.abs_mult = float(of_cfg["absorption_mult"])
        self.abs_hold = int(of_cfg["absorption_hold"])
        # day_of(ts) -> trading-day number; resets cumulative delta and VWAP
        self.day_of = day_of or (lambda ts: ts // (86400 * NS))

        self.sizes = deque(); self.size_sum = 0
        self.dq = deque(); self.roll_delta = 0               # (ts, signed size)
        self.lq = deque(); self.large_buy = 0; self.large_sell = 0  # (ts, side, size)
        self.cum_delta = 0; self.pv = 0.0; self.vwap_vol = 0
        self.session = None
        self.ranges = deque(maxlen=int(of_cfg.get("atr_bars", 14)))
        self.bar = None
        self._agg = None   # pending same-timestamp, same-side fills: [ts, side, size, last_price]
        self.n_prints = 0
        self.n_large = 0
        self.large_log = None      # set to [] to record every big trade (charts)

    # ------------------------------------------------------------------
    def on_tick(self, t: Tick) -> list:
        """Feed one raw fill. Returns a list of closed-bar features (usually empty)."""
        out = []
        a = self._agg
        if a is not None and (t.ts != a[0] or t.side != a[1]):
            self._on_print(a[0], a[3], a[2], a[1], out)
            a = self._agg = None
        if self.bar is not None and t.ts >= self.bar.ts_end:
            out.append(self._close_bar())
        if a is None:
            self._agg = [t.ts, t.side, t.size, t.price]
        else:
            a[2] += t.size
            a[3] = t.price
        return out

    def flush(self) -> list:
        out = []
        if self._agg is not None:
            a = self._agg
            self._on_print(a[0], a[3], a[2], a[1], out)
            self._agg = None
        if self.bar is not None:
            out.append(self._close_bar())
        return out

    # ------------------------------------------------------------------
    def _on_print(self, ts, price, size, side, out):
        sess = self.day_of(ts)
        if sess != self.session:
            self.session = sess
            self.cum_delta = 0; self.pv = 0.0; self.vwap_vol = 0

        n = len(self.sizes)
        mean = self.size_sum / n if n else float(size)
        is_large = (n >= 100 and size >= self.large_min and size >= self.large_mult * mean)

        self.sizes.append(size); self.size_sum += size
        if n + 1 > self.size_win:
            self.size_sum -= self.sizes.popleft()

        self.n_prints += 1
        signed = size * side
        self.cum_delta += signed
        self.roll_delta += signed
        self.dq.append((ts, signed))
        if is_large and side != 0:
            self.n_large += 1
            if self.large_log is not None:
                self.large_log.append((ts, price, size, side))
            self.lq.append((ts, side, size))
            if side > 0: self.large_buy += size
            else: self.large_sell += size

        cutoff = ts - self.win_ns
        dq = self.dq
        while dq and dq[0][0] < cutoff:
            self.roll_delta -= dq.popleft()[1]
        lq = self.lq
        while lq and lq[0][0] < cutoff:
            _, s, z = lq.popleft()
            if s > 0: self.large_buy -= z
            else: self.large_sell -= z

        self.pv += price * size; self.vwap_vol += size

        if self.bar is None or ts >= self.bar.ts_end:
            if self.bar is not None:
                out.append(self._close_bar())
            start = (ts // self.bar_ns) * self.bar_ns
            self.bar = _Bar(start, start + self.bar_ns, price)
        self.bar.add(round(price / self.tick), price, size, side, is_large)

    # ------------------------------------------------------------------
    def _close_bar(self) -> BarFeatures:
        b = self.bar
        self.bar = None
        self.ranges.append(b.h - b.l)
        atr = sum(self.ranges) / len(self.ranges)

        lv = b.levels
        keys = sorted(lv)
        ratio, vmin = self.imb_ratio, self.imb_min
        best_b = run_b = best_s = run_s = 0
        prev = None
        for k in keys:
            if prev is not None and k != prev + 1:
                run_b = run_s = 0
            sell_k, buy_k = lv[k]
            below_sell = lv[k - 1][0] if (k - 1) in lv else 0
            above_buy = lv[k + 1][1] if (k + 1) in lv else 0
            run_b = run_b + 1 if (buy_k >= vmin and buy_k >= ratio * max(below_sell, 1)) else 0
            run_s = run_s + 1 if (sell_k >= vmin and sell_k >= ratio * max(above_buy, 1)) else 0
            best_b = max(best_b, run_b); best_s = max(best_s, run_s)
            prev = k

        absorb_bull = absorb_bear = False
        if len(keys) >= 4 and b.vol > 0:
            avg = b.vol / len(keys)
            lo, hi = keys[0], keys[-1]
            ck = round(b.c / self.tick)
            sell_lo = sum(lv[k][0] for k in (lo, lo + 1) if k in lv)
            buy_lo = sum(lv[k][1] for k in (lo, lo + 1) if k in lv)
            buy_hi = sum(lv[k][1] for k in (hi, hi - 1) if k in lv)
            sell_hi = sum(lv[k][0] for k in (hi, hi - 1) if k in lv)
            absorb_bull = (sell_lo >= self.abs_mult * avg and sell_lo > 2 * buy_lo
                           and ck >= lo + self.abs_hold)
            absorb_bear = (buy_hi >= self.abs_mult * avg and buy_hi > 2 * sell_hi
                           and ck <= hi - self.abs_hold)

        vwap = self.pv / self.vwap_vol if self.vwap_vol else b.c
        return BarFeatures(
            ts_open=b.ts_open, ts_close=b.ts_end, open=b.o, high=b.h, low=b.l, close=b.c,
            volume=b.vol, buy_vol=b.buy, sell_vol=b.sell, delta=b.buy - b.sell,
            cum_delta=self.cum_delta, roll_delta=self.roll_delta,
            large_buy=self.large_buy, large_sell=self.large_sell,
            large_net=self.large_buy - self.large_sell,
            bar_large_buy=b.lbuy, bar_large_sell=b.lsell,
            stacked_buy=best_b, stacked_sell=best_s,
            absorb_bull=absorb_bull, absorb_bear=absorb_bear,
            vwap=vwap, atr=atr)
