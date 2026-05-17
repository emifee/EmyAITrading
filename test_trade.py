"""
test_trade.py — Force a test Claude analysis + trade execution.

Connects to cTrader, fetches XAUUSD data, sends to Claude for
Trend + Liquidity Sweep analysis, and executes the trade on demo.
"""

import os
import sys
from datetime import datetime, timezone

# Ensure we can import our modules
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from twisted.internet import reactor, defer
from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *

import config
import anthropic
import json
import pandas as pd

# ─── Credentials ──────────────────────────────────────────────
CLIENT_ID = config.CTRADER_CLIENT_ID
CLIENT_SECRET = config.CTRADER_CLIENT_SECRET
ACCESS_TOKEN = config.CTRADER_ACCESS_TOKEN
ACCOUNT_ID = int(config.CTRADER_ACCOUNT_ID)

host = EndPoints.PROTOBUF_DEMO_HOST
client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)


@defer.inlineCallbacks
def run_test(c):
    """Full test: connect → fetch → analyze → trade."""
    print("=" * 60)
    print("🧪 FORCED TEST TRADE — Claude Trend + Liquidity Sweep")
    print("=" * 60)

    try:
        # ─── Step 1: Authenticate ────────────────────────────
        print("\n🔑 Step 1: Authenticating...")
        req = ProtoOAApplicationAuthReq()
        req.clientId = CLIENT_ID
        req.clientSecret = CLIENT_SECRET
        msg = yield c.send(req)
        print("   ✅ Application authenticated!")

        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.accessToken = ACCESS_TOKEN
        msg = yield c.send(req)
        print(f"   ✅ Account {ACCOUNT_ID} authenticated!")

        # ─── Step 2: Get balance ─────────────────────────────
        print("\n💰 Step 2: Getting account info...")
        req = ProtoOATraderReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        msg = yield c.send(req)
        trader_res = Protobuf.extract(msg)
        # Balance might be in trader_res.trader.balance or trader_res.ctidTraderAccountId
        if hasattr(trader_res, 'trader'):
            balance = trader_res.trader.balance / 100.0
        elif hasattr(trader_res, 'balance'):
            balance = trader_res.balance / 100.0
        else:
            # Try to find balance field
            print(f"   [DEBUG] Response type: {type(trader_res).__name__}")
            print(f"   [DEBUG] Fields: {[f.name for f in trader_res.DESCRIPTOR.fields]}")
            balance = 100000  # fallback for test
        print(f"   ✅ Balance: ${balance:,.2f}")

        # ─── Step 3: Load symbols & find XAUUSD ──────────────
        print("\n📊 Step 3: Loading symbols...")
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
            name1 = assets.get(s.baseAssetId, "")
            name2 = assets.get(s.quoteAssetId, "")
            full_name = name1 + name2
            if full_name == "XAUUSD":
                symbol_id = s.symbolId
                break

        if not symbol_id:
            # Try by symbolName
            for s in symbols.symbol:
                if hasattr(s, "symbolName") and s.symbolName == "XAUUSD":
                    symbol_id = s.symbolId
                    break

        print(f"   ✅ XAUUSD found (ID: {symbol_id})")

        # ─── Step 4: Get symbol details ──────────────────────
        req = ProtoOASymbolByIdReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.symbolId.append(symbol_id)
        msg = yield c.send(req)
        details = Protobuf.extract(msg)
        digits = 2
        for s in details.symbol:
            digits = s.digits
            print(f"   ✅ Digits: {digits}, Lot size: {s.lotSize / 100.0}")

        # ─── Step 5: Fetch candles (15m) ─────────────────────
        print("\n📈 Step 5: Fetching 15m candles...")
        import time
        now_ms = int(time.time() * 1000)
        from_ms = now_ms - (24 * 60 * 60 * 1000)  # 24 hours back

        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.symbolId = symbol_id
        req.period = ProtoOATrendbarPeriod.M15
        req.fromTimestamp = from_ms
        req.toTimestamp = now_ms
        msg = yield c.send(req)
        bars = Protobuf.extract(msg)

        divisor = 100000.0
        candles = []
        for bar in bars.trendbar:
            low = bar.low / divisor
            high_p = low + (bar.deltaHigh / divisor) if hasattr(bar, "deltaHigh") else low
            open_p = low + (bar.deltaOpen / divisor) if hasattr(bar, "deltaOpen") else low
            close_p = low + (bar.deltaClose / divisor) if hasattr(bar, "deltaClose") else low
            vol = bar.volume / 100.0 if hasattr(bar, "volume") else 0

            candles.append({
                "timestamp": pd.Timestamp.utcfromtimestamp(bar.utcTimestampInMinutes * 60),
                "open": round(open_p, digits),
                "high": round(high_p, digits),
                "low": round(low, digits),
                "close": round(close_p, digits),
                "volume": vol,
            })

        df = pd.DataFrame(candles)
        print(f"   ✅ Fetched {len(df)} candles")
        print(f"   Last price: ${df['close'].iloc[-1]:,.2f}")

        # Print last 5 candles
        for _, row in df.tail(5).iterrows():
            print(f"   {row['timestamp']} | O: ${row['open']:,.2f} H: ${row['high']:,.2f} L: ${row['low']:,.2f} C: ${row['close']:,.2f}")

        # ─── Step 6: Calculate indicators ────────────────────
        print("\n📐 Step 6: Calculating indicators...")
        from data.indicators import calculate_all
        indicators = calculate_all(df)

        if not indicators:
            print("   ⚠️ Not enough candles for full indicators, building minimal set...")
            # Build minimal indicators manually
            close = df["close"]
            current_price = close.iloc[-1]
            indicators = {
                "ema50": round(close.ewm(span=min(50, len(df))).mean().iloc[-1], 2),
                "ema200": round(close.ewm(span=min(200, len(df))).mean().iloc[-1], 2),
                "ema20": round(close.ewm(span=min(20, len(df))).mean().iloc[-1], 2),
                "trend": "BULLISH" if close.ewm(span=min(50, len(df))).mean().iloc[-1] > close.ewm(span=min(200, len(df))).mean().iloc[-1] else "BEARISH",
                "structure": "Mixed/Consolidating",
                "atr": round((df["high"] - df["low"]).tail(14).mean(), 2),
                "rsi": 50.0,
                "current_price": round(current_price, 2),
                "swing_high": round(df["high"].tail(10).max(), 2),
                "swing_low": round(df["low"].tail(10).min(), 2),
                "session_high": round(df["high"].tail(4).max(), 2),
                "session_low": round(df["low"].tail(4).min(), 2),
                "round_levels": [float(int(current_price / 50) * 50 + i * 50) for i in range(-1, 3)],
                "nearest_round": float(round(current_price / 50) * 50),
                "distance_to_round": round(abs(current_price - round(current_price / 50) * 50), 2),
                "24h_high": round(df["high"].max(), 2),
                "24h_low": round(df["low"].min(), 2),
                "sweep_detected": False,
                "sweep_type": "None",
                "sweep_level": 0.0,
                "engulfing_detected": False,
                "macd": None,
                "macd_hist": None,
                "bb_upper": round(close.tail(20).mean() + 2 * close.tail(20).std(), 2) if len(df) >= 20 else 0,
                "bb_lower": round(close.tail(20).mean() - 2 * close.tail(20).std(), 2) if len(df) >= 20 else 0,
                "volume_delta": 0,
                "volume_ratio": 1.0,
                "avg_volume": round(df["volume"].mean(), 2),
            }

        print(f"   ✅ Trend: {indicators['trend']}")
        print(f"   ✅ EMA50: ${indicators['ema50']:,.2f} | EMA200: ${indicators['ema200']:,.2f}")
        print(f"   ✅ ATR: ${indicators['atr']:,.2f}")
        print(f"   ✅ Sweep: {indicators.get('sweep_type', 'None')}")

        # ─── Step 7: Send to Claude ──────────────────────────
        print(f"\n🧠 Step 7: Sending to {config.CLAUDE_MODEL}...")

        from ai.prompt_builder import format_for_claude
        account_info = {"balance": balance, "positions": []}
        prompt = format_for_claude(df, indicators, account_info)

        print(f"   📝 Prompt size: {len(prompt)} chars")

        # Call Claude
        ai_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        from ai.claude_client import SYSTEM_PROMPT
        response = ai_client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=600,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        usage = response.usage

        print(f"   ✅ Response received! ({usage.input_tokens} in / {usage.output_tokens} out)")
        print(f"\n   📋 Claude's raw response:")
        print(f"   {raw}")

        # Parse the decision
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        if not cleaned.startswith("{"):
            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start != -1 and end > start:
                cleaned = cleaned[start:end]

        decision = json.loads(cleaned)

        print(f"\n" + "=" * 60)
        print(f"🎯 CLAUDE'S DECISION:")
        print(f"=" * 60)
        print(f"   Action:       {decision['action']}")
        print(f"   Confidence:   {decision.get('confidence', 'N/A')}%")
        print(f"   Trend Bias:   {decision.get('trend_bias', 'N/A')}")
        print(f"   Sweep:        {'YES ✅' if decision.get('sweep_detected') else 'NO'}")
        print(f"   Session:      {decision.get('session_grade', 'N/A')}-grade")
        print(f"   Entry:        ${decision.get('entry_price', 0):,.2f}")
        print(f"   Stop Loss:    ${decision.get('stop_loss', 0):,.2f}")
        print(f"   Take Profit:  ${decision.get('take_profit', 0):,.2f}")
        print(f"   TP2:          ${decision.get('take_profit_2', 0):,.2f}")
        print(f"   Position %:   {decision.get('position_size_pct', 0)}%")
        print(f"   Reason:       {decision.get('reason', 'N/A')}")

        # ─── Step 8: Execute if BUY or SELL ──────────────────
        if decision["action"] in ["BUY", "SELL"]:
            print(f"\n🚀 Step 8: EXECUTING {decision['action']} on demo...")

            # Calculate R:R
            entry = decision["entry_price"]
            sl = decision["stop_loss"]
            tp = decision["take_profit"]
            risk = abs(entry - sl)
            reward = abs(tp - entry)
            rr = reward / risk if risk > 0 else 0

            print(f"   Risk: ${risk:,.2f} | Reward: ${reward:,.2f} | R:R = {rr:.1f}:1")

            if rr < 2.0:
                print(f"   ⚠️ R:R below 2:1 — but executing on demo for test")

            # Place market order
            order_req = ProtoOANewOrderReq()
            order_req.ctidTraderAccountId = ACCOUNT_ID
            order_req.symbolId = symbol_id
            order_req.orderType = ProtoOAOrderType.MARKET
            order_req.tradeSide = ProtoOATradeSide.BUY if decision["action"] == "BUY" else ProtoOATradeSide.SELL

            # Use minimum volume for test (0.01 lot)
            order_req.volume = 100  # 100 = 0.01 lot in cTrader

            if sl > 0:
                order_req.stopLoss = round(sl * 100000)
                order_req.stopLossTriggerMethod = 1

            if tp > 0:
                order_req.takeProfit = round(tp * 100000)

            msg = yield c.send(order_req)
            result = Protobuf.extract(msg)
            result_type = type(result).__name__

            print(f"\n   📦 Response type: {result_type}")

            if hasattr(result, "position"):
                pos = result.position
                print(f"   ✅ TRADE EXECUTED!")
                print(f"   Position ID: {pos.positionId}")
                print(f"   Entry: ${pos.price / 100000.0:,.2f}")
                print(f"   Volume: {pos.tradeData.volume / 100.0} lots")
            elif hasattr(result, "order"):
                order = result.order
                print(f"   ✅ ORDER PLACED!")
                print(f"   Order ID: {order.orderId}")
            elif hasattr(result, "errorCode"):
                print(f"   ❌ Error: {result.errorCode}")
                if hasattr(result, "description"):
                    print(f"   Description: {result.description}")
            else:
                print(f"   📋 Full response: {result}")

        else:
            print(f"\n⏸️  Claude says HOLD — no trade executed")
            print(f"   Reason: {decision.get('reason', 'N/A')}")

        print(f"\n" + "=" * 60)
        print(f"🎉 TEST COMPLETE!")
        print(f"=" * 60)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

    reactor.stop()


client.setConnectedCallback(run_test)
client.setDisconnectedCallback(lambda c, reason: print(f"Disconnected: {reason}"))
client.setMessageReceivedCallback(lambda c, msg: None)
client.startService()
reactor.run()
