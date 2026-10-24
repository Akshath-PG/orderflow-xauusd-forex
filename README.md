# Order-Flow Bot — Spot Gold XAUUSD (cTrader + MT5)

Spot XAUUSD has no central exchange, so a forex broker's feed has **no real big
trades, no real volume and no buyer/seller side**. This bot reads real gold
order flow from **COMEX gold futures (GC)**, which move with spot gold tick for
tick, and trades spot XAUUSD on a cTrader and/or MetaTrader 5 account.

```
 COMEX GC trades (Databento API)
        │  every trade + aggressor side
        ▼
 xauusd/hub.py ── order-flow engine: big trades, delta, cumulative delta,
        │         footprint imbalance, absorption, VWAP → entry / exit signals
        │         (local WebSocket ws://127.0.0.1:8765)
        ├──► xauusd/ctrader/OrderFlowBridge.cs  (cTrader cBot)   → XAUUSD orders
        └──► xauusd/mt5_executor.py             (MetaTrader 5)   → XAUUSD orders
```

Signals carry price **distances** (stop, TP1, TP2), not futures prices, so the
spot/futures gap doesn't matter. Each executor sizes trades for **its own
account**, enforces **its own prop-firm rules**, and puts a broker-side stop
loss on every order — positions stay protected if anything disconnects.

**Full setup:** [`xauusd/XAUUSD_SETUP.md`](xauusd/XAUUSD_SETUP.md)

## Contents

| Path | What it is |
|---|---|
| `xauusd/hub.py` | Reads GC order flow (live, or replays a file / synthetic tape) and broadcasts signals |
| `xauusd/ctrader/OrderFlowBridge.cs` | cTrader cBot executor — `AccessRights.None`, prop-firm rules in % |
| `xauusd/mt5_executor.py` + `executor_core.py` | MT5 executor via the `MetaTrader5` Python library |
| `orderflow/` | Order-flow engine, strategy, prop-firm risk engine, event-driven backtester |
| `config_xauusd.toml` / `config_xauusd_funded.toml` | All settings: rules ($5k 2-step example), Evaluation / Funded profile, costs, hub, MT5 |
| `orderflow_chart.py` | Price + VWAP with big-trade bubbles, delta per bar, cumulative delta |
| `run_backtest.py`, `robustness_sweep.py`, `fetch_databento.py` | Backtesting on real GC data with spot-gold costs |

## Prop-firm rules + profiles

Max loss (static / end-of-day trailing / intraday trailing), daily drawdown,
profit target, **minimum profitable days** (after the target the bot trades at
quarter size and stops each day once in profit until enough days are banked),
lot cap. The bot stops a safety buffer before every limit and never opens a
trade whose stop-out could breach one.

| Profile | Use for | Risk per trade ($5k / $400 max loss) |
|---|---|---|
| **Evaluation** | passing the phase fast | 20% of the room above the max-loss line (~$72) |
| **Funded** | keeping the account | 8% (~$29), $80 daily stop, 3-loss cooldown |

## Quick start

```
pip install -r requirements.txt
python tests/test_fills.py && python tests/test_risk.py   # engine + rule checks
python tests/test_xauusd_bridge.py                        # hub -> MT5 executor end to end (fake MT5)
python xauusd/hub.py --replay synthetic --speed 120       # plumbing test, no data feed needed
```

Live: set `DATABENTO_API_KEY`, run `python xauusd/hub.py`, then start the cBot in
cTrader Desktop on the same PC and/or `python xauusd/mt5_executor.py`.
**Never connect a real or challenge account to `--replay synthetic`.**

## Real-data backtest, March to today (MT5-style reports)

```
pip install -r requirements.txt
set DATABENTO_API_KEY=db-xxxxxxxx            # new Databento accounts get free credit
python run_real_backtest.py --start 2026-03-01           # shows the cost per month, downloads nothing
python run_real_backtest.py --start 2026-03-01 --yes     # downloads GC trades month by month and runs
```
Or with GC data exported from NinjaTrader (`ninjatrader/OrderFlowTickExporter.cs`, GC, 1-tick, Tick Replay on):
```
python run_real_backtest.py --files "orderflow_*.csv" --start 2026-03-01
```
Writes `reports/<date>/`: `funded_continuous.html` (funded profile through the whole period),
`challenge_attempts.html` (a fresh evaluation started on the 1st of each month, up to 45 days each)
and one full report per attempt. Each report has the MT5 tester layout: settings, results,
drawdowns, trade statistics, balance/equity chart, monthly and hourly breakdowns, prop-firm
rule events and the full deals list.

## Read this before trusting any number

`results/` comes from a **synthetic** tape with a planted edge — it proves the
machinery, not real profitability. $5k rules with spot-gold costs, 22 trading
days, 10 runs: planted edge → 10/10 passed (median 9 days); no edge → 0/10
passed, about −6.7%, none broke the max-loss limit.

Next: backtest 3 months of real GC data with `config_xauusd.toml`, then run the
hub against a **demo** account before a challenge. FundedHive A-Book routing must
be switched on manually in the dashboard.
