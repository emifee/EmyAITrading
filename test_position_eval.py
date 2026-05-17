"""
test_position_eval.py — Test Claude evaluating an open position.
Shows the full position management flow.
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


@defer.inlineCallbacks
def run_test(c):
    print("=" * 60)
    print("🧪 POSITION EVALUATION TEST — Claude reads open trades")
    print("=" * 60)

    try:
        # Auth
        req = ProtoOAApplicationAuthReq()
        req.clientId = CLIENT_ID
        req.clientSecret = CLIENT_SECRET
        yield c.send(req)
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        req.accessToken = ACCESS_TOKEN
        yield c.send(req)
        print("✅ Authenticated")

        # Balance
        req = ProtoOATraderReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        msg = yield c.send(req)
        t = Protobuf.extract(msg)
        balance = t.trader.balance / 100.0
        print(f"💰 Balance: ${balance:,.2f}")

        # Symbols
        req = ProtoOAAssetListReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        msg = yield c.send(req)
        assets = {a.assetId: a.name for a in Protobuf.extract(msg).asset}

        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        msg = yield c.send(req)
        symbol_id = None
        for s in Protobuf.extract(msg).symbol:
            if assets.get(s.baseAssetId, "") + assets.get(s.quoteAssetId, "") == "XAUUSD":
                symbol_id = s.symbolId
                break

        # Get open positions
        print("\n📍 Checking open positions...")
        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = ACCOUNT_ID
        msg = yield c.send(req)
        r = Protobuf.extract(msg)

        positions = []
        for pos in r.position:
            side = "BUY" if pos.tradeData.tradeSide == 1 else "SELL"
            entry = pos.price
            sl = pos.stopLoss if pos.stopLoss else 0
            tp = pos.takeProfit if pos.takeProfit else 0
            vol = pos.tradeData.volume / 100.0

            positions.append({
                "positionId": pos.positionId,
                "side": side,
                "volume": vol,
                "entryPrice": entry,
                "stopLoss": sl,
                "takeProfit": tp,
                "currentPrice": 0,  # will use candle close
                "unrealizedPnl": 0,
            })

            print(f"   ⚡ {side} {vol} lots @ ${entry:,.2f}")
            print(f"     SL: ${sl:,.2f} | TP: ${tp:,.2f}")
            print(f"     ID: {pos.positionId}")

        if not positions:
            print("   ❌ No open positions found — nothing to evaluate")
            reactor.stop()
            return

        # Fetch candles
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
        candles = []
        for bar in bars.trendbar:
            low = bar.low / divisor
            candles.append({
                "timestamp": pd.Timestamp.utcfromtimestamp(bar.utcTimestampInMinutes * 60),
                "open": round(low + bar.deltaOpen / divisor, 2),
                "high": round(low + bar.deltaHigh / divisor, 2),
                "low": round(low, 2),
                "close": round(low + bar.deltaClose / divisor, 2),
                "volume": bar.volume / 100.0 if hasattr(bar, "volume") else 0,
            })

        df = pd.DataFrame(candles)
        current_price = df["close"].iloc[-1]
        print(f"\n📈 {len(df)} candles | Current: ${current_price:,.2f}")

        # Update position with current price
        for p in positions:
            p["currentPrice"] = current_price
            if p["side"] == "BUY":
                p["unrealizedPnl"] = round((current_price - p["entryPrice"]) * p["volume"], 2)
            else:
                p["unrealizedPnl"] = round((p["entryPrice"] - current_price) * p["volume"], 2)
            print(f"   P&L: ${p['unrealizedPnl']:,.2f}")

        # Build indicators
        from data.indicators import calculate_all
        indicators = calculate_all(df)
        if not indicators:
            close = df["close"]
            indicators = {
                "ema50": round(close.ewm(span=min(50, len(df))).mean().iloc[-1], 2),
                "ema200": round(close.ewm(span=min(200, len(df))).mean().iloc[-1], 2),
                "ema20": round(close.ewm(span=min(20, len(df))).mean().iloc[-1], 2),
                "trend": "BULLISH" if close.ewm(span=min(50, len(df))).mean().iloc[-1] > close.ewm(span=min(200, len(df))).mean().iloc[-1] else "BEARISH",
                "structure": "Mixed",
                "atr": round((df["high"] - df["low"]).tail(14).mean(), 2),
                "rsi": 50.0, "current_price": round(current_price, 2),
                "swing_high": round(df["high"].tail(10).max(), 2),
                "swing_low": round(df["low"].tail(10).min(), 2),
                "session_high": round(df["high"].tail(4).max(), 2),
                "session_low": round(df["low"].tail(4).min(), 2),
                "round_levels": [float(int(current_price / 50) * 50 + i * 50) for i in range(-1, 3)],
                "nearest_round": float(round(current_price / 50) * 50),
                "distance_to_round": 0, "24h_high": round(df["high"].max(), 2),
                "24h_low": round(df["low"].min(), 2),
                "sweep_detected": False, "sweep_type": "None",
                "sweep_level": 0.0, "engulfing_detected": False,
                "macd": None, "macd_hist": None,
                "bb_upper": 0, "bb_lower": 0,
                "volume_delta": 0, "volume_ratio": 1.0,
                "avg_volume": round(df["volume"].mean(), 2),
            }

        # Build prompt WITH positions
        from ai.prompt_builder import format_for_claude
        account_data = {"balance": balance, "positions": positions}
        prompt = format_for_claude(df, indicators, account_data)

        print(f"\n🧠 Sending to Claude with POSITION data ({len(prompt)} chars)...")

        from ai.claude_client import SYSTEM_PROMPT
        ai_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = ai_client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=600,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        usage = response.usage
        print(f"✅ Response ({usage.input_tokens} in / {usage.output_tokens} out)")

        # Parse
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        if not cleaned.startswith("{"):
            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            cleaned = cleaned[start:end]

        decision = json.loads(cleaned)

        print(f"\n{'=' * 60}")
        print(f"🎯 CLAUDE'S POSITION EVALUATION:")
        print(f"{'=' * 60}")
        print(f"   Action:          {decision['action']}")
        print(f"   Confidence:      {decision.get('confidence', 'N/A')}%")
        print(f"   Trend Bias:      {decision.get('trend_bias', 'N/A')}")
        print(f"   Sweep vs Trade:  {'YES ⚠️' if decision.get('sweep_detected') else 'No'}")
        print(f"   Session Grade:   {decision.get('session_grade', 'N/A')}")
        if decision.get('position_action_reason'):
            print(f"   Mgmt Reason:     {decision['position_action_reason']}")
        print(f"   Reason:          {decision.get('reason', 'N/A')}")

        action = decision["action"]
        if action == "HOLD":
            print(f"\n   ✅ Claude says KEEP the position — conditions still valid")
        elif action == "CLOSE_TRADE":
            print(f"\n   🔒 Claude says CLOSE THE TRADE — reversal detected!")
        elif action == "MOVE_SL_BE":
            print(f"\n   🛡️ Claude says MOVE SL TO BREAKEVEN — lock in protection!")
        elif action == "PARTIAL_CLOSE":
            print(f"\n   ✂️ Claude says TAKE PARTIAL PROFITS!")
        else:
            print(f"\n   📝 {action} — unexpected for position management")

        print(f"\n{'=' * 60}")
        print(f"🎉 POSITION EVALUATION COMPLETE!")
        print(f"{'=' * 60}")

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
