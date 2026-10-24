"""Where ticks come from. Every source yields Tick objects in time order,
so the backtester doesn't care which one it's fed.

  * load_databento(path)  — a .dbn / .dbn.zst trades file from Databento (real CME tape)
  * load_csv(path)        — your own tick CSV: ts,price,size,side
  * synthetic_tape(...)   — generated tape for testing the plumbing ONLY
  * databento_live(sym)   — live CME trades for paper trading
"""
import csv
import os
from datetime import datetime, timezone, timedelta

import numpy as np

from .events import Tick, NS

_SIDE = {"B": 1, "A": -1, "BUY": 1, "SELL": -1, "1": 1, "-1": -1, "S": -1}


def _side(v) -> int:
    # Databento trades: side 'B' = buyer was the aggressor, 'A' = seller was.
    v = getattr(v, "value", v)
    return _SIDE.get(str(v).strip().upper(), 0)


def load_databento(path: str, symbol: str | None = None):
    """Yield Ticks from a Databento trades file (schema='trades')."""
    import databento as db   # pip install databento
    df = db.DBNStore.from_file(path).to_df().reset_index()
    if symbol and "symbol" in df.columns:
        df = df[df["symbol"] == symbol]
    ts = df["ts_event"].astype("int64").to_numpy()
    px = df["price"].to_numpy(dtype=float)
    sz = df["size"].to_numpy(dtype=int)
    sd = df["side"].to_numpy()
    order = np.argsort(ts, kind="stable")
    for i in order:
        yield Tick(int(ts[i]), float(px[i]), int(sz[i]), _side(sd[i]))


def load_csv(path: str):
    """CSV with header ts,price,size,side. ts = epoch ns or ISO-8601 UTC."""
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            raw = row["ts"].strip()
            if raw.isdigit():
                ts = int(raw)
            else:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                ts = int(dt.timestamp() * NS)
            yield Tick(ts, float(row["price"]), int(float(row["size"])), _side(row["side"]))


def databento_live(symbol: str = "GC.v.0", dataset: str = "GLBX.MDP3"):
    """Yield live Ticks from Databento's live API (needs DATABENTO_API_KEY).
    Untested in this sandbox — verify field names against the Databento docs
    for your client version before relying on it."""
    import databento as db
    client = db.Live(key=os.environ["DATABENTO_API_KEY"])
    client.subscribe(dataset=dataset, schema="trades", stype_in="continuous", symbols=[symbol])
    for rec in client:
        if isinstance(rec, db.TradeMsg):
            yield Tick(int(rec.ts_event), rec.price / 1e9, int(rec.size), _side(rec.side))


# ----------------------------------------------------------------------
def synthetic_tape(days=15, seed=7, p_informed=0.55, start_price=4000.0, tick=0.10,
                   session_start_utc=12, session_hours=8, start_date="2026-06-01",
                   prints_per_sec=2.0, sigma_ticks=1.6, informed_drift=0.20):
    """Generate a gold-futures-like trade tape.

    Large-trader 'episodes' appear about every 20 minutes. With probability
    p_informed an episode is INFORMED (big aggressive prints AND price drifts
    their way); otherwise it's NOISE (same big prints, no follow-through —
    hedges, liquidity). p_informed=0 is a tape with zero edge, used as a
    control: a sound backtester must NOT find profit there.

    This tests the plumbing. A planted edge proves nothing about real markets.
    """
    rng = np.random.default_rng(seed)
    d0 = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
    mid = start_price / tick
    secs = int(session_hours * 3600)
    day, made = 0, 0
    while made < days:
        dt = d0 + timedelta(days=day)
        day += 1
        if dt.weekday() >= 5:
            continue
        made += 1
        base_ns = int((dt.timestamp() + session_start_utc * 3600) * NS)

        drift = np.zeros(secs)
        buyp = np.full(secs, 0.5)
        lrate = np.full(secs, 1 / 400)       # stray noise blocks
        lside = rng.choice([-1, 1], size=secs)
        for _ in range(rng.poisson(secs / 1200)):
            st = int(rng.integers(0, secs - 60))
            dur = int(rng.integers(180, 600))
            sl = slice(st, min(secs, st + dur))
            d = int(rng.choice([-1, 1]))
            lrate[sl] += 1 / 25
            lside[sl] = d
            if rng.random() < p_informed:
                drift[sl] += d * informed_drift
                buyp[sl] = 0.5 + d * 0.10
            else:
                drift[sl] -= d * 0.03              # big prints get faded
                buyp[sl] = 0.5 + d * 0.04

        path = mid + np.cumsum(drift + sigma_ticks * rng.standard_normal(secs))
        mid = path[-1]
        bid = np.floor(path)

        cnt = rng.poisson(prints_per_sec, secs)
        idx = np.repeat(np.arange(secs), cnt)
        n = idx.size
        side = np.where(rng.random(n) < buyp[idx], 1, -1)
        size = rng.geometric(0.45, n)
        px_t = bid[idx] + (side > 0)              # buys at the ask, sells at the bid
        ts = base_ns + idx.astype(np.int64) * NS + rng.integers(1, NS, n)

        bcnt = rng.poisson(lrate)
        bidx = np.repeat(np.arange(secs), bcnt)
        b_ts, b_px, b_sz, b_sd = [], [], [], []
        for i in bidx:
            s = int(lside[i]) if lrate[i] > 1 / 400 else int(rng.choice([-1, 1]))
            total = int(rng.integers(15, 61))
            k = int(rng.integers(2, 6))
            parts = np.diff(np.sort(np.concatenate(([0, total], rng.integers(1, total, k - 1)))))
            t0 = base_ns + int(i) * NS + int(rng.integers(1, NS))
            lvl0 = bid[i] + (s > 0)
            for j, part in enumerate(parts):
                if part <= 0:
                    continue
                b_ts.append(t0); b_px.append(lvl0 + s * j); b_sz.append(int(part)); b_sd.append(s)

        all_ts = np.concatenate([ts, np.array(b_ts, dtype=np.int64)])
        all_px = np.concatenate([px_t, np.array(b_px, dtype=float)]) * tick
        all_sz = np.concatenate([size, np.array(b_sz, dtype=np.int64)])
        all_sd = np.concatenate([side, np.array(b_sd, dtype=np.int64)])
        order = np.argsort(all_ts, kind="stable")
        for i in order:
            yield Tick(int(all_ts[i]), round(float(all_px[i]), 2), int(all_sz[i]), int(all_sd[i]))
