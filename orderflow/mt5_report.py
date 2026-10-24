"""MT5 Strategy Tester-style HTML report.

Same sections and metric names as MetaTrader 5's tester report (Settings,
Results, Orders/Deals), plus a monthly breakdown, an hour-of-day breakdown,
MFE/MAE and the prop-firm rule log. Everything is computed from the
backtester's own records — nothing is estimated.
"""
import base64
import html
import io
import math
from collections import defaultdict
from datetime import datetime, timezone

from .clock import ET
from .events import NS


def _et(ts):
    return datetime.fromtimestamp(ts / NS, tz=ET)


def _fmt(x, d=2):
    if x is None:
        return "-"
    return f"{x:,.{d}f}".replace(",", " ")


def _streaks(pnls):
    """MT5 consecutive-trade statistics."""
    best = {"win_n": 0, "win_usd": 0.0, "loss_n": 0, "loss_usd": 0.0,
            "max_profit_usd": 0.0, "max_profit_n": 0, "max_loss_usd": 0.0, "max_loss_n": 0}
    runs_w, runs_l = [], []
    cur_sign, cur_n, cur_usd = 0, 0, 0.0

    def close_run():
        if cur_n == 0:
            return
        if cur_sign > 0:
            runs_w.append(cur_n)
            if cur_n > best["win_n"] or (cur_n == best["win_n"] and cur_usd > best["win_usd"]):
                best["win_n"], best["win_usd"] = cur_n, cur_usd
            if cur_usd > best["max_profit_usd"]:
                best["max_profit_usd"], best["max_profit_n"] = cur_usd, cur_n
        else:
            runs_l.append(cur_n)
            if cur_n > best["loss_n"] or (cur_n == best["loss_n"] and cur_usd < best["loss_usd"]):
                best["loss_n"], best["loss_usd"] = cur_n, cur_usd
            if cur_usd < best["max_loss_usd"]:
                best["max_loss_usd"], best["max_loss_n"] = cur_usd, cur_n

    for p in pnls:
        s = 1 if p > 0 else -1
        if s != cur_sign:
            close_run()
            cur_sign, cur_n, cur_usd = s, 0, 0.0
        cur_n += 1
        cur_usd += p
    close_run()
    best["avg_win_run"] = sum(runs_w) / len(runs_w) if runs_w else 0
    best["avg_loss_run"] = sum(runs_l) / len(runs_l) if runs_l else 0
    return best


def _drawdowns(start, series):
    """Absolute, maximal ($ and %), relative (% and $) — MT5 definitions."""
    peak = start
    absolute = max(0.0, start - min([start] + series))
    max_usd = max_usd_pct = 0.0
    rel_pct = rel_pct_usd = 0.0
    for v in series:
        if v > peak:
            peak = v
        dd = peak - v
        pct = dd / peak * 100 if peak > 0 else 0
        if dd > max_usd:
            max_usd, max_usd_pct = dd, pct
        if pct > rel_pct:
            rel_pct, rel_pct_usd = pct, dd
    return absolute, max_usd, max_usd_pct, rel_pct, rel_pct_usd


def compute(bt) -> dict:
    tr = bt.trades
    start = float(bt.cfg["propfirm"]["account_size"])
    pnls = [t.pnl for t in tr]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp, gl = sum(wins), sum(losses)
    net = bt.balance - start

    bal_series, b = [], start
    for p in pnls:
        b += p
        bal_series.append(b)
    eq_series = [e for _, e in bt.equity_curve]
    bal_dd = _drawdowns(start, bal_series)
    eq_dd = _drawdowns(start, eq_series)

    # daily returns for Sharpe (closed P&L per trading day)
    day_pnl = defaultdict(float)
    for t in tr:
        day_pnl[bt.clock.day_id(t.exit_ts)] += t.pnl
    rets, bal = [], start
    for d in sorted(day_pnl):
        rets.append(day_pnl[d] / bal)
        bal += day_pnl[d]
    sharpe = None
    if len(rets) > 1:
        m = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
        sharpe = m / sd * math.sqrt(252) if sd > 0 else None

    longs = [t for t in tr if t.side > 0]
    shorts = [t for t in tr if t.side < 0]
    months = defaultdict(list)
    hours = defaultdict(list)
    for t in tr:
        months[_et(t.exit_ts).strftime("%Y-%m")].append(t.pnl)
        hours[_et(t.entry_ts).hour].append(t.pnl)

    return {
        "start": start, "net": net, "gp": gp, "gl": gl,
        "pf": gp / -gl if gl < 0 else None,
        "expected": net / len(tr) if tr else 0,
        "recovery": net / eq_dd[1] if eq_dd[1] > 0 else None,
        "sharpe": sharpe,
        "bal_dd": bal_dd, "eq_dd": eq_dd,
        "trades": len(tr), "deals": len(bt.deals),
        "longs": len(longs), "long_won": sum(t.pnl > 0 for t in longs),
        "shorts": len(shorts), "short_won": sum(t.pnl > 0 for t in shorts),
        "wins": len(wins), "losses": len(losses),
        "largest_win": max(wins) if wins else 0, "largest_loss": min(losses) if losses else 0,
        "avg_win": gp / len(wins) if wins else 0, "avg_loss": gl / len(losses) if losses else 0,
        "streaks": _streaks(pnls),
        "avg_mfe": sum(t.mfe for t in tr) / len(tr) if tr else 0,
        "avg_mae": sum(t.mae for t in tr) / len(tr) if tr else 0,
        "months": dict(sorted(months.items())), "hours": dict(sorted(hours.items())),
        "trading_days": len(day_pnl),
        "best_day": max(day_pnl.values()) if day_pnl else 0,
        "worst_day": min(day_pnl.values()) if day_pnl else 0,
    }


def _chart_png(bt, start):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter

    BAL, EQ, FLOOR, INK2, MUTED, GRID, SURF = "#2a78d6", "#eb6834", "#e34948", "#52514e", "#898781", "#e1e0d9", "#ffffff"
    xs = [_et(ts) for ts, _ in bt.equity_curve]
    eq = [e for _, e in bt.equity_curve]
    fl = [f for _, f in bt.floor_curve]
    bx, by, b = [xs[0]] if xs else [], [start] if xs else [], start
    for t in bt.trades:
        b += t.pnl
        bx.append(_et(t.exit_ts)); by.append(b)

    fig, ax = plt.subplots(figsize=(11, 4), dpi=120)
    fig.patch.set_facecolor(SURF); ax.set_facecolor(SURF)
    if xs:
        ax.plot(xs, eq, color=EQ, lw=1.2, label="Equity")
        ax.step(bx, by, where="post", color=BAL, lw=2, label="Balance")
        ax.step(xs, fl, where="post", color=FLOOR, lw=1.2, ls=(0, (4, 3)), label="Firm drawdown floor")
    ax.axhline(start, color=MUTED, lw=1)
    if bt.risk.target > 0:
        ax.axhline(start + bt.risk.target, color=INK2, lw=1, ls=":", label="Profit target")
    ax.grid(axis="y", color=GRID, lw=1); ax.set_axisbelow(True)
    for sp in ("top", "right", "left"):
        ax.spines[sp].set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.xaxis.set_major_formatter(DateFormatter("%Y-%m-%d", tz=ET))
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), frameon=False, ncol=4, fontsize=8)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURF)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _kv_rows(pairs, cols=3):
    """MT5-style results grid: label/value pairs laid out in `cols` column pairs."""
    out = []
    for i in range(0, len(pairs), cols):
        cells = "".join(f'<td class="k">{html.escape(k)}:</td><td class="v">{html.escape(v)}</td>'
                        for k, v in pairs[i:i + cols])
        out.append(f"<tr>{cells}</tr>")
    return "\n".join(out)


def write_html(bt, path: str, title: str, data_desc: str, synthetic: bool):
    c = compute(bt)
    s = c["streaks"]
    cfg = bt.cfg
    pf_, inst = cfg["propfirm"], cfg["instrument"]
    ab, mx, mxp, rp, rpu = c["bal_dd"]
    eab, emx, emxp, erp, erpu = c["eq_dd"]
    first = _et(bt.first_ts).strftime("%Y.%m.%d") if bt.first_ts else "-"
    last = _et(bt.last_ts).strftime("%Y.%m.%d") if bt.last_ts else "-"

    settings = [
        ("Expert", inst.get("report_expert", "OrderFlowFollower (order-flow follower)")),
        ("Symbol", f"{inst['exec_symbol']} (flow read from {inst['data_symbol']})"),
        ("Period", f"Tick data ({first} - {last}, New York time)"),
        ("Data", data_desc),
        ("Profile", bt.profile),
        ("Initial Deposit", _fmt(c["start"])),
        ("Rules", f"target {_fmt(pf_['profit_target'], 0)} | max drawdown {_fmt(pf_['max_drawdown'], 0)} "
                  f"{pf_['drawdown_type']} | daily loss limit {_fmt(pf_['daily_loss_limit'], 0)} | "
                  f"consistency {pf_['consistency_pct']}% | safety buffer {_fmt(pf_['safety_buffer'], 0)}"),
        ("Costs", f"commission {inst['commission_per_side']} per side per unit | entry slippage "
                  f"{cfg['execution']['entry_slippage_ticks']} ticks | stop slippage "
                  f"{cfg['execution']['stop_slippage_ticks']} ticks | latency {cfg['execution']['latency_ms']} ms"),
        ("Outcome", bt.risk.dead or "Still trading at end of data"),
    ]
    results = [
        ("Total Net Profit", _fmt(c["net"])), ("Balance Drawdown Absolute", _fmt(ab)),
        ("Equity Drawdown Absolute", _fmt(eab)),
        ("Gross Profit", _fmt(c["gp"])), ("Balance Drawdown Maximal", f"{_fmt(mx)} ({mxp:.2f}%)"),
        ("Equity Drawdown Maximal", f"{_fmt(emx)} ({emxp:.2f}%)"),
        ("Gross Loss", _fmt(c["gl"])), ("Balance Drawdown Relative", f"{rp:.2f}% ({_fmt(rpu)})"),
        ("Equity Drawdown Relative", f"{erp:.2f}% ({_fmt(erpu)})"),
        ("Profit Factor", _fmt(c["pf"])), ("Expected Payoff", _fmt(c["expected"])),
        ("Recovery Factor", _fmt(c["recovery"])),
        ("Sharpe Ratio (daily, annualised)", _fmt(c["sharpe"])), ("Trading Days", str(c["trading_days"])),
        ("Return", f"{c['net'] / c['start'] * 100:+.2f}%"),
    ]
    pct = lambda a, b: f"{a / b * 100:.2f}%" if b else "-"
    trades_tbl = [
        ("Total Trades", str(c["trades"])), ("Short Trades (won %)", f"{c['shorts']} ({pct(c['short_won'], c['shorts'])})"),
        ("Long Trades (won %)", f"{c['longs']} ({pct(c['long_won'], c['longs'])})"),
        ("Total Deals", str(c["deals"])), ("Profit Trades (% of total)", f"{c['wins']} ({pct(c['wins'], c['trades'])})"),
        ("Loss Trades (% of total)", f"{c['losses']} ({pct(c['losses'], c['trades'])})"),
        ("Largest profit trade", _fmt(c["largest_win"])), ("Largest loss trade", _fmt(c["largest_loss"])),
        ("Best day", _fmt(c["best_day"])),
        ("Average profit trade", _fmt(c["avg_win"])), ("Average loss trade", _fmt(c["avg_loss"])),
        ("Worst day", _fmt(c["worst_day"])),
        ("Maximum consecutive wins ($)", f"{s['win_n']} ({_fmt(s['win_usd'])})"),
        ("Maximum consecutive losses ($)", f"{s['loss_n']} ({_fmt(s['loss_usd'])})"),
        ("Average MFE per trade", _fmt(c["avg_mfe"])),
        ("Maximal consecutive profit (count)", f"{_fmt(s['max_profit_usd'])} ({s['max_profit_n']})"),
        ("Maximal consecutive loss (count)", f"{_fmt(s['max_loss_usd'])} ({s['max_loss_n']})"),
        ("Average MAE per trade", _fmt(c["avg_mae"])),
        ("Average consecutive wins", f"{s['avg_win_run']:.0f}"),
        ("Average consecutive losses", f"{s['avg_loss_run']:.0f}"),
        ("Signals (incl. skipped)", f"{bt.signals} ({bt.skipped_size} skipped: risk budget)"),
    ]

    def breakdown(groups, label_fn):
        rows = []
        for k, ps in groups.items():
            w = [p for p in ps if p > 0]; l = [p for p in ps if p <= 0]
            pfv = sum(w) / -sum(l) if sum(l) < 0 else None
            rows.append(f"<tr><td>{html.escape(label_fn(k))}</td><td>{len(ps)}</td><td>{pct(len(w), len(ps))}</td>"
                        f"<td>{_fmt(sum(ps))}</td><td>{_fmt(pfv)}</td></tr>")
        return "\n".join(rows) or '<tr><td colspan="5">No trades</td></tr>'

    unit = float(inst.get("report_volume_per_unit", 1))   # e.g. 0.01 = backtest units are 0.01 lots
    deals = []
    for i, d in enumerate(bt.deals, 1):
        typ = "buy" if d["side"] > 0 else "sell"
        deals.append(
            f"<tr><td>{_et(d['ts']).strftime('%Y.%m.%d %H:%M:%S')}</td><td>{i}</td><td>{html.escape(inst['exec_symbol'])}</td>"
            f"<td>{typ}</td><td>{d['dir']}</td><td>{d['n'] * unit:g}</td><td>{d['price']:.2f}</td>"
            f"<td>{_fmt(d['commission'])}</td><td>{_fmt(d['profit']) if d['dir'] == 'out' else ''}</td>"
            f"<td>{_fmt(d['balance'])}</td><td>{html.escape(d['comment'])}</td></tr>")
    events = "\n".join(f"<tr><td>{_et(ts).strftime('%Y.%m.%d %H:%M:%S')}</td><td>{html.escape(txt)}</td></tr>"
                       for ts, txt in bt.risk.events) or '<tr><td colspan="2">No rule events</td></tr>'
    chart = _chart_png(bt, c["start"])
    banner = ('<div class="warn">SYNTHETIC TEST DATA — this report checks that the report works. '
              'It says nothing about real-market performance.</div>') if synthetic else ""

    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Strategy Tester Report — {html.escape(title)}</title>
<style>
body{{font:13px Tahoma,Arial,sans-serif;color:#0b0b0b;background:#fff;margin:24px auto;max-width:1150px;padding:0 16px}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:28px 0 8px;border-bottom:1px solid #e1e0d9;padding-bottom:4px}}
.sub{{color:#52514e;margin-bottom:12px}}
table{{border-collapse:collapse;width:100%}} td,th{{padding:4px 8px;text-align:left;vertical-align:top}}
.grid td.k{{color:#52514e;white-space:nowrap}} .grid td.v{{font-weight:600;white-space:nowrap}}
.grid tr:nth-child(odd){{background:#f7f7f5}}
.list th{{background:#f0efec;font-weight:600;border-bottom:1px solid #e1e0d9}} .list td{{border-bottom:1px solid #f0efec}}
.list td:nth-child(n+2){{font-variant-numeric:tabular-nums}}
.warn{{background:#fff3d6;border:1px solid #fab219;padding:10px 12px;margin:12px 0;font-weight:600}}
.scroll{{max-height:520px;overflow:auto;border:1px solid #e1e0d9}}
img{{max-width:100%}}
</style></head><body>
<h1>Strategy Tester Report</h1>
<div class="sub">{html.escape(title)}</div>
{banner}
<h2>Settings</h2>
<table class="grid">{"".join(f'<tr><td class="k">{html.escape(k)}:</td><td>{html.escape(v)}</td></tr>' for k, v in settings)}</table>
<h2>Results</h2>
<table class="grid">{_kv_rows(results)}</table>
<h2>Trades</h2>
<table class="grid">{_kv_rows(trades_tbl)}</table>
<h2>Balance / Equity</h2>
<img alt="Balance, equity and firm drawdown floor over the test" src="data:image/png;base64,{chart}">
<h2>Monthly breakdown</h2>
<table class="list"><tr><th>Month</th><th>Trades</th><th>Win %</th><th>Net profit</th><th>Profit factor</th></tr>
{breakdown(c["months"], str)}</table>
<h2>By entry hour (New York)</h2>
<table class="list"><tr><th>Hour</th><th>Trades</th><th>Win %</th><th>Net profit</th><th>Profit factor</th></tr>
{breakdown(c["hours"], lambda h: f"{h:02d}:00")}</table>
<h2>Prop-firm rule events</h2>
<table class="list"><tr><th>Time</th><th>Event</th></tr>{events}</table>
<h2>Deals</h2>
<div class="scroll"><table class="list"><tr><th>Time</th><th>Deal</th><th>Symbol</th><th>Type</th><th>Direction</th>
<th>Volume</th><th>Price</th><th>Commission</th><th>Profit</th><th>Balance</th><th>Comment</th></tr>
{"".join(deals) or '<tr><td colspan="11">No deals</td></tr>'}</table></div>
</body></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return c
