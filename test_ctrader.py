"""
test_ctrader.py — Quick connection test for cTrader Open API.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import *
from ctrader_open_api.messages.OpenApiMessages_pb2 import *
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import *
from twisted.internet import reactor, defer
import time

CLIENT_ID = os.getenv("CTRADER_CLIENT_ID")
CLIENT_SECRET = os.getenv("CTRADER_CLIENT_SECRET")
ACCESS_TOKEN = os.getenv("CTRADER_ACCESS_TOKEN")
ACCOUNT_ID = int(os.getenv("CTRADER_ACCOUNT_ID"))
HOST_MODE = os.getenv("CTRADER_HOST", "demo")

host = EndPoints.PROTOBUF_LIVE_HOST if HOST_MODE == "live" else EndPoints.PROTOBUF_DEMO_HOST
client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)


@defer.inlineCallbacks
def on_connected(c):
    print("=" * 55)
    print("📡 cTrader Connection Test")
    print(f"   Host: {'DEMO' if HOST_MODE == 'demo' else 'LIVE'}")
    print(f"   Account: {ACCOUNT_ID}")
    print("=" * 55)

    try:
        # Step 1: App Auth
        print("\n🔑 Step 1: Authenticating application...")
        req = ProtoOAApplicationAuthReq()
        req.clientId = CLIENT_ID
        req.clientSecret = CLIENT_SECRET
        yield client.send(req)
        print("   ✅ Application authenticated!")

        # Step 2: Account Auth
        print(f"\n🔑 Step 2: Authenticating account {ACCOUNT_ID}...")
        req2 = ProtoOAAccountAuthReq()
        req2.ctidTraderAccountId = ACCOUNT_ID
        req2.accessToken = ACCESS_TOKEN
        yield client.send(req2)
        print("   ✅ Account authenticated!")

        # Step 3: Get accounts list
        print("\n📋 Step 3: Getting accounts list...")
        req_accts = ProtoOAGetAccountListByAccessTokenReq()
        req_accts.accessToken = ACCESS_TOKEN
        resp_accts = yield client.send(req_accts)
        msg_accts = Protobuf.extract(resp_accts)
        print(f"   Response type: {type(msg_accts).__name__}")
        if hasattr(msg_accts, "ctidTraderAccount"):
            for acct in msg_accts.ctidTraderAccount:
                print(f"   📂 Account ID: {acct.ctidTraderAccountId} | Live: {acct.isLive}")
        elif hasattr(msg_accts, "errorCode"):
            print(f"   ⚠️ Error: {msg_accts.errorCode} - {msg_accts.description if hasattr(msg_accts, 'description') else ''}")

        # Step 4: Account Info
        print("\n🏦 Step 4: Getting account info...")
        req3 = ProtoOATraderReq()
        req3.ctidTraderAccountId = ACCOUNT_ID
        resp3 = yield client.send(req3)
        msg3 = Protobuf.extract(resp3)
        print(f"   Response type: {type(msg3).__name__}")
        if hasattr(msg3, "trader"):
            balance = msg3.trader.balance / 100.0
            print(f"   ✅ Balance: ${balance:,.2f}")
        elif hasattr(msg3, "errorCode"):
            print(f"   ⚠️ Error: {msg3.errorCode} - {msg3.description if hasattr(msg3, 'description') else ''}")

        # Step 5: Asset List
        print("\n📋 Step 5: Loading assets...")
        req_assets = ProtoOAAssetListReq()
        req_assets.ctidTraderAccountId = ACCOUNT_ID
        resp_assets = yield client.send(req_assets)
        msg_assets = Protobuf.extract(resp_assets)
        print(f"   Response type: {type(msg_assets).__name__}")
        
        asset_map = {}
        if hasattr(msg_assets, "asset"):
            for a in msg_assets.asset:
                asset_map[a.assetId] = a.name
            print(f"   ✅ Loaded {len(asset_map)} assets")
            # Show some assets
            for aid, aname in list(asset_map.items())[:10]:
                print(f"      {aid}: {aname}")
        elif hasattr(msg_assets, "errorCode"):
            print(f"   ⚠️ Error: {msg_assets.errorCode} - {msg_assets.description if hasattr(msg_assets, 'description') else ''}")

        # Step 6: Symbol List
        print("\n📊 Step 6: Loading symbols...")
        req4 = ProtoOASymbolsListReq()
        req4.ctidTraderAccountId = ACCOUNT_ID
        resp4 = yield client.send(req4)
        msg4 = Protobuf.extract(resp4)
        print(f"   Response type: {type(msg4).__name__}")
        
        if hasattr(msg4, "errorCode"):
            print(f"   ⚠️ Error code: {msg4.errorCode}")
            if hasattr(msg4, "description"):
                print(f"   ⚠️ Description: {msg4.description}")
            if hasattr(msg4, "maintenanceEndTimestamp"):
                print(f"   ⚠️ Maintenance until: {msg4.maintenanceEndTimestamp}")
        
        gold_symbols = []
        symbol_map = {}
        
        if hasattr(msg4, "symbol") and msg4.symbol:
            for sym in msg4.symbol:
                name = sym.symbolName if hasattr(sym, "symbolName") and sym.symbolName else ""
                sid = sym.symbolId
                
                if not name and hasattr(sym, "baseAssetId") and hasattr(sym, "quoteAssetId"):
                    base = asset_map.get(sym.baseAssetId, f"#{sym.baseAssetId}")
                    quote = asset_map.get(sym.quoteAssetId, f"#{sym.quoteAssetId}")
                    name = f"{base}{quote}"
                
                if name:
                    symbol_map[name] = sid
                    if "XAU" in name.upper() or "GOLD" in name.upper():
                        gold_symbols.append((name, sid))
            
            print(f"   ✅ Total symbols: {len(msg4.symbol)} (named: {len(symbol_map)})")
            # Show first 15 symbols
            print(f"   First 15 symbols:")
            for name, sid in list(symbol_map.items())[:15]:
                print(f"      • {name} (ID: {sid})")

        if gold_symbols:
            print(f"\n   🥇 Gold symbols found:")
            for name, sid in gold_symbols:
                print(f"      • {name} (ID: {sid})")

        # Step 7: Try candles if we found a gold symbol
        target_id = None
        target_name = "XAUUSD"
        
        if target_name in symbol_map:
            target_id = symbol_map[target_name]
        elif gold_symbols:
            target_name, target_id = gold_symbols[0]
        
        if target_id:
            print(f"\n📈 Step 7: Fetching {target_name} details & candles...")
            
            # Get symbol details FIRST for correct price conversion
            req6 = ProtoOASymbolByIdReq()
            req6.ctidTraderAccountId = ACCOUNT_ID
            req6.symbolId.append(target_id)
            resp6 = yield client.send(req6)
            msg6 = Protobuf.extract(resp6)
            digits = 2
            pip_pos = 1
            if hasattr(msg6, "symbol") and msg6.symbol:
                s = msg6.symbol[0]
                digits = s.digits
                pip_pos = s.pipPosition if hasattr(s, "pipPosition") else digits - 1
                print(f"   ✅ Digits: {digits}, Pip Position: {pip_pos}")
                if hasattr(s, "lotSize"):
                    print(f"   ✅ Lot size: {s.lotSize / 100.0}")
                if hasattr(s, "minVolume"):
                    print(f"   ✅ Min volume: {s.minVolume}")

            divisor = 100000.0  # cTrader always uses 100,000 as price multiplier
            print(f"   ✅ Price divisor: {divisor}")

            # Fetch candles
            now_ms = int(time.time() * 1000)
            from_ms = now_ms - (20 * 15 * 60 * 1000)

            req5 = ProtoOAGetTrendbarsReq()
            req5.ctidTraderAccountId = ACCOUNT_ID
            req5.symbolId = target_id
            req5.period = ProtoOATrendbarPeriod.M15
            req5.fromTimestamp = from_ms
            req5.toTimestamp = now_ms

            resp5 = yield client.send(req5)
            msg5 = Protobuf.extract(resp5)

            if hasattr(msg5, "trendbar") and msg5.trendbar:
                print(f"   ✅ Fetched {len(msg5.trendbar)} candles")
                
                # Debug: show raw values of last bar
                last = msg5.trendbar[-1]
                print(f"\n   [DEBUG] Raw last bar: low={last.low}, deltaHigh={last.deltaHigh}, deltaOpen={last.deltaOpen}, deltaClose={last.deltaClose}")
                
                print(f"\n   Last 3 candles ({target_name}, 15m):")
                for bar in msg5.trendbar[-3:]:
                    low = bar.low / divisor
                    high = low + (bar.deltaHigh / divisor)
                    open_p = low + (bar.deltaOpen / divisor)
                    close = low + (bar.deltaClose / divisor)
                    from datetime import datetime
                    dt = datetime.utcfromtimestamp(bar.utcTimestampInMinutes * 60)
                    print(f"   {dt} | O: ${open_p:,.2f} H: ${high:,.2f} L: ${low:,.2f} C: ${close:,.2f}")
            else:
                print(f"   ⚠️ No candles (market closed)")
                if hasattr(msg5, "errorCode"):
                    print(f"   Error: {msg5.errorCode}")

        print(f"\n{'=' * 55}")
        print("🎉 CONNECTION TEST COMPLETE!")
        print(f"{'=' * 55}")

    except Exception as e:
        import traceback
        print(f"\n❌ Error: {e}")
        traceback.print_exc()

    reactor.stop()


def on_disconnected(c, reason):
    pass

def on_message(c, msg):
    pass

client.setConnectedCallback(on_connected)
client.setDisconnectedCallback(on_disconnected)
client.setMessageReceivedCallback(on_message)
client.startService()
reactor.run()
