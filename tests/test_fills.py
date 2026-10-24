"""Fill-model and engine checks. Run: python tests/test_fills.py"""
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orderflow import Backtester, OrderFlowEngine, Tick          # noqa: E402
from orderflow.backtest import _Position                          # noqa: E402

NS = 1_000_000_000
T0 = 1_780_000_000 * NS - (1_780_000_000 * NS) % (86400 * NS) + 14 * 3600 * NS  # 14:00 UTC


def cfg():
    with open(Path(__file__).resolve().parent / "fixture_config.toml", "rb") as fh:
        c = tomllib.load(fh)
    c["risk"]["use_profit_lock"] = False
    return c


def long_bot(n=2, entry=4000.0):
    bt = Backtester(cfg())
    tk = bt.tick
    bt.pos = _Position(side=1, n_init=n, n_open=n, entry=entry, entry_ts=T0,
                       sl=entry - 30 * tk, tp1=entry + 30 * tk, tp2=entry + 60 * tk)
    return bt


def test_tp_needs_trade_through():
    bt = long_bot()
    bt.on_tick(Tick(T0 + NS, 4003.0, 1, 1))          # touches TP1 exactly
    assert bt.pos.tp1_done is False, "a touch must not fill a resting limit"
    bt.on_tick(Tick(T0 + 2 * NS, 4003.1, 1, 1))      # trades through
    assert bt.pos.tp1_done and bt.pos.n_open == 1
    assert abs(bt.pos.sl - 4000.2) < 1e-9, "stop moves to breakeven + 2 ticks"


def test_stop_gap_fills_at_worse_price_plus_slippage():
    bt = long_bot(n=1)
    start = bt.balance
    bt.on_tick(Tick(T0 + NS, 3996.5, 1, -1))         # gaps through the 3997.0 stop
    assert bt.pos is None and bt.trades[-1].reason == "stop"
    exp = (3996.4 - 4000.0) / 0.1 * 1.0 - 0.85       # gap price - 1 tick, one exit commission
    assert abs((bt.balance - start) - exp) < 1e-6, (bt.balance - start, exp)


def test_single_contract_exits_fully_at_tp1():
    bt = long_bot(n=1)
    bt.on_tick(Tick(T0 + NS, 4003.1, 1, 1))
    assert bt.pos is None and bt.trades[-1].reason == "tp1"


def test_entry_latency_and_slippage():
    bt = Backtester(cfg())
    from orderflow.backtest import _Pending
    bt.pending = _Pending(due=T0 + 50_000_000, side=1, n=1, signal_ts=T0)
    bt.on_tick(Tick(T0 + 10_000_000, 4000.0, 1, 1))  # before latency elapses
    assert bt.pos is None
    bt.on_tick(Tick(T0 + 60_000_000, 4000.5, 1, 1))
    assert bt.pos and abs(bt.pos.entry - 4000.6) < 1e-9


def test_sweep_fills_merge_into_one_large_print():
    c = cfg()["orderflow"]
    eng = OrderFlowEngine(c, 0.1)
    ts = T0
    for i in range(300):                                 # warm up the average size (~1)
        eng.on_tick(Tick(ts + i * 1000, 4000.0, 1, 1 if i % 2 else -1))
    sweep = T0 + 10 * NS
    for j, part in enumerate([12, 10, 9, 8]):            # one 39-lot sweep across 4 prices
        eng.on_tick(Tick(sweep, 4000.1 + j * 0.1, part, 1))
    eng.on_tick(Tick(sweep + 1, 4000.4, 1, -1))         # next print flushes the sweep
    assert eng.n_large == 1 and eng.large_buy == 39


def test_no_lookahead_bar_closes_on_next_bar_print():
    eng = OrderFlowEngine(cfg()["orderflow"], 0.1)
    out = eng.on_tick(Tick(T0, 4000.0, 1, 1))
    out += eng.on_tick(Tick(T0 + 30 * NS, 4000.1, 1, 1))
    assert out == []
    out = eng.on_tick(Tick(T0 + 61 * NS, 4000.2, 1, 1))
    assert len(out) == 1 and out[0].close == 4000.1


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f()
        print("PASS", f.__name__)
    print(f"{len(fns)} tests passed")
