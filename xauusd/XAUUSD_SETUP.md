# XAUUSD Order-Flow Bot — cTrader + MT5

## How it works

Spot XAUUSD has no central exchange, so your broker's feed has **no real big
trades, no real volume and no buyer/seller side**. Real gold order flow exists
on COMEX gold futures (GC). Spot gold and GC move together tick for tick
(spot sits a roughly constant few dollars away), so the bot:

```
 COMEX GC trades (Databento API)
        │  every trade + aggressor side
        ▼
 hub.py ── order-flow engine: big trades, delta, cumulative delta,
        │  footprint imbalance, absorption, VWAP → entry / exit signals
        │  (local WebSocket ws://127.0.0.1:8765)
        ├──────────────► OrderFlowBridge cBot (cTrader)  → XAUUSD orders
        └──────────────► mt5_executor.py (MetaTrader 5)  → XAUUSD orders
```

Signals carry **distances** (stop $3.00, TP1 $3.00, TP2 $6.00), never futures
prices, so the spot/futures gap doesn't matter. Each executor sizes the trade
for **its own account**, applies **its own prop-firm rules**, and puts a
broker-side stop loss on every order — if the hub, the script or your internet
stops, open positions stay protected.

## What you need

| Piece | Where | Cost |
|---|---|---|
| Databento account + API key | databento.com — GC trades, live feed `GLBX.MDP3` | usage-based; check the quote with `fetch_databento.py` first |
| Python 3.11+ | the PC that runs the hub | free |
| cTrader Desktop | for the FundedHive account | free |
| MetaTrader 5 + Windows | for an MT5 account | free |

I can't create the Databento account or key for you — it needs your details
and payment. Once you have the key, everything else connects by itself.

```
pip install -r requirements.txt
pip install MetaTrader5          # only on the Windows PC running MT5
set DATABENTO_API_KEY=db-xxxxxxxx   (PowerShell: $env:DATABENTO_API_KEY="db-xxxxxxxx")
```

## 1. Start the hub

```
python xauusd/hub.py                                  # live GC order flow
python xauusd/hub.py --replay synthetic --speed 120   # test the plumbing with no data feed
```
Every signal prints with its reasons (big-trade net, delta, stop and targets).
Signals are only sent while at least one executor is connected.

**Never connect a funded or challenge account to `--replay synthetic`** — those
signals are fake. Use a demo account for plumbing tests.

## 2a. cTrader (FundedHive)

1. cTrader → **Algo** → **New cBot**, name it `OrderFlowBridge`, replace the code
   with `xauusd/ctrader/OrderFlowBridge.cs`, click **Build**.
2. Open an **XAUUSD** chart, add an instance of the cBot.
3. Fill in **1. Prop firm rules** from your FundedHive rules page:

| Parameter | Example ($5k 2-step, Phase 1) |
|---|---|
| Profile | Evaluation (pass fast) / Funded (safe) |
| Account size | 5000 |
| Profit target % | 8 (Phase 2: its own target; funded: 0) |
| Max loss % / type | 8 / Static |
| Daily drawdown % | 4 |
| Daily base = higher of balance/equity | on if the daily limit is measured from the higher of the two |
| Day resets at | 1700 (New York) |
| Min profitable days | your firm's number — after the target is hit, the bot keeps trading at quarter size and stops each day once it's in profit, until the days are banked |
| Max lots | the firm's cap |

4. Start the cBot. The log shows `[OFB] connected to hub` and the rules it understood.

The cBot runs with **AccessRights.None**. It must run in **cTrader Desktop on the
same PC as the hub** — cTrader's cloud can't reach `127.0.0.1`. To use the cloud
you'd need to host the hub on a server with a secure `wss://` address.

**FundedHive A-Book:** the bot can't switch account routing — activate A-Book in
your dashboard yourself, as with your other bots.

## 2b. MetaTrader 5

1. In `config_xauusd.toml`, set `[mt5]` → `symbol` to the exact name in Market
   Watch (e.g. `XAUUSD`, `XAUUSD.m`), and the `[propfirm]` numbers in dollars.
2. Open MT5 and log in, enable **Algo Trading**.
3. `python xauusd/mt5_executor.py`

Works on netting and hedging accounts (one position at a time). Lot size uses
MT5's own `order_calc_profit`, so it's right for your broker's contract size.

## Profiles

Same idea as the futures bot — Evaluation pushes to pass, Funded protects the
account. On a $5k / $400-max-loss account:

| | Evaluation | Funded |
|---|---|---|
| Risk per trade | 20% of the room above the max-loss line (~$72 at the start) | 8% (~$29) |
| Daily stop | firm daily limit minus buffer | 20% of max loss ($80) |
| Losing streak | pause 24h after 4 | pause 48h after 3 |

## Charts

```
python orderflow_chart.py --data synthetic --date 2026-06-03
python orderflow_chart.py --data GC_v_0_trades_2026-09-01_2026-09-06.dbn.zst --date 2026-09-03 --from 08:30 --to 12:00
```
Price and VWAP with every big trade (blue circles = aggressive buyers, red
triangles = aggressive sellers, sized by contracts), delta per bar, and
cumulative delta — the same numbers the bot trades on.

## Backtest first

```
python fetch_databento.py --start 2026-07-01 --end 2026-09-30            # quotes the cost
python fetch_databento.py --start 2026-07-01 --end 2026-09-30 --yes
python run_backtest.py --config config_xauusd.toml --data GC_v_0_trades_2026-07-01_2026-09-30.dbn.zst
```
`config_xauusd.toml` uses spot-gold costs (spread, slippage, ~$7/lot
commission) on GC prices, with the $5k rules. Look for a profit factor above
~1.3 after costs across several months before risking a challenge fee.
