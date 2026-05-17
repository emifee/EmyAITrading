"""Quick test to verify Bybit API connection and data fetching."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

import config

print("=" * 55)
print("📡 Bybit API Connection Test")
print("=" * 55)

# Check keys
if "your_" in config.BYBIT_API_KEY or not config.BYBIT_API_KEY:
    print("❌ BYBIT_API_KEY is still a placeholder!")
    sys.exit(1)

print(f"✅ API Key: {config.BYBIT_API_KEY[:8]}...{config.BYBIT_API_KEY[-4:]}")
print(f"✅ Mode: {'TESTNET' if config.BYBIT_TESTNET else '🔴 MAINNET'}")

# Test 1: Connect
print("\n📡 Step 1: Connecting to Bybit...")
from data.bybit_client import get_bybit_session, get_account_info, get_server_time

session = get_bybit_session()

# Test 2: Server time
print("\n⏰ Step 2: Server time check...")
server = get_server_time(session)
print(f"   ✅ Server time: {server['result']['timeSecond']}")

# Test 3: Account info
print("\n🏦 Step 3: Account info...")
try:
    account = get_account_info(session)
    print(f"   ✅ Balance: ${account['balance']:,.2f} USDT")
    print(f"   ✅ Equity: ${account['equity']:,.2f} USDT")
    print(f"   ✅ Open positions: {len(account['positions'])}")
except Exception as e:
    print(f"   ⚠️ Account info error (may be normal on testnet): {e}")

# Test 4: Market data
print("\n📊 Step 4: Fetching XAUUSD candles...")
from data.market_data import get_candles, get_current_price

try:
    candles = get_candles(session)
    if not candles.empty:
        print(f"   ✅ Fetched {len(candles)} candles")
        print(f"   ✅ Latest close: ${candles['close'].iloc[-1]:,.2f}")
        print(f"   ✅ Time range: {candles['timestamp'].iloc[0]} → {candles['timestamp'].iloc[-1]}")
        print(f"\n   Last 5 candles:")
        print(candles[['timestamp','open','high','low','close','volume']].tail().to_string(index=False))
    else:
        print("   ⚠️ No candle data returned")
except Exception as e:
    print(f"   ❌ Candle fetch error: {e}")

# Test 5: Ticker
print("\n💲 Step 5: Current price...")
try:
    price = get_current_price(session)
    print(f"   ✅ Last price: ${price['last_price']:,.2f}")
    print(f"   ✅ Bid: ${price['bid']:,.2f} | Ask: ${price['ask']:,.2f}")
    print(f"   ✅ 24H High: ${price['24h_high']:,.2f} | Low: ${price['24h_low']:,.2f}")
except Exception as e:
    print(f"   ❌ Ticker error: {e}")

# Test 6: Indicators
print("\n📐 Step 6: Calculating indicators...")
try:
    from data.indicators import calculate_all
    indicators = calculate_all(candles)
    if indicators:
        print(f"   ✅ RSI(14): {indicators.get('rsi')}")
        print(f"   ✅ EMA20: ${indicators.get('ema20', 0):,.2f}")
        print(f"   ✅ EMA50: ${indicators.get('ema50', 0):,.2f}")
        print(f"   ✅ Trend: {indicators.get('trend')}")
        print(f"   ✅ ATR(14): ${indicators.get('atr', 0):,.2f}")
        print(f"   ✅ MACD: {indicators.get('macd')}")
        print(f"   ✅ BB Upper: ${indicators.get('bb_upper', 0):,.2f} | Lower: ${indicators.get('bb_lower', 0):,.2f}")
    else:
        print("   ⚠️ Not enough data for indicators")
except Exception as e:
    print(f"   ❌ Indicator error: {e}")

print("\n" + "=" * 55)
print("🎉 DATA PIPELINE TEST COMPLETE!")
print("=" * 55)
