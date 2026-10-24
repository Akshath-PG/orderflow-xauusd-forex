"""One backtest can be luck. Run the challenge many times and count outcomes.

  python robustness_sweep.py                       # 10 synthetic tapes with a planted edge
  python robustness_sweep.py --p-informed 0        # 10 tapes with NO edge (control)

With real data, do the same thing across different date windows
(e.g. every month of the past year) instead of different seeds.
"""
import argparse
import tomllib

from orderflow import Backtester
from orderflow import data_sources as ds
from orderflow.report import summarize


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config_xauusd.toml")
    ap.add_argument("--p-informed", type=float, default=0.55)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--days", type=int, default=22)
    a = ap.parse_args()
    cfg = tomllib.load(open(a.config, "rb"))

    outcomes, pass_days, monthly = [], [], []
    print(f"{a.config} | p_informed={a.p_informed} | {a.days} trading days per run")
    for seed in range(1, a.seeds + 1):
        bt = Backtester(cfg).run(ds.synthetic_tape(days=a.days, seed=seed, p_informed=a.p_informed,
                                                   tick=cfg["instrument"]["tick_size"]))
        s = summarize(bt)
        outcomes.append(s["outcome"])
        dtp = s["trading_days_to_pass"]
        if dtp: pass_days.append(dtp)
        monthly.append(s["net_pct"])
        print(f"  seed {seed:2d}  {s['outcome']:<15} net {s['net_pnl']:+9.1f}  "
              f"trades {s['trades']:3d}  WR {s['win_rate_pct']:5.1f}%  "
              f"max DD {s['max_drawdown_equity']:7.1f}  "
              f"{'passed in ' + str(dtp) + ' trading days' if dtp else ''}")
    for k in ("PASSED", "DRAWDOWN_GUARD", "STILL RUNNING"):
        print(f"{k}: {outcomes.count(k)}")
    if pass_days:
        pass_days.sort()
        print(f"days to pass: fastest {pass_days[0]}, median {pass_days[len(pass_days)//2]}, slowest {pass_days[-1]}")
    monthly.sort()
    print(f"return over the run: median {monthly[len(monthly)//2]:+.2f}%, "
          f"worst {monthly[0]:+.2f}%, months >= +2%: {sum(m >= 2 for m in monthly)}/{len(monthly)}")


if __name__ == "__main__":
    main()
