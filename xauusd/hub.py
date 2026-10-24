"""SIGNAL HUB — reads COMEX gold futures order flow, broadcasts XAUUSD trade signals.

Spot XAUUSD has no central tape, so the big trades / delta / footprint come
from GC futures. This process runs the same order-flow engine and strategy as
the futures bot, and publishes every entry and exit as a JSON message on a
local WebSocket. Executors (the cTrader cBot and/or mt5_executor.py) connect,
size the trade for their own account, apply their own prop-firm rules, and
place the XAUUSD orders.

  python xauusd/hub.py                                   # live GC from Databento
  python xauusd/hub.py --replay synthetic --speed 120    # test without any data feed
  python xauusd/hub.py --replay my_gc_ticks.csv --speed 30

Live mode needs:  pip install databento   and   DATABENTO_API_KEY set.
"""
import argparse
import asyncio
import copy
import json
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from websockets.asyncio.server import serve           # noqa: E402

from orderflow import Backtester                       # noqa: E402
from orderflow import data_sources as ds               # noqa: E402
from orderflow.events import NS                        # noqa: E402

PROTOCOL_VERSION = 1


def shadow_config(cfg: dict) -> dict:
    """The hub tracks a 1-lot 'shadow' position only to know when to exit and
    when the post-trade cooldown ends. Account rules are NOT applied here —
    each executor applies its own account's rules."""
    c = copy.deepcopy(cfg)
    c["risk"]["profile"] = "custom"
    c["risk"].update(use_profit_lock=False, use_streak_cooldown=False, use_adaptive_risk=False,
                     risk_pct_of_room=50.0, max_contracts=1)
    c["propfirm"].update(account_size=1e9, profit_target=0, max_drawdown=1e8, drawdown_type="static",
                         daily_loss_limit=0, bot_daily_stop=1e8, max_contracts_firm=1,
                         consistency_pct=0, daily_profit_cap=0, safety_buffer=0)
    return c


def _now():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


class Hub:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.bt = Backtester(shadow_config(cfg), research=True)
        self.bt.on_event = self.publish
        self.clients = set()
        self.loop = None
        self.queue = None
        self.last_price = None
        self.last_ts = None
        self.ticks = 0
        self.tick_listeners = []      # callbacks(Tick) — used by the test harness
        self.done = threading.Event()

    # ---------------- feed thread ----------------
    def publish(self, ev: dict):
        ev["v"] = PROTOCOL_VERSION
        line = json.dumps(ev)
        if ev["type"] == "signal":
            print(f"{_now()} SIGNAL #{ev['id']} {'LONG' if ev['side'] > 0 else 'SHORT'} "
                  f"| big-trade net {ev['large_net']} | delta {ev['roll_delta']} "
                  f"| SL {ev['sl_dist']} TP1 {ev['tp1_dist']} TP2 {ev['tp2_dist']}", flush=True)
        else:
            print(f"{_now()} exit #{ev['id']} ({ev['reason']})", flush=True)
        if self.loop:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, line)

    def run_feed(self, ticks, speed: float):
        t0_wall = time.monotonic()
        t0_data = None
        try:
            for t in ticks:
                if speed > 0:
                    if t0_data is None:
                        t0_data = t.ts
                    due = (t.ts - t0_data) / NS / speed
                    lag = due - (time.monotonic() - t0_wall)
                    if lag > 0:
                        time.sleep(min(lag, 1.0))
                self.ticks += 1
                self.last_price, self.last_ts = t.price, t.ts
                for cb in self.tick_listeners:
                    cb(t)
                self.bt.on_tick(t)
        except Exception as exc:                    # feed died — keep the hub up, say so loudly
            print(f"{_now()} FEED ERROR: {exc!r}", flush=True)
        finally:
            self.done.set()

    # ---------------- asyncio side ----------------
    async def _handler(self, ws):
        self.clients.add(ws)
        print(f"{_now()} executor connected ({len(self.clients)} total)", flush=True)
        await ws.send(json.dumps({"type": "hello", "v": PROTOCOL_VERSION,
                                  "flow_symbol": self.cfg["instrument"]["data_symbol"]}))
        try:
            async for msg in ws:                     # executors report fills / status
                print(f"{_now()} executor: {msg}", flush=True)
        except Exception:
            pass
        finally:
            self.clients.discard(ws)
            print(f"{_now()} executor disconnected ({len(self.clients)} left)", flush=True)

    async def _broadcaster(self):
        while True:
            line = await self.queue.get()
            for ws in list(self.clients):
                try:
                    await ws.send(line)
                except Exception:
                    self.clients.discard(ws)

    async def _heartbeat(self, every: float):
        while True:
            await asyncio.sleep(every)
            self.queue.put_nowait(json.dumps({
                "type": "heartbeat", "v": PROTOCOL_VERSION, "ts": self.last_ts,
                "flow_price": self.last_price, "ticks": self.ticks,
                "feed_alive": not self.done.is_set()}))

    async def serve(self, ticks, speed: float, exit_when_done: bool = False):
        h = self.cfg["hub"]
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        async with serve(self._handler, h["host"], h["port"]):
            print(f"{_now()} hub listening on ws://{h['host']}:{h['port']}", flush=True)
            tasks = [asyncio.create_task(self._broadcaster()),
                     asyncio.create_task(self._heartbeat(h["heartbeat_sec"]))]
            feed = threading.Thread(target=self.run_feed, args=(ticks, speed), daemon=True)
            feed.start()
            while not (exit_when_done and self.done.is_set()):
                await asyncio.sleep(0.2)
            await asyncio.sleep(1.0)                 # let the last messages drain
            for t in tasks:
                t.cancel()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config_xauusd.toml"))
    ap.add_argument("--replay", default="", help="'synthetic' or a tick file (.csv / .dbn.zst)")
    ap.add_argument("--speed", type=float, default=60.0, help="replay speed x real time (0 = flat out)")
    ap.add_argument("--days", type=int, default=3, help="synthetic replay length")
    a = ap.parse_args()
    cfg = tomllib.load(open(a.config, "rb"))

    if not a.replay:
        ticks = ds.databento_live(cfg["instrument"]["data_symbol"])
        speed = 0
        print("LIVE: GC order flow from Databento")
    elif a.replay == "synthetic":
        ticks = ds.synthetic_tape(days=a.days, tick=cfg["instrument"]["tick_size"])
        speed = a.speed
    elif a.replay.endswith((".dbn", ".dbn.zst")):
        ticks, speed = ds.load_databento(a.replay), a.speed
    else:
        ticks, speed = ds.load_csv(a.replay), a.speed

    try:
        asyncio.run(Hub(cfg).serve(ticks, speed, exit_when_done=bool(a.replay)))
    except KeyboardInterrupt:
        print("hub stopped")


if __name__ == "__main__":
    main()
