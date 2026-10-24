"""Event-driven backtester (also the paper-trading engine).

Ticks are processed one at a time, in order, through the SAME engine,
strategy and risk code used live — feed it historical ticks and it's a
backtest, feed it a live stream and it's a paper trader.

Fill model (deliberately pessimistic — order-flow backtests lie easily):
  * Market entries fill on the first print at least `latency_ms` after the
    signal, at that price + entry_slippage_ticks against you.
  * Stops fill at the worse of the stop price and the print that triggered
    it, minus stop_slippage_ticks.
  * Resting take-profits fill only when price trades THROUGH the limit by a
    tick (you'd be at the back of the queue, so a touch is not a fill).
  * Commission is charged per contract per side.
  * Positions too large for the risk budget are skipped, never rounded up
    to a minimum size.
"""
from dataclasses import dataclass, field

from .events import Tick, NS
from .clock import EtClock, hhmm
from .engine import OrderFlowEngine
from .strategy import FlowFollower
from .risk import RiskManager
from .profiles import apply_profile
import copy


@dataclass
class _Pending:
    due: int
    side: int
    n: int
    signal_ts: int


@dataclass
class _Position:
    side: int
    n_init: int
    n_open: int
    entry: float
    entry_ts: int
    sl: float
    tp1: float
    tp2: float
    tp1_done: bool = False
    realized: float = 0.0
    exits: list = field(default_factory=list)   # (n, price)


@dataclass
class TradeRecord:
    entry_ts: int
    exit_ts: int
    side: int
    contracts: int
    entry: float
    exit_avg: float
    pnl: float
    reason: str


class Backtester:
    def __init__(self, cfg: dict, research: bool = False):
        cfg = copy.deepcopy(cfg)
        self.profile = apply_profile(cfg)
        inst, sess, ex = cfg["instrument"], cfg["session"], cfg["execution"]
        self.cfg = cfg
        self.tick = inst["tick_size"]
        self.tv = inst["tick_value"]
        self.comm = inst["commission_per_side"]
        self.st = cfg["strategy"]
        self.clock = EtClock(hhmm(sess["day_reset_et"]))
        self.start_m = hhmm(sess["trade_start_et"])
        self.end_m = hhmm(sess["trade_end_et"])
        self.flat_m = hhmm(sess["flatten_et"])
        self.latency = int(ex["latency_ms"] * 1_000_000)
        self.e_slip = ex["entry_slippage_ticks"] * self.tick
        self.s_slip = ex["stop_slippage_ticks"] * self.tick
        self.thru = self.tick if ex["tp_require_trade_through"] else 0.0
        self.eps = self.tick * 1e-6

        self.balance = float(cfg["propfirm"]["account_size"])
        self.engine = OrderFlowEngine(cfg["orderflow"], self.tick, self.clock.day_id)
        self.strat = FlowFollower(self.st)
        self.risk = RiskManager(cfg["propfirm"], cfg["risk"], research=research)
        self.floor_curve: list[tuple[int, float]] = []

        self.pos: _Position | None = None
        self.pending: _Pending | None = None
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[tuple[int, float]] = []
        self.signals = 0
        self.skipped_size = 0
        self.ticks_seen = 0
        self.first_ts = self.last_ts = None
        self.on_fill = None    # optional callback(text) for live/paper mode
        self.on_event = None   # optional callback(dict): structured signal/exit events (signal hub)
        self._sig_id = 0

    # ------------------------------------------------------------------
    def run(self, ticks):
        for t in ticks:
            self.on_tick(t)
        if self.pos and self.last_ts is not None:
            self._close(self.pos.n_open, self._last_price, self.last_ts, "end_of_data")
        return self

    def on_tick(self, t: Tick):
        self.ticks_seen += 1
        if self.first_ts is None:
            self.first_ts = t.ts
        self.last_ts = t.ts
        self._last_price = t.price
        day = self.clock.day_id(t.ts)
        mod = self.clock.minute_of_day(t.ts)

        if self.pending and t.ts >= self.pending.due:
            self._fill_entry(t)
        if self.pos:
            self._manage(t)

        equity = self.balance + self._unrealized(t.price)
        why = self.risk.check(t.ts, day, self.balance, equity)
        if why:
            self.pending = None
            if self.pos:
                self._close(self.pos.n_open, t.price - self.pos.side * self.s_slip, t.ts, why)

        if self.pos and self.flat_m <= mod < self.clock.reset:
            self._close(self.pos.n_open, t.price - self.pos.side * self.s_slip, t.ts, "session_end")

        for f in self.engine.on_tick(t):
            self._on_bar(f, t, mod)

    # ------------------------------------------------------------------
    def _on_bar(self, f, t: Tick, mod: int):
        eq = self.balance + self._unrealized(t.price)
        self.equity_curve.append((f.ts_close, eq))
        self.floor_curve.append((f.ts_close, self.risk.floor))
        if self.pos:
            if self.strat.exit_signal(f, self.pos.side):
                self._close(self.pos.n_open, t.price - self.pos.side * self.s_slip, t.ts,
                            "flow_reversal")
            return
        if self.pending:
            return
        if not (self.start_m <= mod < self.end_m):
            return
        if not self.risk.can_trade(t.ts):
            return
        side = self.strat.entry_signal(f)
        if side == 0:
            return
        self.signals += 1
        per_contract = (self.st["sl_ticks"] * self.tv + 2 * self.comm
                        + (self.e_slip + self.s_slip) / self.tick * self.tv)
        n = self.risk.contracts(eq, per_contract)
        if n < 1:
            self.skipped_size += 1
            return
        self.pending = _Pending(due=t.ts + self.latency, side=side, n=n, signal_ts=t.ts)
        if self.on_event:
            self._sig_id += 1
            tk = self.tick
            self.on_event({
                "type": "signal", "id": self._sig_id, "ts": t.ts, "side": side,
                "ref_price": t.price,
                "sl_dist": round(self.st["sl_ticks"] * tk, 5),
                "tp1_dist": round(self.st["tp1_ticks"] * tk, 5),
                "tp2_dist": round(self.st["tp2_ticks"] * tk, 5),
                "tp1_fraction": self.st["tp1_fraction"],
                "be_offset": round(self.st["breakeven_offset_ticks"] * tk, 5),
                "max_hold_min": self.st["max_hold_minutes"],
                "large_net": f.large_net, "roll_delta": f.roll_delta, "delta": f.delta,
                "stacked_buy": f.stacked_buy, "stacked_sell": f.stacked_sell,
                "absorb_bull": f.absorb_bull, "absorb_bear": f.absorb_bear,
                "vwap": round(f.vwap, 2), "atr": round(f.atr, 3)})

    def _fill_entry(self, t: Tick):
        p = self.pending
        self.pending = None
        tk = self.tick
        price = t.price + p.side * self.e_slip
        self.pos = _Position(
            side=p.side, n_init=p.n, n_open=p.n, entry=price, entry_ts=t.ts,
            sl=price - p.side * self.st["sl_ticks"] * tk,
            tp1=price + p.side * self.st["tp1_ticks"] * tk,
            tp2=price + p.side * self.st["tp2_ticks"] * tk)
        self.balance -= self.comm * p.n
        self.pos.realized -= self.comm * p.n
        if self.on_fill:
            self.on_fill(f"ENTRY {'LONG' if p.side > 0 else 'SHORT'} {p.n} @ {price:.2f}")

    def _manage(self, t: Tick):
        pos, p, s = self.pos, t.price, self.pos.side
        # stop loss
        if (s > 0 and p <= pos.sl + self.eps) or (s < 0 and p >= pos.sl - self.eps):
            worse = min(pos.sl, p) if s > 0 else max(pos.sl, p)
            self._close(pos.n_open, worse - s * self.s_slip, t.ts,
                        "breakeven_stop" if pos.tp1_done else "stop")
            return
        # take profit (resting limit — needs trade-through)
        target = pos.tp2 if pos.tp1_done else pos.tp1
        hit = (p >= target + self.thru - self.eps) if s > 0 else (p <= target - self.thru + self.eps)
        if hit:
            if not pos.tp1_done and pos.n_open >= 2:
                k = max(1, int(pos.n_open * self.st["tp1_fraction"]))
                self._close(k, target, t.ts, "tp1", partial=True)
                pos.tp1_done = True
                pos.sl = pos.entry + s * self.st["breakeven_offset_ticks"] * self.tick
            else:
                self._close(pos.n_open, target, t.ts, "tp2" if pos.tp1_done else "tp1")
            return
        # time stop
        if t.ts - pos.entry_ts >= self.st["max_hold_minutes"] * 60 * NS:
            self._close(pos.n_open, p - s * self.s_slip, t.ts, "time_stop")

    def _close(self, n, price, ts, reason, partial=False):
        pos = self.pos
        pnl = (price - pos.entry) * pos.side / self.tick * self.tv * n - self.comm * n
        self.balance += pnl
        pos.realized += pnl
        pos.n_open -= n
        pos.exits.append((n, price))
        if self.on_fill:
            self.on_fill(f"EXIT {n} @ {price:.2f} ({reason}) pnl {pnl:+.2f}")
        if pos.n_open > 0 and partial:
            return
        exit_avg = sum(k * px for k, px in pos.exits) / pos.n_init
        if self.on_event:
            self.on_event({"type": "exit", "id": self._sig_id, "ts": ts, "side": pos.side,
                           "reason": reason, "price": round(price, 2)})
        self.trades.append(TradeRecord(pos.entry_ts, ts, pos.side, pos.n_init, pos.entry,
                                       exit_avg, pos.realized, reason))
        self.risk.on_trade_closed(ts, pos.realized)
        self.strat.on_position_closed()
        self.pos = None

    def _unrealized(self, price):
        pos = self.pos
        if not pos:
            return 0.0
        return (price - pos.entry) * pos.side / self.tick * self.tv * pos.n_open
