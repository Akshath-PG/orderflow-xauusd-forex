"""End-to-end: hub replays a GC tape and broadcasts over a real local WebSocket;
the MT5 executor (against fake_mt5) receives signals and trades XAUUSD.
Run: python tests/test_xauusd_bridge.py"""
import asyncio
import copy
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "xauusd")); sys.path.insert(0, str(ROOT / "tests"))

import fake_mt5                                   # noqa: E402
from hub import Hub                               # noqa: E402
from mt5_executor import Mt5Broker, run           # noqa: E402
from orderflow import data_sources as ds          # noqa: E402


async def main(days=3, speed=20000.0):
    cfg = tomllib.load(open(ROOT / "config_xauusd.toml", "rb"))
    cfg["hub"]["port"] = 8799
    cfg["hub"]["heartbeat_sec"] = 1
    cfg["mt5"]["poll_seconds"] = 0.005
    hub = Hub(cfg)
    clock = {"ts": 0}

    def on_tick(t):
        clock["ts"] = t.ts
        fake_mt5.set_flow_price(t.price)
    hub.tick_listeners.append(on_tick)

    broker = Mt5Broker(fake_mt5, cfg["mt5"])
    logs = []
    stop = asyncio.Event()
    ticks = ds.synthetic_tape(days=days, seed=1, tick=cfg["instrument"]["tick_size"])

    hub_task = asyncio.create_task(hub.serve(ticks, speed, exit_when_done=True))
    await asyncio.sleep(0.3)
    ex_task = asyncio.create_task(run(cfg, broker, lambda: clock["ts"], log=logs.append, stop_event=stop))
    await hub_task
    stop.set()
    core = await ex_task
    return hub, core, logs


if __name__ == "__main__":
    hub, core, logs = asyncio.run(main())
    signals = sum(1 for l in logs if "OPEN #" in l) + sum(1 for l in logs if "skipped" in l)
    opens = [l for l in logs if "OPEN #" in l]
    closes = [l for l in logs if "CLOSED #" in l]
    for l in logs[:3] + opens[:3] + [l for l in logs if "TP1 #" in l][:2] + closes[:4]:
        print(l)
    st = fake_mt5._state
    print(f"\nhub ticks {hub.ticks} | executor saw {signals} signals | opened {len(opens)} | closed {len(closes)}")
    print(f"broker SL hits {st['sl_hits']} | broker TP hits {st['tp_hits']} | balance {st['balance']:.2f}")
    assert hub.ticks > 100000, "replay ran"
    assert len(opens) >= 3, "executor received and placed trades over the WebSocket"
    assert len(closes) >= len(opens) - 1, "trades were managed to completion"
    assert st["pos"] is None or core.trade is not None
    pnl_sum = sum(p for _, p, _ in core.closed)
    assert abs((st["balance"] - 5000.0) - pnl_sum) < 0.05 or core.trade is not None, (st["balance"], pnl_sum)
    print("END-TO-END PASS")
