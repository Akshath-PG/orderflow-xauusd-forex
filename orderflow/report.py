"""Backtest report: stats, trade list, equity chart."""
import csv
import json
from collections import Counter
from datetime import datetime, timezone

from .events import NS


def _iso(ts):
    return datetime.fromtimestamp(ts / NS, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _days_to_pass(bt):
    if bt.risk.dead != "PASSED" or not bt.first_ts:
        return None
    pass_ts = next(ts for ts, txt in bt.risk.events if txt.startswith("PROFIT TARGET"))
    d0, d1 = bt.clock.day_id(bt.first_ts), bt.clock.day_id(pass_ts)
    return sum(1 for d in range(d0, d1 + 1) if (d + 3) % 7 < 5)   # weekdays only (day 0 = Thu 1970-01-01)


def summarize(bt) -> dict:
    tr = bt.trades
    start = float(bt.cfg["propfirm"]["account_size"])
    days_pnl = {}
    for t in tr:
        d = bt.clock.day_id(t.exit_ts)
        days_pnl[d] = days_pnl.get(d, 0.0) + t.pnl
    best_day = max(days_pnl.values()) if days_pnl else 0.0
    total_profit = bt.balance - start
    wins = [t.pnl for t in tr if t.pnl > 0]
    losses = [t.pnl for t in tr if t.pnl <= 0]
    gw, gl = sum(wins), -sum(losses)

    peak, mdd, bal = start, 0.0, start
    for t in tr:
        bal += t.pnl
        peak = max(peak, bal)
        mdd = max(mdd, peak - bal)
    eq_peak, eq_mdd = start, 0.0
    for _, e in bt.equity_curve:
        eq_peak = max(eq_peak, e)
        eq_mdd = max(eq_mdd, eq_peak - e)

    days = len(days_pnl)
    rk = bt.risk
    return {
        "outcome": bt.risk.dead or "STILL RUNNING",
        "profile": bt.profile,
        "trading_days_to_pass": _days_to_pass(bt),
        "start_balance": start,
        "end_balance": round(bt.balance, 2),
        "net_pnl": round(bt.balance - start, 2),
        "net_pct": round((bt.balance - start) / start * 100, 2),
        "firm_rules": {
            "drawdown": f"${rk.dd:.0f} {rk.dd_type}",
            "final_drawdown_floor": round(rk.floor, 2),
            "room_left_above_floor": round(bt.balance - rk.floor, 2),
            "daily_loss_allowed_by_bot": round(rk.daily_allow, 2),
            "daily_profit_cap": round(rk.daily_cap, 2),
            "profit_target": rk.target,
            "contract_cap": rk.max_c,
        },
        "best_day": round(best_day, 2),
        "best_day_pct_of_target": round(best_day / rk.target * 100, 1) if rk.target else None,
        "best_day_pct_of_total_profit": round(best_day / total_profit * 100, 1) if total_profit > 0 else None,
        "trades": len(tr),
        "trading_days": days,
        "win_rate_pct": round(len(wins) / len(tr) * 100, 1) if tr else 0,
        "profit_factor": round(gw / gl, 2) if gl else None,
        "avg_win": round(gw / len(wins), 2) if wins else 0,
        "avg_loss": round(-gl / len(losses), 2) if losses else 0,
        "expectancy_per_trade": round((gw - gl) / len(tr), 2) if tr else 0,
        "max_drawdown_closed": round(mdd, 2),
        "max_drawdown_equity": round(eq_mdd, 2),
        "longs": sum(1 for t in tr if t.side > 0),
        "shorts": sum(1 for t in tr if t.side < 0),
        "exit_reasons": dict(Counter(t.reason for t in tr)),
        "signals": bt.signals,
        "skipped_too_small_account": bt.skipped_size,
        "ticks_processed": bt.ticks_seen,
        "large_prints_detected": bt.engine.n_large,
        "data_from": _iso(bt.first_ts) if bt.first_ts else None,
        "data_to": _iso(bt.last_ts) if bt.last_ts else None,
        "risk_events": [f"{_iso(ts)}  {txt}" for ts, txt in bt.risk.events],
    }


def write_outputs(bt, summary: dict, out_dir: str, title: str):
    import os
    os.makedirs(out_dir, exist_ok=True)
    with open(f"{out_dir}/summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)
    with open(f"{out_dir}/trades.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["entry_time", "exit_time", "side", "contracts", "entry", "exit_avg", "pnl", "reason"])
        for t in bt.trades:
            w.writerow([_iso(t.entry_ts), _iso(t.exit_ts), "LONG" if t.side > 0 else "SHORT",
                        t.contracts, f"{t.entry:.2f}", f"{t.exit_avg:.2f}", f"{t.pnl:.2f}", t.reason])
    _plot(bt, summary, f"{out_dir}/equity.png", title)


def _plot(bt, s, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter

    start = s["start_balance"]
    xs = [datetime.fromtimestamp(ts / NS, tz=timezone.utc) for ts, _ in bt.equity_curve]
    ys = [e for _, e in bt.equity_curve]
    fs = [f for _, f in bt.floor_curve]
    fig, ax = plt.subplots(figsize=(10, 4.2), dpi=130)
    ax.plot(xs, ys, color="#2563EB", lw=1.4, label="Equity")
    ax.step(xs, fs, where="post", color="#DC2626", lw=1.0, label=f"Firm drawdown floor ({bt.risk.dd_type})")
    ax.axhline(start, color="#6B7280", lw=0.8, ls=":")
    if bt.risk.target > 0:
        ax.axhline(start + bt.risk.target, color="#16A34A", lw=0.9, ls="--",
                   label=f"Profit target +${bt.risk.target:,.0f}")
    for ts, txt in bt.risk.events:
        ax.axvline(datetime.fromtimestamp(ts / NS, tz=timezone.utc), color="#F59E0B", lw=0.6, alpha=0.6)
    ax.set_title(f"{title}  |  {s['outcome']}  |  net {s['net_pnl']:+.0f} ({s['net_pct']:+.1f}%)  "
                 f"|  {s['trades']} trades  WR {s['win_rate_pct']}%", fontsize=10)
    ax.xaxis.set_major_formatter(DateFormatter("%m-%d"))
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8, loc="upper left")
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
