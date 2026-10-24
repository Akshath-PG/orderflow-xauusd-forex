"""Order-flow chart for one session: price + VWAP with big-trade bubbles,
delta per bar, and cumulative delta — computed by the same engine the bots use.

  python orderflow_chart.py --data synthetic --date 2026-06-02
  python orderflow_chart.py --data GC_v_0_trades_2026-09-01_2026-09-06.dbn.zst --date 2026-09-03 --from 08:30 --to 12:00
  python orderflow_chart.py --data orderflow_MGC_12-26_....csv --date 2026-10-08

Times are New York. Big trades: blue circles = aggressive buyers,
red triangles = aggressive sellers, sized by contracts.
"""
import argparse
import tomllib
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                       # noqa: E402
from matplotlib.dates import DateFormatter             # noqa: E402

from orderflow import OrderFlowEngine                  # noqa: E402
from orderflow import data_sources as ds               # noqa: E402
from orderflow.clock import ET                         # noqa: E402
from orderflow.events import NS                        # noqa: E402

SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9"
BUY, SELL = "#2a78d6", "#e34948"                       # diverging poles: buyers vs sellers


def et(ts):
    return datetime.fromtimestamp(ts / NS, tz=ET)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config_xauusd.toml")
    ap.add_argument("--data", default="synthetic")
    ap.add_argument("--date", required=True, help="session date, YYYY-MM-DD (New York)")
    ap.add_argument("--from", dest="t_from", default="08:30")
    ap.add_argument("--to", dest="t_to", default="16:00")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    cfg = tomllib.load(open(a.config, "rb"))

    if a.data == "synthetic":
        start = datetime.fromisoformat(a.date) - timedelta(days=3)
        ticks = ds.synthetic_tape(days=5, start_date=start.strftime("%Y-%m-%d"), tick=cfg["instrument"]["tick_size"])
    elif a.data.endswith((".dbn", ".dbn.zst")):
        ticks = ds.load_databento(a.data)
    else:
        ticks = ds.load_csv(a.data)

    d = datetime.fromisoformat(a.date).replace(tzinfo=ET)
    h0, m0 = map(int, a.t_from.split(":")); h1, m1 = map(int, a.t_to.split(":"))
    w0 = int(d.replace(hour=h0, minute=m0).timestamp() * NS)
    w1 = int(d.replace(hour=h1, minute=m1).timestamp() * NS)

    eng = OrderFlowEngine(cfg["orderflow"], cfg["instrument"]["tick_size"])
    eng.large_log = []
    bars = []
    for t in ticks:
        if t.ts > w1 + 120 * NS:
            break
        for f in eng.on_tick(t):
            if w0 <= f.ts_open < w1:
                bars.append(f)
    big = [b for b in eng.large_log if w0 <= b[0] < w1]
    if not bars:
        raise SystemExit("No trades in that window - check --date / --from / --to against the data.")

    x = [et(b.ts_open) for b in bars]
    cum, c = [], 0
    for b in bars:
        c += b.delta
        cum.append(c)

    plt.rcParams.update({"font.size": 9, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
                         "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK})
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(12, 8.5), dpi=130, sharex=True,
                                        gridspec_kw={"height_ratios": [3, 1.2, 1.2]})
    fig.patch.set_facecolor(SURFACE)
    for ax in (ax1, ax2, ax3):
        ax.set_facecolor(SURFACE)
        ax.grid(axis="y", color=GRID, lw=1)
        ax.set_axisbelow(True)
        for sp in ("top", "right", "left"):
            ax.spines[sp].set_visible(False)

    # 1. price, VWAP, big trades
    ax1.plot(x, [b.close for b in bars], color=INK2, lw=2, label="Price (1-min close)")
    ax1.plot(x, [b.vwap for b in bars], color=MUTED, lw=2, ls=(0, (4, 3)), label="Session VWAP")
    smax = max([s for _, _, s, _ in big] or [1])
    for side, col, mk, lab in ((1, BUY, "o", "Big buyer (aggressive)"), (-1, SELL, "v", "Big seller (aggressive)")):
        pts = [(et(ts), px, sz) for ts, px, sz, sd in big if sd == side]
        if pts:
            ax1.scatter([p[0] for p in pts], [p[1] for p in pts], s=[30 + 270 * p[2] / smax for p in pts],
                        c=col, marker=mk, alpha=0.75, edgecolors=SURFACE, linewidths=2, label=lab, zorder=3)
    ax1.set_ylabel("Price")
    ax1.legend(loc="lower left", bbox_to_anchor=(0, 1.0), frameon=False, ncol=4, borderaxespad=0.2)
    nb = sum(1 for b in big if b[3] > 0); ns_ = len(big) - nb
    ax1.set_title(f"Order flow  |  {a.date}  {a.t_from}-{a.t_to} New York  |  "
                  f"{nb} big buys, {ns_} big sells  |  {len(bars)} bars", loc="left", fontsize=11, color=INK, pad=26)

    # 2. delta per bar
    width = timedelta(seconds=cfg["orderflow"]["bar_seconds"] * 0.8)
    ax2.bar(x, [b.delta for b in bars], width=width, align="edge",
            color=[BUY if b.delta >= 0 else SELL for b in bars], edgecolor=SURFACE, linewidth=0.5)
    ax2.axhline(0, color=MUTED, lw=1)
    ax2.set_ylabel("Delta / bar\n(buy - sell)")

    # 3. cumulative delta
    ax3.plot(x, cum, color=INK2, lw=2)
    ax3.fill_between(x, cum, 0, where=[v >= 0 for v in cum], color=BUY, alpha=0.15, lw=0)
    ax3.fill_between(x, cum, 0, where=[v < 0 for v in cum], color=SELL, alpha=0.15, lw=0)
    ax3.axhline(0, color=MUTED, lw=1)
    ax3.set_ylabel("Cumulative\ndelta")
    ax3.xaxis.set_major_formatter(DateFormatter("%H:%M", tz=ET))

    fig.tight_layout()
    out = a.out or f"orderflow_{a.date}.png"
    fig.savefig(out, facecolor=SURFACE)
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
