"""Download real CME trade-by-trade data from Databento for backtesting.

  pip install databento
  export DATABENTO_API_KEY=db-xxxxxxxx         (Windows: set DATABENTO_API_KEY=...)
  python fetch_databento.py --start 2026-09-01 --end 2026-09-06          # shows cost only
  python fetch_databento.py --start 2026-09-01 --end 2026-09-06 --yes    # downloads

Always check the quoted cost first — start with a few days.
"""
import argparse
import os

import databento as db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="GC.v.0", help="continuous: GC.v.0 = most-traded gold contract")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--schema", default="trades", help="trades (tape) or mbo (full order book)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--yes", action="store_true", help="actually download")
    a = ap.parse_args()

    client = db.Historical(os.environ["DATABENTO_API_KEY"])
    q = dict(dataset="GLBX.MDP3", symbols=[a.symbol], stype_in="continuous",
             schema=a.schema, start=a.start, end=a.end)
    cost = client.metadata.get_cost(**q)
    print(f"Quoted cost for {a.symbol} {a.schema} {a.start} -> {a.end}: ${cost:.2f}")
    if not a.yes:
        print("Re-run with --yes to download.")
        return
    out = a.out or f"{a.symbol.replace('.', '_')}_{a.schema}_{a.start}_{a.end}.dbn.zst"
    client.timeseries.get_range(**q).to_file(out)
    print(f"Saved {out}  ->  python run_backtest.py --data {out}")


if __name__ == "__main__":
    main()
