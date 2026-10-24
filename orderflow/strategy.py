"""Flow-following strategy.

Idea: follow large aggressive participants ONLY when the rest of the tape
agrees with them. A big print on its own is ambiguous (it may be a hedge or
liquidity), so an entry needs three things lined up:

  1. Big money:   net size in large prints over the window clears a threshold
  2. Agreement:   bar delta and rolling delta point the same way
  3. Structure:   the footprint shows stacked imbalance (aggressors walking
                  the book) or absorption (the other side got absorbed)

plus a VWAP side/extension filter so it doesn't chase exhausted moves.
"""
from .engine import BarFeatures


class FlowFollower:
    def __init__(self, cfg: dict):
        self.c = cfg
        self.cooldown = 0

    def entry_signal(self, f: BarFeatures) -> int:
        """+1 long, -1 short, 0 nothing. Call only when flat."""
        c = self.c
        if self.cooldown > 0:
            self.cooldown -= 1
            return 0
        if f.atr <= 0:
            return 0

        thr = c["min_net_large"]
        long_ = f.large_net >= thr
        short = f.large_net <= -thr

        if c["require_delta_confirm"]:
            long_ = long_ and f.roll_delta > 0 and f.delta > 0
            short = short and f.roll_delta < 0 and f.delta < 0

        if c["require_structure"]:
            long_ = long_ and (f.stacked_buy >= c["min_stacked"] or f.absorb_bull)
            short = short and (f.stacked_sell >= c["min_stacked"] or f.absorb_bear)

        if c["use_vwap_filter"]:
            ext = c["max_vwap_extension_atr"] * f.atr
            long_ = long_ and f.vwap < f.close <= f.vwap + ext
            short = short and f.vwap - ext <= f.close < f.vwap

        if long_ and not short:
            return 1
        if short and not long_:
            return -1
        return 0

    def exit_signal(self, f: BarFeatures, side: int) -> bool:
        """True if big flow has flipped against an open position."""
        c = self.c
        if not c["flow_reversal_exit"]:
            return False
        thr = c["min_net_large"] * c["reversal_frac"]
        if side > 0:
            return f.large_net <= -thr and f.roll_delta < 0
        return f.large_net >= thr and f.roll_delta > 0

    def on_position_closed(self):
        self.cooldown = int(self.c["cooldown_bars"])
