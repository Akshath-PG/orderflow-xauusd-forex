"""Run a backtest — the equivalent of MT5's Strategy Tester.

  python run_backtest.py --data synthetic                    # plumbing test, planted edge
  python run_backtest.py --data synthetic --p-informed 0     # control: tape with NO edge
  python run_backtest.py --data gc_trades.dbn.zst            # real CME tape from Databento
  python run_backtest.py --data my_ticks.csv                 # your own tick CSV
  add --research to switch off target/profit-lock and see the raw strategy
"""
import argparse
import json
import tomllib

from orderflow import Backtester
from orderflow import data_sources as ds
from orderflow.report import summarize, write_outputs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config_xauusd.toml")
    ap.add_argument("--data", default="synthetic")
    ap.add_argument("--days", type=int, default=15, help="synthetic only")
    ap.add_argument("--seed", type=int, default=1, help="synthetic only")
    ap.add_argument("--p-informed", type=float, default=0.55, help="synthetic only")
    ap.add_argument("--research", action="store_true")
    ap.add_argument("--out", default="results")
    a = ap.parse_args()

    with open(a.config, "rb") as fh:
        cfg = tomllib.load(fh)

    if a.data == "synthetic":
        ticks = ds.synthetic_tape(days=a.days, seed=a.seed, p_informed=a.p_informed,
                                  tick=cfg["instrument"]["tick_size"])
        title = f"Synthetic tape (p_informed={a.p_informed}, seed={a.seed})"
    elif a.data.endswith((".dbn", ".dbn.zst")):
        ticks = ds.load_databento(a.data)
        title = a.data
    else:
        ticks = ds.load_csv(a.data)
        title = a.data

    bt = Backtester(cfg, research=a.research).run(ticks)
    s = summarize(bt)
    write_outputs(bt, s, a.out, title)
    print(json.dumps(s, indent=2))
    print(f"\nWrote {a.out}/summary.json, trades.csv, equity.png")


if __name__ == "__main__":
    main()
