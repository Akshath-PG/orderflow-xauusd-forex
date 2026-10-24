"""Broker-agnostic executor logic for XAUUSD.

Takes hub signals, sizes them for THIS account, enforces THIS account's
prop-firm rules (the same rule engine as the futures bot), and manages the
trade: broker-side stop loss and take profit on every order, half off at TP1
with the stop moved to breakeven, time stop, session flatten, and the hub's
strategy exits. Works on netting and hedging accounts (one position at a time).

A broker adapter must provide:
  volume_min, volume_step, volume_max
  quote() -> (bid, ask)
  account() -> (balance, equity)
  position() -> None | {"side", "volume", "price_open", "sl", "tp"}
  open(side, volume, sl, tp, comment) -> (ok, info, fill_price)
  close(volume=None) -> (ok, info)
  modify(sl, tp) -> (ok, info)
  loss_per_lot(side, price, stop_price) -> $ lost by 1.00 lot from price to stop
"""
import copy
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orderflow.clock import EtClock, hhmm          # noqa: E402
from orderflow.profiles import apply_profile       # noqa: E402
from orderflow.risk import RiskManager             # noqa: E402

STRATEGY_EXITS = {"flow_reversal", "time_stop", "session_end"}


class ExecutorCore:
    def __init__(self, cfg: dict, broker, now_ns, name: str = "executor", log=print, send=None):
        cfg = copy.deepcopy(cfg)
        self.profile = apply_profile(cfg)
        self.cfg, self.b, self.now, self.name = cfg, broker, now_ns, name
        self.log_fn, self.send = log, send
        self.risk = RiskManager(cfg["propfirm"], cfg["risk"])
        s = cfg["session"]
        self.clock = EtClock(hhmm(s["day_reset_et"]))
        self.flat_m = hhmm(s["flatten_et"])
        inst = cfg["instrument"]
        # commission is configured per 0.01 lot per side -> per lot round trip
        self.comm_lot_rt = inst["commission_per_side"] * 100 * 2
        self.stop_slip = cfg["execution"]["stop_slippage_ticks"] * inst["tick_size"]
        self.trade = None
        self.closed = []          # (signal id, pnl, reason)

    # ------------------------------------------------------------------
    def log(self, text):
        self.log_fn(f"[{self.name}] {text}")

    def _status(self, **kw):
        if self.send:
            self.send({"type": "status", "executor": self.name, **kw})

    def on_message(self, m: dict):
        t = m.get("type")
        if t == "signal":
            self._on_signal(m)
        elif t == "exit":
            if self.trade and m.get("id") == self.trade["id"] and m.get("reason") in STRATEGY_EXITS:
                self.log(f"hub exit #{m['id']} ({m['reason']}) - closing")
                self._close_all(m["reason"])

    # ------------------------------------------------------------------
    def _on_signal(self, m):
        self.poll()                                          # refresh rules first
        side = int(m["side"])
        if self.trade or self.b.position():
            self.log(f"signal #{m['id']} skipped - already in a position")
            return
        now = self.now()
        if not self.risk.can_trade(now):
            self.log(f"signal #{m['id']} skipped - rules: {self.risk.dead or 'paused / daily stop / cooldown'}")
            return
        bid, ask = self.b.quote()
        price = ask if side > 0 else bid
        sl = price - side * m["sl_dist"]
        step = self.b.volume_step
        loss_lot = self.b.loss_per_lot(side, price, sl - side * self.stop_slip)
        per_step = (loss_lot + self.comm_lot_rt) * step
        bal, eq = self.b.account()
        units = self.risk.contracts(eq, per_step)
        vol = min(round(units * step, 8), self.b.volume_max)
        vol = math.floor(vol / step + 1e-9) * step
        if vol < self.b.volume_min:
            self.log(f"signal #{m['id']} skipped - risk budget too small for the minimum lot")
            return
        split = vol >= 2 * self.b.volume_min
        tp = price + side * (m["tp2_dist"] if split else m["tp1_dist"])
        ok, info, fill = self.b.open(side, round(vol, 2), sl, tp, f"OF#{m['id']}")
        if not ok:
            self.log(f"signal #{m['id']} order REJECTED: {info}")
            return
        entry = fill or price
        self.trade = {
            "id": m["id"], "side": side, "entry": entry, "vol": round(vol, 2), "split": split,
            "tp1": entry + side * m["tp1_dist"], "tp2": tp,
            "be": entry + side * m["be_offset"], "frac": m["tp1_fraction"], "tp1_done": False,
            "opened": self.now(), "max_hold_ns": int(m["max_hold_min"] * 60e9), "bal_open": bal}
        self.log(f"OPEN #{m['id']} {'BUY' if side > 0 else 'SELL'} {vol:.2f} lots @ {entry:.2f} "
                 f"SL {sl:.2f} TP {tp:.2f} (risk ~${loss_lot * vol + self.comm_lot_rt * vol:.0f}, "
                 f"room ${self.risk.room(eq):.0f})")
        self._status(event="open", id=m["id"], side=side, volume=round(vol, 2), price=round(entry, 2))

    # ------------------------------------------------------------------
    def poll(self):
        """Call every fraction of a second: account rules + trade management."""
        now = self.now()
        bal, eq = self.b.account()
        why = self.risk.check(now, self.clock.day_id(now), bal, eq)
        pos = self.b.position()

        if self.trade and not pos:
            self._finish("broker stop/target")
            return
        if why and pos:
            self.log(f"RULE: {why} - closing everything")
            self._close_all(why)
            return
        if not self.trade or not pos:
            return

        tr, s = self.trade, self.trade["side"]
        bid, ask = self.b.quote()
        px = bid if s > 0 else ask                         # price we'd exit at
        if tr["split"] and not tr["tp1_done"] and s * (px - tr["tp1"]) >= 0:
            step = self.b.volume_step
            part = max(self.b.volume_min, math.floor(pos["volume"] * tr["frac"] / step + 1e-9) * step)
            if part < pos["volume"]:
                ok, info = self.b.close(round(part, 2))
                if ok:
                    tr["tp1_done"] = True
                    self.b.modify(tr["be"], tr["tp2"])
                    self.log(f"TP1 #{tr['id']}: closed {part:.2f} lots, stop -> breakeven {tr['be']:.2f}")
            return
        if now - tr["opened"] >= tr["max_hold_ns"]:
            self.log(f"time stop #{tr['id']}")
            self._close_all("time_stop")
            return
        mod = self.clock.minute_of_day(now)
        if self.flat_m <= mod < self.clock.reset:
            self.log(f"session flatten #{tr['id']}")
            self._close_all("session_end")

    def _close_all(self, reason):
        if self.b.position():
            ok, info = self.b.close()
            if not ok:
                self.log(f"CLOSE FAILED ({reason}): {info} - broker stop loss still protects the position")
                return
        if self.trade:
            self._finish(reason)

    def _finish(self, reason):
        bal, _ = self.b.account()
        tr = self.trade
        pnl = bal - tr["bal_open"]
        self.risk.on_trade_closed(self.now(), pnl)
        self.closed.append((tr["id"], pnl, reason))
        self.log(f"CLOSED #{tr['id']} ({reason}) P&L {pnl:+.2f} | balance {bal:.2f} | "
                 f"floor {self.risk.floor:.2f} | losing streak {self.risk.streak}")
        self._status(event="closed", id=tr["id"], pnl=round(pnl, 2), reason=reason, balance=round(bal, 2))
        self.trade = None
