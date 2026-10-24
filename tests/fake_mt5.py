"""Stand-in for the MetaTrader5 package (which only runs on Windows), with the
same function names and the behaviour the executor relies on: quotes with a
spread, broker-side SL/TP that trigger on price, netting positions, partial
closes, SL/TP modification, commission, order_calc_profit."""
from types import SimpleNamespace as NS

TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
ORDER_TIME_GTC = 0
TRADE_RETCODE_DONE = 10009

CONTRACT = 100.0          # oz per lot
SPREAD = 0.24
COMM_PER_LOT_SIDE = 3.5
BASIS = -15.0             # spot trades ~$15 under the futures contract

_state = {"bid": 0.0, "ask": 0.0, "balance": 5000.0, "pos": None, "ticket": 0,
          "deals": [], "sl_hits": 0, "tp_hits": 0}


def set_flow_price(gc_price: float):
    """Drive the fake XAUUSD quote from the GC price, and fire broker SL/TP."""
    mid = gc_price + BASIS
    _state["bid"], _state["ask"] = round(mid - SPREAD / 2, 2), round(mid + SPREAD / 2, 2)
    p = _state["pos"]
    if not p:
        return
    px = _state["bid"] if p.type == POSITION_TYPE_BUY else _state["ask"]
    s = 1 if p.type == POSITION_TYPE_BUY else -1
    if p.sl and s * (px - p.sl) <= 0:
        _state["sl_hits"] += 1
        _close(p.volume, px)
    elif p.tp and s * (px - p.tp) >= 0:
        _state["tp_hits"] += 1
        _close(p.volume, p.tp)


def _close(volume, price):
    p = _state["pos"]
    s = 1 if p.type == POSITION_TYPE_BUY else -1
    pnl = (price - p.price_open) * s * volume * CONTRACT - COMM_PER_LOT_SIDE * volume
    _state["balance"] += pnl
    _state["deals"].append(("close", volume, price, pnl))
    p.volume = round(p.volume - volume, 2)
    if p.volume <= 1e-9:
        _state["pos"] = None


def initialize(**kw): return True
def last_error(): return (0, "ok")
def symbol_select(sym, on): return True


def symbol_info(sym):
    return NS(volume_min=0.01, volume_step=0.01, volume_max=50.0, digits=2,
              trade_contract_size=CONTRACT, filling_mode=2)


def symbol_info_tick(sym):
    return NS(bid=_state["bid"], ask=_state["ask"])


def account_info():
    p, eq = _state["pos"], _state["balance"]
    if p:
        s = 1 if p.type == POSITION_TYPE_BUY else -1
        px = _state["bid"] if s > 0 else _state["ask"]
        eq += (px - p.price_open) * s * p.volume * CONTRACT
    return NS(balance=_state["balance"], equity=eq)


def positions_get(symbol=None):
    return (_state["pos"],) if _state["pos"] else ()


def order_calc_profit(order_type, sym, volume, price_open, price_close):
    s = 1 if order_type == ORDER_TYPE_BUY else -1
    return (price_close - price_open) * s * volume * CONTRACT


def order_send(req):
    if req["action"] == TRADE_ACTION_SLTP:
        p = _state["pos"]
        if not p:
            return NS(retcode=10013, comment="no position", price=0)
        p.sl, p.tp = req["sl"], req["tp"]
        return NS(retcode=TRADE_RETCODE_DONE, comment="modified", price=0)
    buy = req["type"] == ORDER_TYPE_BUY
    price = _state["ask"] if buy else _state["bid"]
    p = _state["pos"]
    if req.get("position"):                                   # closing (part of) a position
        _close(req["volume"], price)
        return NS(retcode=TRADE_RETCODE_DONE, comment="closed", price=price)
    if p:
        return NS(retcode=10019, comment="netting test: already in a position", price=0)
    s = 1 if buy else -1
    if (req["sl"] and s * (price - req["sl"]) <= 0) or (req["tp"] and s * (req["tp"] - price) <= 0):
        return NS(retcode=10016, comment="invalid stops", price=0)
    _state["ticket"] += 1
    _state["balance"] -= COMM_PER_LOT_SIDE * req["volume"]
    _state["pos"] = NS(ticket=_state["ticket"], type=POSITION_TYPE_BUY if buy else POSITION_TYPE_SELL,
                       volume=req["volume"], price_open=price, sl=req["sl"], tp=req["tp"],
                       magic=req["magic"])
    _state["deals"].append(("open", req["volume"], price, 0.0))
    return NS(retcode=TRADE_RETCODE_DONE, comment="done", price=price)
