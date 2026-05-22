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

# ─── Dual Execution State ─────────────────────────────────────
_demo_client = None
_demo_account_id = None
_demo_is_authenticated = False
_demo_account_balance = 0.0
_demo_account_equity = 0.0
_demo_position_map = {}  # live_position_id -> (demo_position_id, demo_volume)
_demo_symbol_cache = {}  # symbol_name -> symbol_id on demo server
_demo_current_price = {}  # symbol_name -> {"bid": float, "ask": float} from demo feed

# Price offset tracker: measures the difference between live and demo prices
# Used to adjust SL/TP when executing on live with demo-analyzed data
_price_offset = {}  # symbol_name -> {"bid_offset": float, "ask_offset": float, "avg_offset": float}
_OFFSET_ALPHA = 0.1  # Exponential moving average smoothing factor


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

# --- Dual Execution Initialization ---
def set_demo_client(client):
    """Store the demo client instance from main.py"""
    global _demo_client
    _demo_client = client

@defer.inlineCallbacks
def on_demo_connected(client):
    """Callback triggered when the demo client connects."""
    log.info("📡 Authenticating Demo Application...")
    request = ProtoOAApplicationAuthReq()
    request.clientId = config.CTRADER_CLIENT_ID
    request.clientSecret = config.CTRADER_CLIENT_SECRET
    yield client.send(request)
    
    global _demo_account_id, _demo_is_authenticated
    _demo_account_id = int(config.CTRADER_DEMO_ACCOUNT_ID)
    
    log.info(f"📡 Authenticating Demo Account {_demo_account_id}...")
    req_acc = ProtoOAAccountAuthReq()
    req_acc.ctidTraderAccountId = _demo_account_id
    req_acc.accessToken = config.CTRADER_DEMO_ACCESS_TOKEN
    resp_acc = yield client.send(req_acc)
    msg_acc = Protobuf.extract(resp_acc)
    if hasattr(msg_acc, 'errorCode') and msg_acc.errorCode:
        log.error(f"❌ Demo Auth Failed: {msg_acc.errorCode} - {getattr(msg_acc, 'description', '')}")
        defer.returnValue(None)

    _demo_is_authenticated = True
    
    # Fetch demo balance
    req_info = ProtoOATraderReq()
    req_info.ctidTraderAccountId = _demo_account_id
    resp_info = yield client.send(req_info)
    msg_info = Protobuf.extract(resp_info)
    
    if hasattr(msg_info, 'trader'):
        global _demo_account_balance
        _demo_account_balance = msg_info.trader.balance / 100.0
    else:
        log.warning(f"Demo balance response missing 'trader' attribute. Response type: {type(msg_info).__name__}")
        if hasattr(msg_info, 'errorCode'):
            log.error(f"Error fetching demo balance: {msg_info.errorCode} - {getattr(msg_info, 'description', '')}")
        attrs = [a for a in dir(msg_info) if not a.startswith('_')]
        log.debug(f"Response attrs: {attrs}")
        
    log.info(f"✅ Demo Account {_demo_account_id} Authenticated for Mirroring | Balance: ${_demo_account_balance:,.2f}")

    # Load demo symbol list so we can subscribe to ticks
    global _demo_symbol_cache
    req_sym = ProtoOASymbolsListReq()
    req_sym.ctidTraderAccountId = _demo_account_id
    resp_sym = yield client.send(req_sym)
    msg_sym = Protobuf.extract(resp_sym)
    if hasattr(msg_sym, 'symbol'):
        for sym in msg_sym.symbol:
            _demo_symbol_cache[sym.symbolName] = sym.symbolId
    log.info(f"📊 Demo: Loaded {len(_demo_symbol_cache)} symbols")

    # Subscribe to live ticks on the demo feed for all trading symbols
    for sym_name in config.TRADING_SYMBOLS:
        demo_sid = _demo_symbol_cache.get(sym_name)
        if demo_sid:
            req_sub = ProtoOASubscribeSpotsReq()
            req_sub.ctidTraderAccountId = _demo_account_id
            req_sub.symbolId.append(demo_sid)
            yield client.send(req_sub)
            log.info(f"📡 Demo: Subscribed to {sym_name} ticks (ID: {demo_sid})")
        else:
            log.warning(f"⚠️ Demo: {sym_name} not found in demo symbol list")



@defer.inlineCallbacks
def authenticate_account(client):
    """Step 2: Authenticate the Live trading account."""
    global _account_id

    _account_id = int(config.CTRADER_LIVE_ACCOUNT_ID)

    log.info(f"📡 Authenticating Live trading account {_account_id}...")

    request = ProtoOAAccountAuthReq()
    request.ctidTraderAccountId = _account_id
    request.accessToken = config.CTRADER_LIVE_ACCESS_TOKEN

    response = yield client.send(request)
    log.info(f"✅ Live Account {_account_id} authenticated")

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

        log.debug(f"🏦 Account balance: ${_account_balance:,.2f}")

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


def update_demo_price(symbol_name, bid=None, ask=None):
    """Update demo price tracking and calculate price offset vs live."""
    global _demo_current_price, _price_offset

    if bid and bid > 0:
        _demo_current_price.setdefault(symbol_name, {})["bid"] = bid
    if ask and ask > 0:
        _demo_current_price.setdefault(symbol_name, {})["ask"] = ask

    # Calculate offset between live and demo prices
    live_p = _current_price.get(symbol_name, {})
    demo_p = _demo_current_price.get(symbol_name, {})
    live_bid = live_p.get("bid", 0)
    demo_bid = demo_p.get("bid", 0)
    live_ask = live_p.get("ask", 0)
    demo_ask = demo_p.get("ask", 0)

    if live_bid > 0 and demo_bid > 0 and live_ask > 0 and demo_ask > 0:
        bid_off = live_bid - demo_bid
        ask_off = live_ask - demo_ask
        avg_off = (bid_off + ask_off) / 2

        if symbol_name not in _price_offset:
            _price_offset[symbol_name] = {"bid_offset": bid_off, "ask_offset": ask_off, "avg_offset": avg_off}
        else:
            # Exponential moving average to smooth the offset
            prev = _price_offset[symbol_name]
            _price_offset[symbol_name] = {
                "bid_offset": _OFFSET_ALPHA * bid_off + (1 - _OFFSET_ALPHA) * prev["bid_offset"],
                "ask_offset": _OFFSET_ALPHA * ask_off + (1 - _OFFSET_ALPHA) * prev["ask_offset"],
                "avg_offset": _OFFSET_ALPHA * avg_off + (1 - _OFFSET_ALPHA) * prev["avg_offset"],
            }


def get_price_offset(symbol_name):
    """Get the current smoothed price offset for a symbol."""
    return _price_offset.get(symbol_name, {"bid_offset": 0, "ask_offset": 0, "avg_offset": 0})


def get_demo_current_price(symbol_name=None):
    """Get the latest demo price for a symbol."""
    if symbol_name:
        return _demo_current_price.get(symbol_name, {})
    return _demo_current_price


@defer.inlineCallbacks
def get_demo_trendbars(symbol_name, period_minutes=15, count=50):
    """Fetch historical candle data from the DEMO server."""
    if not _demo_client or not _demo_is_authenticated:
        log.warning("Demo client not ready for trendbars fetch")
        defer.returnValue([])

    demo_sid = _demo_symbol_cache.get(symbol_name)
    if not demo_sid:
        log.error(f"Symbol {symbol_name} not found in demo cache")
        defer.returnValue([])

    period = TIMEFRAME_MAP.get(period_minutes, ProtoOATrendbarPeriod.M15)

    now_ms = int(time.time() * 1000)
    from_ms = now_ms - (count * period_minutes * 60 * 1000)

    request = ProtoOAGetTrendbarsReq()
    request.ctidTraderAccountId = _demo_account_id
    request.symbolId = demo_sid
    request.period = period
    request.fromTimestamp = from_ms
    request.toTimestamp = now_ms

    response = yield _demo_client.send(request)
    msg = Protobuf.extract(response)

    candles = []
    if hasattr(msg, "trendbar"):
        divisor = 100000.0
        for bar in msg.trendbar:
            o = (bar.low / divisor) + (bar.deltaOpen / divisor) if hasattr(bar, "deltaOpen") else bar.low / divisor
            h = (bar.low / divisor) + (bar.deltaHigh / divisor) if hasattr(bar, "deltaHigh") else bar.low / divisor
            c = (bar.low / divisor) + (bar.deltaClose / divisor) if hasattr(bar, "deltaClose") else bar.low / divisor
            low = bar.low / divisor
            vol = bar.volume if hasattr(bar, "volume") else 0

            candles.append({
                "open": round(o, 5),
                "high": round(h, 5),
                "low": round(low, 5),
                "close": round(c, 5),
                "volume": vol,
                "timestamp": bar.utcTimestampInMinutes * 60 * 1000 if hasattr(bar, "utcTimestampInMinutes") else 0,
            })

    log.debug(f"📊 Demo: Fetched {len(candles)} {period_minutes}m candles for {symbol_name}")
    defer.returnValue(candles)


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

    log.debug(f"📊 Fetched {len(candles)} candles for {symbol_name} ({period_minutes}m)")
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
    if not sl_price or not tp_price:
        error_msg = f"CRITICAL: Attempted to open {side} order on {symbol_name} without SL or TP! Trade rejected by hard fail-safe."
        log.error(error_msg)
        raise Exception(error_msg)

    symbol_id = _symbol_cache.get(symbol_name)
    if not symbol_id:
        log.error(f"Symbol {symbol_name} not found")
        defer.returnValue(None)

    request = ProtoOANewOrderReq()
    request.ctidTraderAccountId = _account_id
    request.symbolId = symbol_id
    request.orderType = ProtoOAOrderType.MARKET
    request.tradeSide = ProtoOATradeSide.BUY if side.upper() == "BUY" else ProtoOATradeSide.SELL
    request.volume = int(round(volume * 100))  # cTrader: volume in centi-units

    log.info(f"🚀 Placing {side} order: {symbol_name} | Vol: {volume} units (API volume: {request.volume})")

    response = yield client.send(request)
    msg = Protobuf.extract(response)

    # Check for errors in response
    if hasattr(msg, 'errorCode') and msg.errorCode:
        log.error(f"❌ Order REJECTED by cTrader: {msg.errorCode} — {getattr(msg, 'description', 'unknown')}")
        defer.returnValue(None)

    log.info(f"✅ Live Order placed successfully | Response: {type(msg).__name__}")
    
    # ─── Dual Execution (Demo Mirror) ───
    _temp_demo_pos_id = None
    _temp_demo_volume = 0
    if config.DUAL_EXECUTION_ENABLED and _demo_is_authenticated and _demo_client:
        try:
            # Use demo symbol cache (demo server may have different IDs)
            demo_sid = _demo_symbol_cache.get(symbol_name, symbol_id)
            
            # Determine volume step
            step_vol = 1
            if "XAU" not in symbol_name and "BTC" not in symbol_name and "ETH" not in symbol_name:
                step_vol = 1000
            
            # Scale lot size proportionally to demo account balance
            # Demo ~$108k vs Live ~$5k = ~22x multiplier
            if _demo_account_balance > 0 and _account_balance > 0:
                balance_ratio = _demo_account_balance / _account_balance
                raw_demo_vol = volume * balance_ratio
                demo_volume = int(round(raw_demo_vol / step_vol)) * step_vol
            else:
                demo_volume = int(round(volume / step_vol)) * step_vol  # Fallback: same size
            
            demo_req = ProtoOANewOrderReq()
            demo_req.ctidTraderAccountId = _demo_account_id
            demo_req.symbolId = demo_sid
            demo_req.orderType = ProtoOAOrderType.MARKET
            demo_req.tradeSide = ProtoOATradeSide.BUY if side.upper() == "BUY" else ProtoOATradeSide.SELL
            demo_req.volume = max(demo_volume, 1) * 100
            _temp_demo_volume = demo_req.volume  # Store for position map
            
            log.info(f"🚀 Mirroring {side} order to Demo | Vol: {demo_volume} lots (scaled from {int(round(volume))})")
            demo_resp = yield _demo_client.send(demo_req)
            demo_msg = Protobuf.extract(demo_resp)
            
            if hasattr(demo_msg, 'errorCode') and demo_msg.errorCode:
                log.error(f"⚠️ Demo mirror REJECTED: {demo_msg.errorCode} — {getattr(demo_msg, 'description', '')}")
            else:
                log.info(f"✅ Demo order placed successfully")
            
            # Map Live position ID to Demo position ID
            demo_pos_id = None
            if hasattr(demo_msg, 'position') and hasattr(demo_msg.position, 'positionId'):
                demo_pos_id = demo_msg.position.positionId
            elif hasattr(demo_msg, 'order') and hasattr(demo_msg.order, 'positionId'):
                demo_pos_id = demo_msg.order.positionId
            
            _temp_demo_pos_id = demo_pos_id
            
        except Exception as e:
            log.error(f"⚠️ Failed to mirror order to Demo: {e}")
            _temp_demo_pos_id = None
            _temp_demo_volume = 0

    # Extract position ID from execution event and set SL/TP
    position_id = None
    if hasattr(msg, 'position') and hasattr(msg.position, 'positionId'):
        position_id = msg.position.positionId
    elif hasattr(msg, 'order') and hasattr(msg.order, 'positionId'):
        position_id = msg.order.positionId
        
    if config.DUAL_EXECUTION_ENABLED and position_id and _temp_demo_pos_id:
        _demo_position_map[position_id] = (_temp_demo_pos_id, _temp_demo_volume)
        log.info(f"🔗 Mapped Live Pos {position_id} -> Demo Pos {_temp_demo_pos_id} (vol: {_temp_demo_volume})")

    if position_id and (sl_price or tp_price):
        # --- SLIPPAGE TP FIX ---
        exec_price = None
        if hasattr(msg, 'position') and hasattr(msg.position, 'price'):
            exec_price = msg.position.price
        elif hasattr(msg, 'order') and hasattr(msg.order, 'executionPrice'):
            exec_price = msg.order.executionPrice

        # The execution price might be divided by 100000 in cTrader depending on the protocol version.
        # But `price` is usually the real price in Open API V2. Let's assume it's correct.
        if exec_price and exec_price > 0 and sl_price and tp_price:
            # Check if price needs scaling (e.g. 15916700 instead of 159.167)
            if exec_price > 1000000:
                exec_price = exec_price / 100000.0
                
            actual_sl_dist = abs(exec_price - sl_price)
            actual_tp_dist = abs(tp_price - exec_price)
            
            # Mathematical guarantee: TP distance must be >= SL distance
            if actual_tp_dist < actual_sl_dist:
                log.warning(f"⚠️ Slippage detected! Executed at {exec_price}. SL dist ({actual_sl_dist:.5f}) > TP dist ({actual_tp_dist:.5f}). Fixing TP...")
                if side.upper() == "BUY":
                    tp_price = exec_price + actual_sl_dist
                else:
                    tp_price = exec_price - actual_sl_dist

        log.info(f"📎 Setting SL/TP on position {position_id}: SL=${sl_price:.5f} | TP=${tp_price:.5f}")
        try:
            yield amend_position_sltp(client, position_id, sl_price, tp_price)
            log.info(f"✅ SL/TP set successfully on position {position_id}")
        except Exception as e:
            log.error(f"⚠️ Order filled but SL/TP amendment failed: {e}")
            log.error(f"🚨 Failsafe: SL/TP amendment failed. Closing naked position {position_id} immediately to protect account!")
            try:
                if hasattr(msg, 'position') and hasattr(msg.position, 'volume'):
                    vol_to_close = msg.position.volume
                elif hasattr(msg, 'order') and hasattr(msg.order, 'executedVolume'):
                    vol_to_close = msg.order.executedVolume
                else:
                    vol_to_close = request.volume
                yield close_position(client, position_id, vol_to_close)
                log.info(f"🛡️ Naked trade {position_id} closed successfully via failsafe.")
            except Exception as close_e:
                log.error(f"💀 CRITICAL: Failed to close naked trade {position_id} after SL/TP amend failed: {close_e}")
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
    
    # ─── Dual Execution (Demo Mirror) ───
    if config.DUAL_EXECUTION_ENABLED and _demo_is_authenticated and _demo_client:
        demo_entry = _demo_position_map.get(position_id)
        demo_pos_id = None
        demo_vol = volume  # fallback to same volume
        if demo_entry:
            if isinstance(demo_entry, tuple):
                demo_pos_id = demo_entry[0]
                demo_vol = demo_entry[1] if demo_entry[1] > 0 else volume
            else:
                demo_pos_id = demo_entry
        if demo_pos_id:
            try:
                demo_req = ProtoOAClosePositionReq()
                demo_req.ctidTraderAccountId = _demo_account_id
                demo_req.positionId = demo_pos_id
                demo_req.volume = demo_vol
                yield _demo_client.send(demo_req)
                log.info(f"✅ Demo Position {demo_pos_id} close request sent (vol: {demo_vol})")
                # Remove from map on full close
                _demo_position_map.pop(position_id, None)
            except Exception as e:
                log.error(f"⚠️ Failed to close demo position {demo_pos_id}: {e}")
                
    defer.returnValue({"success": True, "message": msg})


@defer.inlineCallbacks
def amend_position_sl(client, position_id, new_sl_price):
    """Amend the stop loss of an open position."""
    yield amend_position_sltp(client, position_id, new_sl_price, None)


@defer.inlineCallbacks
def amend_position_sltp(client, position_id, sl_price=None, tp_price=None, symbol_name=None):
    """Amend SL and/or TP on an open position.

    Args:
        position_id: The position ID to amend.
        sl_price: New stop loss price (absolute, normal format like 4739.00).
        tp_price: New take profit price (absolute, normal format like 4705.00).
        symbol_name: Optional. Used to look up correct decimal digits for rounding.
    """
    request = ProtoOAAmendPositionSLTPReq()
    request.ctidTraderAccountId = _account_id
    request.positionId = position_id

    # Determine correct digits
    digits = 5
    if symbol_name:
        sid = _symbol_cache.get(symbol_name)
        if sid and sid in _symbol_details:
            digits = _symbol_details[sid].get("digits", 5)
    else:
        # Fallback: Guess based on price magnitude (Gold is > 1000, Forex is < 200 usually)
        price_to_check = sl_price or tp_price
        if price_to_check and price_to_check > 1000:
            digits = 2

    if sl_price and sl_price > 0:
        request.stopLoss = round(sl_price, digits)
    if tp_price and tp_price > 0:
        request.takeProfit = round(tp_price, digits)

    log.info(f"📎 Amending position {position_id}: SL=${request.stopLoss} | TP=${request.takeProfit}")

    response = yield client.send(request)
    msg = Protobuf.extract(response)

    if hasattr(msg, 'errorCode') and msg.errorCode:
        log.error(f"❌ Amend FAILED: {msg.errorCode} — {getattr(msg, 'description', 'unknown')}")
        defer.returnValue(None)

    log.info(f"✅ Position {position_id} SL/TP amended: SL=${sl_price} | TP=${tp_price}")
    
    # ─── Dual Execution (Demo Mirror) ───
    if config.DUAL_EXECUTION_ENABLED and _demo_is_authenticated and _demo_client:
        demo_entry = _demo_position_map.get(position_id)
        demo_pos_id = None
        if demo_entry:
            demo_pos_id = demo_entry[0] if isinstance(demo_entry, tuple) else demo_entry
        if demo_pos_id:
            try:
                demo_req = ProtoOAAmendPositionSLTPReq()
                demo_req.ctidTraderAccountId = _demo_account_id
                demo_req.positionId = demo_pos_id
                if sl_price and sl_price > 0:
                    demo_req.stopLoss = round(sl_price, 5)
                if tp_price and tp_price > 0:
                    demo_req.takeProfit = round(tp_price, 5)
                yield _demo_client.send(demo_req)
                log.info(f"✅ Demo Position {demo_pos_id} SL/TP amended")
            except Exception as e:
                log.error(f"⚠️ Failed to amend demo position {demo_pos_id}: {e}")
                
    defer.returnValue({"success": True, "message": msg})
