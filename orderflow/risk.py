"""Prop-firm rules + the bot's own capital protection.

FIRM RULES (dollar amounts, as futures prop firms state them)
  * Max drawdown, one of:
      static            — floor = account - drawdown, never moves
      eod_trailing      — floor rises with each day's closing balance
      intraday_trailing — floor rises with the equity peak in real time,
                          including open profit
    Trailing floors can stop rising at account + trail_lock_offset.
  * Daily loss limit
  * Profit target (evaluation) — 0 means funded account
  * Consistency rule — best day < consistency_pct of the target: the bot
    stops for the day before it gets there
  * Contract cap
The bot always stops `safety_buffer` dollars before a firm limit, and never
opens a trade whose full stop-out could breach the drawdown floor or the day's
loss allowance.

BOT PROTECTION (ported from Challenge_EA_Protected)
  * Profit lock — ratcheting floor under peak profit, pause, then resume
  * Losing-streak cooldown
  * Adaptive risk — each consecutive loss shrinks the next position
  * Position size = a % of the room left above the drawdown floor, so size
    shrinks automatically as the account nears its limit
"""
import math

from .events import NS

DD_TYPES = ("static", "eod_trailing", "intraday_trailing")


class RiskManager:
    def __init__(self, pf: dict, r: dict, research: bool = False):
        if pf["drawdown_type"] not in DD_TYPES:
            raise ValueError(f"drawdown_type must be one of {DD_TYPES}")
        self.pf, self.r, self.research = pf, r, research
        self.acct = float(pf["account_size"])
        self.dd = float(pf["max_drawdown"])
        self.dd_type = pf["drawdown_type"]
        self.lock_level = (self.acct + pf["trail_lock_offset"]) if pf["trail_stops_at_start"] else math.inf
        self.floor = self.acct - self.dd           # the firm's failure line
        self.firm_peak = self.acct
        self.buf = float(pf["safety_buffer"])
        self.target = float(pf["profit_target"])

        dll, bot = float(pf["daily_loss_limit"]), float(pf["bot_daily_stop"])
        allow = [x for x in ((dll - self.buf) if dll > 0 else 0, bot) if x > 0]
        self.daily_allow = min(allow) if allow else 0.5 * self.dd

        cap = float(pf["daily_profit_cap"])
        if self.target > 0 and pf["consistency_pct"] > 0:
            cons = pf["consistency_pct"] / 100 * self.target * 0.9   # 10% margin under the rule
            cap = min(cap, cons) if cap > 0 else cons
        self.daily_cap = 0 if research else cap
        self.max_c = int(min(r["max_contracts"], pf["max_contracts_firm"]))
        # minimum profitable days (forex firms): after the target, trade small until enough are banked
        self.min_days = int(pf.get("min_profitable_days", 0))
        self.day_min = float(pf.get("profitable_day_min", 0.0))
        self.prof_days = 0
        self.collect = False

        self.peak = self.acct
        self.lock_floor = 0.0
        self.lock_until = 0
        self.streak = 0
        self.streak_until = 0
        self.dead = None
        self.day = None
        self.day_start = self.acct
        self.day_halt = False
        self.day_pnls = {}
        self.events = []

    # ------------------------------------------------------------------
    def _raise_floor(self, level: float):
        cand = min(level - self.dd, self.lock_level)
        if cand > self.floor:
            self.floor = cand

    def check(self, ts: int, day_id: int, balance: float, equity: float) -> str | None:
        """Call on every tick. Returns a reason if positions must be flattened now."""
        if self.dead:
            return None
        if day_id != self.day:
            if self.day is not None:
                self.day_pnls[self.day] = balance - self.day_start
                if self.min_days and balance - self.day_start >= max(self.day_min, 0.01):
                    self.prof_days += 1
                if self.dd_type == "eod_trailing":
                    self._raise_floor(balance)
            self.day, self.day_start, self.day_halt = day_id, balance, False

        if self.dd_type == "intraday_trailing" and equity > self.firm_peak:
            self.firm_peak = equity
            self._raise_floor(equity)

        if equity <= self.floor + self.buf:
            self.dead = "DRAWDOWN_GUARD"
            self._log(ts, f"DRAWDOWN GUARD at equity {equity:.2f} - firm floor {self.floor:.2f}, "
                          f"stopped {equity - self.floor:.0f} above it. Permanent stop.")
            return "drawdown_guard"

        # aim a quarter of the safety buffer past the target so closing slippage
        # can't leave the closed balance a few dollars short
        if not self.research and self.target > 0 and equity >= self.acct + self.target + 0.25 * self.buf:
            today_counts = self.min_days and equity - self.day_start >= max(self.day_min, 0.01)
            if self.prof_days + (1 if today_counts else 0) >= self.min_days:
                self.dead = "PASSED"
                self._log(ts, f"PROFIT TARGET reached at equity {equity:.2f} - evaluation passed")
                return "target"
            if not self.collect:
                self.collect = True
                self._log(ts, f"Target reached but only {self.prof_days}/{self.min_days} profitable days - "
                              f"trading at quarter size, stopping each day at +{self.day_min:.0f}")

        day_pnl = equity - self.day_start
        if not self.day_halt:
            if day_pnl <= -self.daily_allow:
                self.day_halt = True
                self._log(ts, f"DAILY STOP at day P&L {day_pnl:.2f} (allowed -{self.daily_allow:.0f})")
                return "daily_stop"
            if self.collect and day_pnl >= max(self.day_min, 0.01):
                self.day_halt = True
                self._log(ts, f"profitable day banked (+{day_pnl:.2f}) - done for today")
                return "daily_cap"
            if self.daily_cap > 0 and day_pnl >= self.daily_cap:
                self.day_halt = True
                self._log(ts, f"DAILY PROFIT CAP {day_pnl:.2f} >= {self.daily_cap:.0f} (consistency) - done for today")
                return "daily_cap"

        r = self.r
        if r["use_profit_lock"] and not self.research:
            if equity > self.peak:
                self.peak = equity
            pp = self.peak - self.acct
            if pp >= r["profit_lock_activate"]:
                f = self.acct + pp * (1 - r["profit_lock_giveback_pct"] / 100)
                if f > self.lock_floor:
                    self.lock_floor = f
            if self.lock_floor > 0 and equity < self.lock_floor and ts >= self.lock_until:
                self.lock_until = ts + int(r["profit_lock_pause_hours"] * 3600 * NS)
                self._log(ts, f"PROFIT LOCK at equity {equity:.2f} (lock {self.lock_floor:.2f}) "
                              f"- pausing {r['profit_lock_pause_hours']:.0f}h")
                self.lock_floor = equity - r["relock_buffer"]
                self.peak = equity
                return "profit_lock"
        return None

    def can_trade(self, ts: int) -> bool:
        if self.dead or self.day_halt or ts < self.lock_until:
            return False
        if self.streak_until:
            if ts < self.streak_until:
                return False
            self.streak_until = 0
            self.streak = 0
            self._log(ts, "Streak cooldown over - trading resumes")
        return True

    def on_trade_closed(self, ts: int, pnl: float):
        r = self.r
        if pnl < -0.01 * self.dd:
            self.streak += 1
            if r["use_streak_cooldown"] and self.streak >= r["max_consecutive_losses"] \
                    and not self.streak_until:
                self.streak_until = ts + int(r["streak_cooldown_hours"] * 3600 * NS)
                self._log(ts, f"{self.streak} losses in a row - cooling down "
                              f"{r['streak_cooldown_hours']:.0f}h")
        elif pnl > 0.005 * self.dd:
            self.streak = 0

    def room(self, equity: float) -> float:
        return equity - (self.floor + self.buf)

    def contracts(self, equity: float, per_contract: float) -> int:
        """Size from the room above the floor, then shrink until a full stop-out
        can breach neither the drawdown guard nor today's loss allowance."""
        r = self.r
        room = self.room(equity)
        if room <= 0 or per_contract <= 0:
            return 0
        mult = 1.0
        if r["use_adaptive_risk"] and self.streak > 0:
            mult = max(r["min_risk_mult"], r["risk_cut_per_loss"] ** self.streak)
        base = room * r["risk_pct_of_room"] / 100 * (0.25 if self.collect else 1.0)
        n = int(base * mult // per_contract)
        if n < 1 and int(base // per_contract) >= 1:
            n = 1          # adaptive risk never shrinks to zero (would freeze the bot)
        n = min(n, self.max_c)
        day_pnl = equity - self.day_start
        while n > 0 and (equity - n * per_contract <= self.floor + self.buf
                         or day_pnl - n * per_contract <= -self.daily_allow):
            n -= 1
        return n

    def _log(self, ts, text):
        self.events.append((ts, text))
