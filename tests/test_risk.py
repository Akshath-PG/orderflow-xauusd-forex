"""Prop-firm rule checks. Run: python tests/test_risk.py"""
import copy
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orderflow.risk import RiskManager      # noqa: E402

NS = 1_000_000_000
CFG = tomllib.load(open(Path(__file__).resolve().parent / "fixture_config.toml", "rb"))


def rm(**pf_over):
    pf = copy.deepcopy(CFG["propfirm"]); pf.update(pf_over)
    r = copy.deepcopy(CFG["risk"]); r["use_profit_lock"] = False
    return RiskManager(pf, r)


def test_static_floor_never_moves():
    k = rm(drawdown_type="static")
    k.check(0, 1, 50000, 51500)
    k.check(NS, 2, 51500, 51500)
    assert k.floor == 48000


def test_intraday_trailing_follows_open_profit_and_locks():
    k = rm(drawdown_type="intraday_trailing", trail_stops_at_start=True, trail_lock_offset=100)
    k.check(0, 1, 50000, 50800)                 # open profit counts
    assert k.floor == 48800
    k.check(NS, 1, 50000, 53000)
    assert k.floor == 50100, "floor stops at start + lock offset"
    k.check(2 * NS, 1, 50000, 50600)
    assert k.floor == 50100, "floor never moves down"


def test_eod_trailing_only_moves_at_day_close():
    k = rm(drawdown_type="eod_trailing", trail_stops_at_start=True, trail_lock_offset=0)
    k.check(0, 1, 50000, 50900)
    assert k.floor == 48000, "intraday peaks ignored"
    k.check(NS, 2, 50700, 50700)                 # new day: yesterday closed at 50700
    assert k.floor == 48700


def test_guard_fires_buffer_above_floor():
    k = rm(drawdown_type="static", safety_buffer=200, daily_loss_limit=0, bot_daily_stop=5000)
    assert k.check(0, 1, 50000, 48250) is None
    assert k.check(NS, 1, 50000, 48190) == "drawdown_guard"
    assert k.dead == "DRAWDOWN_GUARD"


def test_daily_loss_limit_minus_buffer():
    k = rm(daily_loss_limit=1000, safety_buffer=200, bot_daily_stop=0)
    assert k.daily_allow == 800
    k.check(0, 1, 50000, 50000)
    assert k.check(NS, 1, 50000, 49190) == "daily_stop"
    assert not k.can_trade(2 * NS)
    k.check(3 * NS, 2, 49190, 49190)
    assert k.can_trade(4 * NS), "new day resumes"


def test_consistency_caps_the_day_under_the_rule():
    k = rm(profit_target=3000, consistency_pct=50, daily_profit_cap=0)
    assert k.daily_cap == 1350                   # 50% of 3000 with a 10% margin
    k.check(0, 1, 50000, 50000)
    assert k.check(NS, 1, 50000, 51400) == "daily_cap"


def test_trade_size_never_risks_a_breach():
    k = rm(drawdown_type="static", max_drawdown=2000, safety_buffer=200, daily_loss_limit=0,
           bot_daily_stop=0, max_contracts_firm=50)
    k.check(0, 1, 50000, 50000)
    n = k.contracts(50000, 33.7)                 # room 1800 x 12% = 216 -> 6 micros
    assert n == 6
    assert k.contracts(48300, 33.7) == 0         # only $100 of room left: no trade


def test_trade_size_shrinks_to_fit_daily_allowance():
    k = rm(drawdown_type="static", daily_loss_limit=1000, safety_buffer=200, bot_daily_stop=0,
           max_contracts_firm=50)
    k.check(0, 1, 50700, 50700)                  # day started at 50700
    n = k.contracts(50000, 33.7)                 # already -700 today, allowance 800
    assert n == 2 and -700 - n * 33.7 > -800, n  # 6 by room, cut to 2 by the daily limit


def test_funded_mode_has_no_target():
    k = rm(profit_target=0, consistency_pct=50, daily_profit_cap=0)
    assert k.daily_cap == 0
    k.check(0, 1, 50000, 60000)
    assert k.dead is None


def test_min_profitable_days_keeps_trading_small_after_target():
    k = rm(profit_target=3000, min_profitable_days=3, profitable_day_min=100, consistency_pct=0,
           daily_loss_limit=0, bot_daily_stop=5000, drawdown_type="static")
    k.check(0, 1, 50000, 50000)
    k.check(NS, 2, 51500, 51500)                 # day 1 closed +1500 -> 1 profitable day
    assert k.check(2 * NS, 2, 51500, 53100) == "daily_cap"     # target hit, day already profitable: banked
    assert k.collect and not k.dead
    k.check(3 * NS, 3, 53100, 53100)                           # day 2 counted -> 2 days
    assert k.prof_days == 2
    assert k.contracts(53100, 33.7) <= max(1, int((53100 - 48200) * 0.12 * 0.25 // 33.7))   # quarter size
    assert k.check(4 * NS, 3, 53100, 53260) == "target" and k.dead == "PASSED"           # 3rd day -> pass


if __name__ == "__main__":
    fns = [v for k_, v in dict(globals()).items() if k_.startswith("test_")]
    for f in fns:
        f(); print("PASS", f.__name__)
    print(f"{len(fns)} tests passed")
