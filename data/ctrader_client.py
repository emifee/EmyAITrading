"""
ctrader_client.py — cTrader Open API connection and account operations.

Handles TCP connection, OAuth authentication, and account info retrieval
using the ctrader-open-api Python SDK (Twisted-based).
"""

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAApplicationAuthRes,
    ProtoOAAccountAuthReq,
    ProtoOAAccountAuthRes,
    ProtoOAGetTrendbarsReq,
    ProtoOAGetTrendbarsRes,
    ProtoOANewOrderReq,
    ProtoOAReconcileReq,
    ProtoOAReconcileRes,
    ProtoOATraderReq,
    ProtoOATraderRes,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
    ProtoOASymbolByIdReq,
    ProtoOASymbolByIdRes,
    ProtoOAClosePositionReq,
    ProtoOAAmendPositionSLTPReq,
    ProtoOAAssetListReq,
    ProtoOAAssetListRes,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetAccountListByAccessTokenRes,
    ProtoOACashFlowHistoryListReq,
    ProtoOACashFlowHistoryListRes,
    ProtoOASubscribeSpotsReq,
    ProtoOASubscribeSpotsRes,
    ProtoOASpotEvent,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOATrendbarPeriod,
    ProtoOAOrderType,
    ProtoOATradeSide,
)

from twisted.internet import reactor, defer
from utils.logger import log
import config
import time


# ─── Connection State ─────────────────────────────────────────
_client = None
_is_authenticated = False
_account_id = None
_symbol_cache = {}  # symbol_name -> symbol_id mapping
_symbol_details = {}  # symbol_id -> details (digits, pipPosition, etc.)
_current_price = {}  # symbol_name -> {"bid": float, "ask": float}
_account_balance = 0.0
_account_equity = 0.0


# ─── Timeframe Mapping ───────────────────────────────────────
TIMEFRAME_MAP = {
    1: ProtoOATrendbarPeriod.M1,
    5: ProtoOATrendbarPeriod.M5,
    15: ProtoOATrendbarPeriod.M15,
    30: ProtoOATrendbarPeriod.M30,
    60: ProtoOATrendbarPeriod.H1,
    240: ProtoOATrendbarPeriod.H4,
    1440: ProtoOATrendbarPeriod.D1,
}


def get_host():
    """Get the connection host based on config."""
    if config.CTRADER_HOST.lower() == "live":
        return EndPoints.PROTOBUF_LIVE_HOST
    return EndPoints.PROTOBUF_DEMO_HOST


def get_port():
    """Get the connection port."""
    return EndPoints.PROTOBUF_PORT


def create_client():
    """Create a new cTrader Open API client."""
    global _client
    host = get_host()
    _client = Client(host, get_port(), TcpProtocol)
    return _client


@defer.inlineCallbacks
def authenticate_app(client):
    """Step 1: Authenticate the application."""
    global _is_authenticated

    log.info("📡 Authenticating cTrader application...")

    request = ProtoOAApplicationAuthReq()
    request.clientId = config.CTRADER_CLIENT_ID
    request.clientSecret = config.CTRADER_CLIENT_SECRET

    response = yield client.send(request)
    log.info("✅ Application authenticated")

    _is_authenticated = True
    defer.returnValue(response)


@defer.inlineCallbacks
def authenticate_account(client):
    """Step 2: Authenticate the trading account."""
    global _account_id

    _account_id = int(config.CTRADER_ACCOUNT_ID)

    log.info(f"📡 Authenticating trading account {_account_id}...")

    request = ProtoOAAccountAuthReq()
    request.ctidTraderAccountId = _account_id
    request.accessToken = config.CTRADER_ACCESS_TOKEN

    response = yield client.send(request)
    log.info(f"✅ Account {_account_id} authenticated")

    defer.returnValue(response)


@defer.inlineCallbacks
def get_account_info(client):
    """Get trader account info (balance, equity)."""
    global _account_balance, _account_equity

    request = ProtoOATraderReq()
    request.ctidTraderAccountId = _account_id

    response = yield client.send(request)
    msg = Protobuf.extract(response)

    if hasattr(msg, "trader"):
        # Balance is in cents (multiply by 0.01)
        _account_balance = msg.trader.balance / 100.0
        _account_equity = _account_balance  # Will be updated with position P&L

        log.info(f"🏦 Account balance: ${_account_balance:,.2f}")

    defer.returnValue({
        "balance": _account_balance,
        "equity": _account_equity,
        "positions": [],
    })


@defer.inlineCallbacks
def get_symbol_list(client):
    """Get all available symbols and cache them."""
    global _symbol_cache

    request = ProtoOASymbolsListReq()
    request.ctidTraderAccountId = _account_id

    response = yield client.send(request)
    msg = Protobuf.extract(response)

    if hasattr(msg, "symbol"):
        for sym in msg.symbol:
            _symbol_cache[sym.symbolName] = sym.symbolId
            log.debug(f"Symbol: {sym.symbolName} -> ID {sym.symbolId}")

    log.info(f"📊 Loaded {len(_symbol_cache)} symbols")

    # Find and log our target symbol
    target = config.TRADING_SYMBOL
    if target in _symbol_cache:
        log.info(f"✅ {target} found (ID: {_symbol_cache[target]})")
    else:
        log.warning(f"⚠️ {target} not found! Available gold symbols:")
        for name, sid in _symbol_cache.items():
            if "XAU" in name or "GOLD" in name.upper():
                log.info(f"   {name} (ID: {sid})")

    defer.returnValue(_symbol_cache)


@defer.inlineCallbacks
def get_symbol_details(client, symbol_id):
    """Get detailed info for a symbol (digits, pip position, etc.)."""
    global _symbol_details

    request = ProtoOASymbolByIdReq()
    request.ctidTraderAccountId = _account_id
    request.symbolId.append(symbol_id)

    response = yield client.send(request)
    msg = Protobuf.extract(response)

    if hasattr(msg, "symbol") and msg.symbol:
        details = msg.symbol[0]
        _symbol_details[symbol_id] = {
            "digits": details.digits,
            "pipPosition": details.pipPosition if hasattr(details, "pipPosition") else details.digits - 1,
            "lotSize": details.lotSize if hasattr(details, "lotSize") else 100000,
            "minVolume": details.minVolume if hasattr(details, "minVolume") else 1000,
            "maxVolume": details.maxVolume if hasattr(details, "maxVolume") else 10000000,
            "stepVolume": details.stepVolume if hasattr(details, "stepVolume") else 1000,
        }
        log.debug(f"Symbol details: {_symbol_details[symbol_id]}")

    defer.returnValue(_symbol_details.get(symbol_id, {}))


@defer.inlineCallbacks
def subscribe_to_prices(client, symbol_name):
    """Subscribe to live price updates for a symbol."""

    symbol_id = _symbol_cache.get(symbol_name)
    if not symbol_id:
        log.warning(f"Cannot subscribe to {symbol_name} — symbol not found")
        return

    request = ProtoOASubscribeSpotsReq()
    request.ctidTraderAccountId = _account_id
    request.symbolId.append(symbol_id)

    response = yield client.send(request)
    log.info(f"📡 Subscribed to live prices for {symbol_name}")

    defer.returnValue(response)


def handle_spot_event(message):
    """Handle incoming price tick events."""
    global _current_price

    msg = Protobuf.extract(message)
    if hasattr(msg, "symbolId"):
        # Find symbol name from ID
        symbol_name = None
        for name, sid in _symbol_cache.items():
            if sid == msg.symbolId:
                symbol_name = name
                break

        if symbol_name:
            if hasattr(msg, "bid") and msg.bid:
                bid = msg.bid / 100000.0
                _current_price.setdefault(symbol_name, {})["bid"] = bid
            if hasattr(msg, "ask") and msg.ask:
                ask = msg.ask / 100000.0
                _current_price.setdefault(symbol_name, {})["ask"] = ask


@defer.inlineCallbacks
def get_trendbars(client, symbol_name, period_minutes=15, count=50):
    """
    Fetch historical candle data (trendbars).

    Args:
        client: cTrader client
        symbol_name: e.g. "XAUUSD"
        period_minutes: Timeframe in minutes
        count: Number of candles

    Returns:
        list of dicts with OHLCV data
    """
    symbol_id = _symbol_cache.get(symbol_name)
    if not symbol_id:
        log.error(f"Symbol {symbol_name} not found in cache")
        defer.returnValue([])

    period = TIMEFRAME_MAP.get(period_minutes, ProtoOATrendbarPeriod.M15)

    # Calculate time range
    now_ms = int(time.time() * 1000)
    # Each candle = period_minutes * 60 * 1000 ms
    from_ms = now_ms - (count * period_minutes * 60 * 1000)

    request = ProtoOAGetTrendbarsReq()
    request.ctidTraderAccountId = _account_id
    request.symbolId = symbol_id
    request.period = period
    request.fromTimestamp = from_ms
    request.toTimestamp = now_ms

    response = yield client.send(request)
    msg = Protobuf.extract(response)

    candles = []
    if hasattr(msg, "trendbar"):
        # cTrader always transmits prices multiplied by 100,000
        divisor = 100000.0
        details = _symbol_details.get(symbol_id, {})
        digits = details.get("digits", 2)

        for bar in msg.trendbar:
            # Low is the base price, others are deltas from low
            low = bar.low / divisor
            high = low + (bar.deltaHigh / divisor) if hasattr(bar, "deltaHigh") else low
            open_price = low + (bar.deltaOpen / divisor) if hasattr(bar, "deltaOpen") else low
            close = low + (bar.deltaClose / divisor) if hasattr(bar, "deltaClose") else low

            candles.append({
                "timestamp": bar.utcTimestampInMinutes * 60 * 1000,  # Back to ms
                "open": round(open_price, digits),
                "high": round(high, digits),
                "low": round(low, digits),
                "close": round(close, digits),
                "volume": bar.volume / 100.0 if hasattr(bar, "volume") else 0,
            })

    log.info(f"📊 Fetched {len(candles)} candles for {symbol_name} ({period_minutes}m)")
    defer.returnValue(candles)


@defer.inlineCallbacks
def get_open_positions(client):
    """Get all open positions."""
    request = ProtoOAReconcileReq()
    request.ctidTraderAccountId = _account_id

    response = yield client.send(request)
    msg = Protobuf.extract(response)

    positions = []
    if hasattr(msg, "position"):
        for pos in msg.position:
            symbol_name = None
            for name, sid in _symbol_cache.items():
                if sid == pos.tradeData.symbolId:
                    symbol_name = name
                    break

            # Position prices are already in decimal format (not scaled)
            entry_price = pos.price if hasattr(pos, "price") else 0
            sl_price = pos.stopLoss if hasattr(pos, "stopLoss") and pos.stopLoss else 0
            tp_price = pos.takeProfit if hasattr(pos, "takeProfit") and pos.takeProfit else 0
            side = "BUY" if pos.tradeData.tradeSide == ProtoOATradeSide.BUY else "SELL"
            volume = pos.tradeData.volume / 100.0

            # Calculate unrealized P&L using live price
            current = _current_price.get(symbol_name, {})
            current_bid = current.get("bid", 0)
            current_ask = current.get("ask", 0)
            unrealized_pnl = 0
            if entry_price > 0 and current_bid > 0:
                if side == "BUY":
                    unrealized_pnl = round((current_bid - entry_price) * volume, 2)
                else:
                    unrealized_pnl = round((entry_price - current_ask) * volume, 2)

            positions.append({
                "positionId": pos.positionId,
                "symbol": symbol_name or str(pos.tradeData.symbolId),
                "side": side,
                "volume": volume,
                "entryPrice": entry_price,
                "stopLoss": sl_price,
                "takeProfit": tp_price,
                "swap": pos.swap / 100.0 if hasattr(pos, "swap") else 0,
                "unrealizedPnl": unrealized_pnl,
                "currentPrice": current_bid if side == "BUY" else current_ask,
            })

    defer.returnValue(positions)


@defer.inlineCallbacks
def place_market_order(client, symbol_name, side, volume, sl_price=None, tp_price=None):
    """
    Place a market order.

    Args:
        client: cTrader client
        symbol_name: e.g. "XAUUSD"
        side: "BUY" or "SELL"
        volume: Volume in units (will be converted to cTrader volume * 100)
        sl_price: Stop loss price (optional)
        tp_price: Take profit price (optional)

    Returns:
        dict with order result
    """
    symbol_id = _symbol_cache.get(symbol_name)
    if not symbol_id:
        log.error(f"Symbol {symbol_name} not found")
        defer.returnValue(None)

    request = ProtoOANewOrderReq()
    request.ctidTraderAccountId = _account_id
    request.symbolId = symbol_id
    request.orderType = ProtoOAOrderType.MARKET
    request.tradeSide = ProtoOATradeSide.BUY if side.upper() == "BUY" else ProtoOATradeSide.SELL
    request.volume = int(round(volume)) * 100  # cTrader: round to whole lots, then to cents

    log.info(f"🚀 Placing {side} order: {symbol_name} | Vol: {int(round(volume))} lots ({int(round(volume)) * 100} units)")

    response = yield client.send(request)
    msg = Protobuf.extract(response)

    # Check for errors in response
    if hasattr(msg, 'errorCode') and msg.errorCode:
        log.error(f"❌ Order REJECTED by cTrader: {msg.errorCode} — {getattr(msg, 'description', 'unknown')}")
        defer.returnValue(None)

    log.info(f"✅ Order placed successfully | Response: {type(msg).__name__}")

    # Extract position ID from execution event and set SL/TP
    position_id = None
    if hasattr(msg, 'position') and hasattr(msg.position, 'positionId'):
        position_id = msg.position.positionId
    elif hasattr(msg, 'order') and hasattr(msg.order, 'positionId'):
        position_id = msg.order.positionId

    if position_id and (sl_price or tp_price):
        log.info(f"📎 Setting SL/TP on position {position_id}: SL=${sl_price} | TP=${tp_price}")
        try:
            yield amend_position_sltp(client, position_id, sl_price, tp_price)
            log.info(f"✅ SL/TP set successfully on position {position_id}")
        except Exception as e:
            log.error(f"⚠️ Order filled but SL/TP amendment failed: {e}")
    elif not position_id:
        log.warning(f"⚠️ Could not extract position ID from response to set SL/TP")
        # Log response attributes for debugging
        attrs = [a for a in dir(msg) if not a.startswith('_')]
        log.debug(f"Response attributes: {attrs}")

    defer.returnValue({"success": True, "message": msg, "positionId": position_id})


@defer.inlineCallbacks
def close_position(client, position_id, volume):
    """Close an open position (fully or partially).

    Args:
        position_id: The position ID to close.
        volume: Volume in cTrader units (e.g. 100 = 0.01 lot).
    """
    request = ProtoOAClosePositionReq()
    request.ctidTraderAccountId = _account_id
    request.positionId = position_id
    request.volume = volume

    response = yield client.send(request)
    msg = Protobuf.extract(response)

    log.info(f"✅ Position {position_id} close request sent")
    defer.returnValue({"success": True, "message": msg})


@defer.inlineCallbacks
def amend_position_sl(client, position_id, new_sl_price):
    """Amend the stop loss of an open position."""
    yield amend_position_sltp(client, position_id, new_sl_price, None)


@defer.inlineCallbacks
def amend_position_sltp(client, position_id, sl_price=None, tp_price=None):
    """Amend SL and/or TP on an open position.

    Args:
        position_id: The position ID to amend.
        sl_price: New stop loss price (absolute, normal format like 4739.00).
        tp_price: New take profit price (absolute, normal format like 4705.00).
    """
    request = ProtoOAAmendPositionSLTPReq()
    request.ctidTraderAccountId = _account_id
    request.positionId = position_id

    if sl_price and sl_price > 0:
        request.stopLoss = round(sl_price, 2)
    if tp_price and tp_price > 0:
        request.takeProfit = round(tp_price, 2)

    log.info(f"📎 Amending position {position_id}: SL=${sl_price} | TP=${tp_price}")

    response = yield client.send(request)
    msg = Protobuf.extract(response)

    if hasattr(msg, 'errorCode') and msg.errorCode:
        log.error(f"❌ Amend FAILED: {msg.errorCode} — {getattr(msg, 'description', 'unknown')}")
        defer.returnValue(None)

    log.info(f"✅ Position {position_id} SL/TP amended: SL=${sl_price} | TP=${tp_price}")
    defer.returnValue({"success": True, "message": msg})
