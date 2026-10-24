from dataclasses import dataclass

BUY = 1
SELL = -1
NS = 1_000_000_000


@dataclass(slots=True)
class Tick:
    """One executed trade from the tape (Time & Sales).

    side is the AGGRESSOR: +1 = buyer lifted the offer, -1 = seller hit the
    bid, 0 = unknown. Aggressor side is what makes delta possible — it is the
    single most important field in order-flow data.
    """
    ts: int        # exchange timestamp, nanoseconds since epoch (UTC)
    price: float
    size: int      # contracts
    side: int


def hour_utc(ts: int) -> int:
    return (ts // NS // 3600) % 24
