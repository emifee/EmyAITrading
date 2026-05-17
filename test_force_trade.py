"""
test_force_trade.py — Force Claude to place a trade on demo to test the pipeline.
Uses a special prompt that tells Claude to pick the best direction regardless.
"""

import os, sys, time, json
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

from twisted.internet import reactor, defer
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *

import config
import anthropic
import pandas as pd

CLIENT_ID = config.CTRADER_CLIENT_ID
CLIENT_SECRET = config.CTRADER_CLIENT_SECRET
ACCESS_TOKEN = config.CTRADER_ACCESS_TOKEN
ACCOUNT_ID = int(config.CTRADER_ACCOUNT_ID)

host = EndPoints.PROTOBUF_DEMO_HOST
client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)

# Force-trade prompt — tells Claude to pick a direction
FORCE_PROMPT = """You are a XAUUSD trading analyst. This is a DEMO ACCOUNT TEST.

INSTRUCTION: You MUST return either BUY or SELL (NOT HOLD). Pick the best direction 
based on the data below. This is a test trade on a demo account to verify the system works.

Use these rules:
- Pick BUY or SELL based on the most recent price action
- Set stop_loss 10-15 points away from entry
- Set take_profit 20-30 points away from entry (2:1 R:R)
- Use position_size_pct of 1.0
- Set entry_price to the current market price

OUTPUT FORMAT (strict JSON only):
{
  "action": "BUY" or "SELL",
  "confidence": 70,
  "entry_price": 0.00,
  "stop_loss": 0.00,
  "take_profit": 0.00,
  "position_size_pct": 1.0,
  "reason": "Test trade on demo account"
}

Return ONLY the JSON, no other text.
"""


@defer.inlineCallbacks
def run_test(c):
    print("=" * 60)
    print("🧪 FORCE TRADE TEST — Demo Account")
    print("=" * 60)

    try:
        # Authenticate
        print("\n🔑 Authenticating...")
        req = ProtoOAApplicationAuthReq()
        req.clientId = CLIENT_ID
        req.clientSecret = CLIENT_SECRET
        yield c.send(req)
        print("   ✅ App authenticated")

        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.accessToken = ACCESS_TOKEN
        yield c.send(req)
        print(f"   ✅ Account {ACCOUNT_ID} authenticated")

        # Get balance
        req = ProtoOATraderReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        msg = yield c.send(req)
        trader_res = Protobuf.extract(msg)
        balance = trader_res.trader.balance / 100.0 if hasattr(trader_res, 'trader') else 100000
        print(f"   💰 Balance: ${balance:,.2f}")

        # Load symbols
        req = ProtoOAAssetListReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        msg = yield c.send(req)
        assets = {a.assetId: a.name for a in Protobuf.extract(msg).asset}

        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        msg = yield c.send(req)
        symbols = Protobuf.extract(msg)

        symbol_id = None
        for s in symbols.symbol:
            name = assets.get(s.baseAssetId, "") + assets.get(s.quoteAssetId, "")
            if name == "XAUUSD":
                symbol_id = s.symbolId
                break

        print(f"   📊 XAUUSD ID: {symbol_id}")

        # Fetch candles
        print("\n📈 Fetching live candles...")
        now_ms = int(time.time() * 1000)
        from_ms = now_ms - (24 * 60 * 60 * 1000)

        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.symbolId = symbol_id
        req.period = ProtoOATrendbarPeriod.M15
        req.fromTimestamp = from_ms
        req.toTimestamp = now_ms
        msg = yield c.send(req)
        bars = Protobuf.extract(msg)

        divisor = 100000.0
        last_bar = bars.trendbar[-1]
        low = last_bar.low / divisor
        close_price = low + (last_bar.deltaClose / divisor)
        print(f"   Current price: ~${close_price:,.2f}")

        # Show last 3 candles
        for bar in bars.trendbar[-3:]:
            b_low = bar.low / divisor
            b_high = b_low + bar.deltaHigh / divisor
            b_open = b_low + bar.deltaOpen / divisor
            b_close = b_low + bar.deltaClose / divisor
            ts = pd.Timestamp.utcfromtimestamp(bar.utcTimestampInMinutes * 60)
            print(f"   {ts} | O: ${b_open:,.2f} H: ${b_high:,.2f} L: ${b_low:,.2f} C: ${b_close:,.2f}")

        # Ask Claude
        print(f"\n🧠 Asking Claude to pick a direction...")

        market_data = f"""
XAUUSD DEMO TEST — Current Price: ${close_price:,.2f}
Balance: ${balance:,.2f}
Last 3 candles show price range ${low:,.2f} - ${low + last_bar.deltaHigh / divisor:,.2f}

Pick BUY or SELL and set appropriate SL/TP around ${close_price:,.2f}.
"""

        ai_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = ai_client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=300,
            system=FORCE_PROMPT,
            messages=[{"role": "user", "content": market_data}],
        )

        raw = response.content[0].text.strip()
        print(f"   ✅ Response: {raw}")

        # Parse
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        if not cleaned.startswith("{"):
            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            cleaned = cleaned[start:end]

        decision = json.loads(cleaned)

        action = decision["action"]
        entry = decision["entry_price"]
        sl = decision["stop_loss"]
        tp = decision["take_profit"]

        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0

        print(f"\n{'🟢' if action == 'BUY' else '🔴'} Claude says: {action}")
        print(f"   Entry:  ${entry:,.2f}")
        print(f"   SL:     ${sl:,.2f} (${risk:,.2f} risk)")
        print(f"   TP:     ${tp:,.2f} (${reward:,.2f} reward)")
        print(f"   R:R:    {rr:.1f}:1")
        print(f"   Reason: {decision.get('reason', 'N/A')}")

        # EXECUTE THE TRADE
        print(f"\n🚀 EXECUTING {action} on DEMO...")
        order_req = ProtoOANewOrderReq()
        order_req.ctidTraderAccountId = ACCOUNT_ID
        order_req.symbolId = symbol_id
        order_req.orderType = ProtoOAOrderType.MARKET
        order_req.tradeSide = ProtoOATradeSide.BUY if action == "BUY" else ProtoOATradeSide.SELL
        order_req.volume = 100  # 0.01 lot (minimum, safe for test)

        # For market orders, SL/TP must be relative (distance in pips * 100000)
        if sl > 0:
            sl_distance = abs(entry - sl)
            order_req.relativeStopLoss = round(sl_distance * 100000)
        if tp > 0:
            tp_distance = abs(tp - entry)
            order_req.relativeTakeProfit = round(tp_distance * 100000)

        print(f"   Volume: 0.01 lot | SL: {round(sl * 100000)} | TP: {round(tp * 100000)}")

        msg = yield c.send(order_req)
        result = Protobuf.extract(msg)
        result_type = type(result).__name__

        print(f"   Response type: {result_type}")

        if hasattr(result, "position"):
            pos = result.position
            fill_price = pos.price / 100000.0
            print(f"\n   ✅✅✅ TRADE FILLED! ✅✅✅")
            print(f"   Position ID:  {pos.positionId}")
            print(f"   Fill Price:   ${fill_price:,.2f}")
            print(f"   Volume:       {pos.tradeData.volume / 100.0} lots")
            print(f"   Side:         {'BUY' if pos.tradeData.tradeSide == 1 else 'SELL'}")
        elif hasattr(result, "order"):
            order = result.order
            print(f"\n   ✅ ORDER PLACED!")
            print(f"   Order ID: {order.orderId}")
        elif hasattr(result, "executionEvent"):
            event = result.executionEvent
            print(f"\n   ✅ Execution event received!")
        elif hasattr(result, "errorCode"):
            print(f"\n   ❌ Error: {result.errorCode}")
            if hasattr(result, "description"):
                print(f"   {result.description}")
        else:
            # Print all available fields
            print(f"\n   📋 Response details:")
            for field in result.DESCRIPTOR.fields:
                val = getattr(result, field.name, None)
                if val:
                    print(f"   {field.name}: {val}")

        # Check open positions now
        print(f"\n📍 Checking open positions...")
        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        msg = yield c.send(req)
        reconcile = Protobuf.extract(msg)

        if hasattr(reconcile, "position") and reconcile.position:
            for pos in reconcile.position:
                side = "BUY" if pos.tradeData.tradeSide == 1 else "SELL"
                entry_p = pos.price / 100000.0
                vol = pos.tradeData.volume / 100.0
                sl_p = pos.stopLoss / 100000.0 if hasattr(pos, "stopLoss") and pos.stopLoss else 0
                tp_p = pos.takeProfit / 100000.0 if hasattr(pos, "takeProfit") and pos.takeProfit else 0
                print(f"   📍 {side} {vol} lots @ ${entry_p:,.2f} | SL: ${sl_p:,.2f} | TP: ${tp_p:,.2f}")
        else:
            print("   No open positions found")

        print(f"\n" + "=" * 60)
        print(f"🎉 TEST TRADE COMPLETE!")
        print(f"=" * 60)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

    reactor.stop()


client.setConnectedCallback(run_test)
client.setDisconnectedCallback(lambda c, r: None)
client.setMessageReceivedCallback(lambda c, m: None)
client.startService()
reactor.run()
