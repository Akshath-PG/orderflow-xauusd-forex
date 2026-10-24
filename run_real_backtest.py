"""Real-data backtest, March to today, with MT5-style reports.

  # 1) Databento (new accounts get free credit) — downloads month by month, shows the cost first
  set DATABENTO_API_KEY=db-xxxxxxxx
  python run_real_backtest.py --start 2026-03-01                  # quote only
  python run_real_backtest.py --start 2026-03-01 --yes            # download + backtest

  # 2) Data exported from NinjaTrader (OrderFlowTickExporter), one or more CSV files
  python run_real_backtest.py --files orderflow_MGC_*.csv

Writes to reports/<date>/:
  funded_continuous.html   — the funded profile run straight through the whole period
  challenge_attempts.html  — a fresh evaluation started at the beginning of every month
  challenge_<month>.html   — the full report for each of those attempts
"""
import argparse
import copy
import glob
import heapq
import os
import sys
import tomllib
from datetime import date, datetime, timedelta

from orderflow import Backtester
from orderflow import data_sources as ds
from orderflow.clock import ET
from orderflow.events import NS
from orderflow.mt5_report import write_html


def month_ranges(start: date, end: date):
    d = start.replace(day=1) if start.day == 1 else start
    while d < end:
        nxt = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield d, min(nxt, end)
        d = nxt


def download(symbol, start, end, data_dir, yes):
    import databento as db
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        sys.exit("Set DATABENTO_API_KEY first.")
    client = db.Historical(key)
    os.makedirs(data_dir, exist_ok=True)
    plan, total = [], 0.0
    for a, b in month_ranges(start, end):
        path = os.path.join(data_dir, f"{symbol.replace('.', '_')}_trades_{a}_{b}.dbn.zst")
        q = dict(dataset="GLBX.MDP3", symbols=[symbol], stype_in="continuous", schema="trades",
                 start=a.isoformat(), end=b.isoformat())
        if os.path.exists(path):
            print(f"  {a} -> {b}: already downloaded")
        else:
            cost = client.metadata.get_cost(**q)
            total += cost
            print(f"  {a} -> {b}: ${cost:.2f}")
        plan.append((path, q))
    print(f"Total to download: ${total:.2f}")
    if not yes and total > 0:
        sys.exit("Re-run with --yes to download.")
    for path, q in plan:
        if not os.path.exists(path):
            print(f"  downloading {os.path.basename(path)} ...", flush=True)
            client.timeseries.get_range(**q).to_file(path)
    return [p for p, _ in plan]


def tick_stream(files):
    """Merge files into one time-ordered stream (files may be per month or overlap)."""
    gens = []
    for f in files:
        gens.append(ds.load_databento(f) if f.endswith((".dbn", ".dbn.zst")) else ds.load_csv(f))
    return heapq.merge(*gens, key=lambda t: t.ts)


def run_one(cfg, files, t0=None, t1=None):
    bt = Backtester(cfg)
    for t in tick_stream(files):
        if t0 is not None and t.ts < t0:
            continue
        if t1 is not None and t.ts >= t1:
            break
        bt.on_tick(t)
        if bt.risk.dead in ("PASSED", "DRAWDOWN_GUARD") and t1 is not None and not bt.pos:
            break
    if bt.pos and bt.last_ts is not None:
        bt._close(bt.pos.n_open, bt._last_price, bt.last_ts, "end_of_data")
    return bt


def et_ns(d: date):
    return int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp() * NS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-config", default="config_xauusd.toml")
    ap.add_argument("--funded-config", default="config_xauusd_funded.toml")
    ap.add_argument("--start", default="2026-03-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--files", nargs="*", help="tick files (NinjaTrader CSV or Databento) instead of downloading")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    ev = tomllib.load(open(a.eval_config, "rb"))
    fu = tomllib.load(open(a.funded_config, "rb"))
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    if a.files:
        files = sorted({f for pat in a.files for f in glob.glob(pat)})
        if not files:
            sys.exit("No files matched.")
        desc = f"{len(files)} tick file(s): " + ", ".join(os.path.basename(f) for f in files[:3]) + ("..." if len(files) > 3 else "")
    else:
        files = download(ev["instrument"]["data_symbol"], start, end, a.data_dir, a.yes)
        desc = f"Databento GLBX.MDP3 trades, {ev['instrument']['data_symbol']}, {start} to {end}"

    out = a.out or os.path.join("reports", date.today().isoformat())
    os.makedirs(out, exist_ok=True)

    # 1) funded profile, straight through the whole period
    print("Funded profile, whole period ...", flush=True)
    bt = run_one(fu, files, et_ns(start), et_ns(end))
    c = write_html(bt, os.path.join(out, "funded_continuous.html"),
                   f"Funded profile, {start} to {end}", desc, synthetic=False)
    print(f"  net {c['net']:+.2f} | trades {c['trades']} | PF {c['pf']} | max equity DD {c['eq_dd'][1]:.2f}")

    # 2) a fresh evaluation attempt at the start of every month
    rows = []
    for a0, a1 in month_ranges(start, end):
        print(f"Evaluation attempt starting {a0} ...", flush=True)
        stop = et_ns(min(a0 + timedelta(days=45), end))     # an attempt gets up to ~45 calendar days
        e = copy.deepcopy(ev)
        bt = run_one(e, files, et_ns(a0), stop)
        name = f"challenge_{a0:%Y-%m}.html"
        c = write_html(bt, os.path.join(out, name), f"Evaluation attempt starting {a0}", desc, synthetic=False)
        days = None
        if bt.risk.dead == "PASSED":
            pts = next(ts for ts, txt in bt.risk.events if txt.startswith("PROFIT TARGET"))
            days = sum(1 for d in range(bt.clock.day_id(et_ns(a0)), bt.clock.day_id(pts) + 1) if (d + 3) % 7 < 5)
        rows.append((a0, bt.risk.dead or "NOT PASSED IN 45 DAYS", days, c["net"], c["trades"], name))
        print(f"  {rows[-1][1]} | net {c['net']:+.2f} | trades {c['trades']}")

    with open(os.path.join(out, "challenge_attempts.html"), "w", encoding="utf-8") as fh:
        body = "".join(f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2] or '-'}</td><td>{r[3]:+,.2f}</td>"
                       f"<td>{r[4]}</td><td><a href='{r[5]}'>report</a></td></tr>" for r in rows)
        passed = sum(r[1] == "PASSED" for r in rows)
        fh.write(f"""<!doctype html><html><head><meta charset="utf-8"><title>Challenge attempts</title>
<style>body{{font:13px Tahoma,Arial,sans-serif;max-width:900px;margin:24px auto;padding:0 16px}}
table{{border-collapse:collapse;width:100%}}th,td{{padding:5px 8px;border-bottom:1px solid #e1e0d9;text-align:left}}
th{{background:#f0efec}}</style></head><body><h1>Evaluation attempts — {start} to {end}</h1>
<p>{desc}. A fresh evaluation starts on the first of each month and runs up to 45 days.
<b>{passed} of {len(rows)} passed.</b></p>
<table><tr><th>Start</th><th>Outcome</th><th>Trading days to pass</th><th>Net</th><th>Trades</th><th></th></tr>{body}</table>
</body></html>""")
    print(f"\nReports written to {out}/")


if __name__ == "__main__":
    main()
