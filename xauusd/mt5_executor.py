"""MT5 EXECUTOR — trades XAUUSD in MetaTrader 5 from the hub's signals.

  pip install MetaTrader5 websockets      (Windows, MT5 terminal installed and logged in)
  python xauusd/hub.py                    (in one window)
  python xauusd/mt5_executor.py           (in another)

Settings: [propfirm], [risk] (profile), [session] and [mt5] in config_xauusd.toml.
Every order carries a broker-side stop loss, so the position stays protected
even if this script, the hub, or the internet connection stops.
"""
import argparse
import asyncio
import json
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from websockets.asyncio.client import connect      # noqa: E402

from executor_core import ExecutorCore               # noqa: E402


class Mt5Broker:
    def __init__(self, mt5, c: dict):
        self.mt5, self.sym, self.magic, self.dev = mt5, c["symbol"], int(c["magic"]), int(c["deviation"])
        kw = {}
        if c.get("terminal_path"):
            kw["path"] = c["terminal_path"]
        if c.get("login"):
            kw.update(login=int(c["login"]), password=c["password"], server=c["server"])
        if not mt5.initialize(**kw):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        if not mt5.symbol_select(self.sym, True):
            raise RuntimeError(f"symbol {self.sym} not available - check the exact name in Market Watch")
        info = mt5.symbol_info(self.sym)
        self.volume_min, self.volume_step, self.volume_max = info.volume_min, info.volume_step, info.volume_max
        self.digits = info.digits
        self.contract = info.trade_contract_size
        fm = info.filling_mode                       # bit 1 = FOK allowed, bit 2 = IOC allowed
        self.filling = (mt5.ORDER_FILLING_FOK if fm & 1 else
                        mt5.ORDER_FILLING_IOC if fm & 2 else mt5.ORDER_FILLING_RETURN)

    def quote(self):
        t = self.mt5.symbol_info_tick(self.sym)
        return t.bid, t.ask

    def account(self):
        a = self.mt5.account_info()
        return a.balance, a.equity

    def _mine(self):
        ps = self.mt5.positions_get(symbol=self.sym) or ()
        return [p for p in ps if p.magic == self.magic]

    def position(self):
        mine = self._mine()
        if not mine:
            return None
        p = mine[0]
        return {"side": 1 if p.type == self.mt5.POSITION_TYPE_BUY else -1, "volume": p.volume,
                "price_open": p.price_open, "sl": p.sl, "tp": p.tp, "ticket": p.ticket}

    def _send(self, req):
        r = self.mt5.order_send(req)
        if r is None:
            return False, str(self.mt5.last_error()), None
        return r.retcode == self.mt5.TRADE_RETCODE_DONE, f"{r.retcode} {r.comment}", getattr(r, "price", None)

    def open(self, side, volume, sl, tp, comment):
        bid, ask = self.quote()
        m = self.mt5
        return self._send({
            "action": m.TRADE_ACTION_DEAL, "symbol": self.sym, "volume": float(volume),
            "type": m.ORDER_TYPE_BUY if side > 0 else m.ORDER_TYPE_SELL,
            "price": ask if side > 0 else bid,
            "sl": round(sl, self.digits), "tp": round(tp, self.digits),
            "deviation": self.dev, "magic": self.magic, "comment": comment[:31],
            "type_time": m.ORDER_TIME_GTC, "type_filling": self.filling})

    def close(self, volume=None):
        p = self.position()
        if not p:
            return True, "flat"
        bid, ask = self.quote()
        m = self.mt5
        ok, info, _ = self._send({
            "action": m.TRADE_ACTION_DEAL, "symbol": self.sym, "position": p["ticket"],
            "volume": float(volume or p["volume"]),
            "type": m.ORDER_TYPE_SELL if p["side"] > 0 else m.ORDER_TYPE_BUY,
            "price": bid if p["side"] > 0 else ask,
            "deviation": self.dev, "magic": self.magic, "comment": "OF close",
            "type_time": m.ORDER_TIME_GTC, "type_filling": self.filling})
        return ok, info

    def modify(self, sl, tp):
        p = self.position()
        if not p:
            return False, "flat"
        ok, info, _ = self._send({"action": self.mt5.TRADE_ACTION_SLTP, "symbol": self.sym,
                                  "position": p["ticket"], "sl": round(sl, self.digits),
                                  "tp": round(tp, self.digits)})
        return ok, info

    def loss_per_lot(self, side, price, stop_price):
        m = self.mt5
        v = m.order_calc_profit(m.ORDER_TYPE_BUY if side > 0 else m.ORDER_TYPE_SELL,
                                self.sym, 1.0, price, stop_price)
        return abs(v) if v is not None else abs(price - stop_price) * self.contract


def _stamp():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


async def run(cfg, broker, now_ns, log=print, stop_event=None):
    h = cfg["hub"]
    uri = f"ws://{'127.0.0.1' if h['host'] == '0.0.0.0' else h['host']}:{h['port']}"
    outbox = asyncio.Queue()
    core = ExecutorCore(cfg, broker, now_ns, name="mt5", log=log, send=outbox.put_nowait)
    log(f"[mt5] profile {core.profile} | account {cfg['propfirm']['account_size']} | "
        f"floor {core.risk.floor:.2f} | daily allowance {core.risk.daily_allow:.0f}")
    last_msg = [time.monotonic()]
    poll = float(cfg["mt5"]["poll_seconds"])

    async def poller():
        warned = False
        while not (stop_event and stop_event.is_set()):
            try:
                core.poll()
            except Exception as exc:
                log(f"[mt5] poll error: {exc!r}")
            silent = time.monotonic() - last_msg[0]
            if silent > 4 * h["heartbeat_sec"] and not warned:
                log("[mt5] WARNING: no messages from the hub - open positions keep their broker stop loss")
                warned = True
            if silent < 4 * h["heartbeat_sec"]:
                warned = False
            await asyncio.sleep(poll)

    async def link():
        while not (stop_event and stop_event.is_set()):
            try:
                async with connect(uri) as ws:
                    log(f"[mt5] connected to hub {uri}")

                    async def sender():
                        while True:
                            await ws.send(json.dumps(await outbox.get()))
                    snd = asyncio.create_task(sender())
                    try:
                        async for raw in ws:
                            last_msg[0] = time.monotonic()
                            core.on_message(json.loads(raw))
                    finally:
                        snd.cancel()
            except Exception as exc:
                if stop_event and stop_event.is_set():
                    break
                log(f"[mt5] hub connection lost ({exc.__class__.__name__}) - retrying in 3s")
                await asyncio.sleep(3)

    tasks = [asyncio.create_task(poller()), asyncio.create_task(link())]
    if stop_event:
        while not stop_event.is_set():
            await asyncio.sleep(0.1)
        for t in tasks:
            t.cancel()
    else:
        await asyncio.gather(*tasks)
    return core


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config_xauusd.toml"))
    a = ap.parse_args()
    cfg = tomllib.load(open(a.config, "rb"))
    import MetaTrader5 as mt5           # Windows only
    broker = Mt5Broker(mt5, cfg["mt5"])
    stamped = lambda text: print(_stamp(), text, flush=True)
    try:
        asyncio.run(run(cfg, broker, time.time_ns, log=stamped))
    except KeyboardInterrupt:
        print("executor stopped - open positions keep their broker stop loss and target")


if __name__ == "__main__":
    main()
