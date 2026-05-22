import sys
import pandas as pd
from datetime import datetime, timezone
import json

# Setup mock data
now = datetime.now(timezone.utc)
mock_candles = pd.DataFrame([
    {"timestamp": now, "open": 2340, "high": 2345, "low": 2335, "close": 2344, "volume": 120},
    {"timestamp": now, "open": 2344, "high": 2344, "low": 2320, "close": 2340, "volume": 350},  # Massive wick
    {"timestamp": now, "open": 2340, "high": 2355, "low": 2340, "close": 2354, "volume": 500},  # Surging volume
])

# 1. Test Context Compressor
print("\n--- TESTING CONTEXT COMPRESSOR ---")
try:
    from ai.context_compressor import compress_candle_history
    summary = compress_candle_history(mock_candles, atr=10, avg_volume=150)
    print("SUCCESS: Context Compressor Output:")
    print(summary)
except Exception as e:
    print(f"FAILED: Context compressor raised {e}")

# 2. Test Prompt Builder
print("\n--- TESTING PROMPT BUILDER ---")
try:
    from ai.prompt_builder import format_for_claude
    mock_indicators = {
        "atr": 10.0, "avg_volume": 150, "current_price": 2354,
        "ema50": 2300, "ema200": 2250, "trend": "BULLISH", "adx": 30
    }
    
    sys_add, user_msg = format_for_claude(
        candles_15m=mock_candles,
        indicators=mock_indicators,
        account={"balance": 10000, "positions": []},
        ml_report="ML Mock Report",
        symbol="XAUUSD"
    )
    print("SUCCESS: Prompt Builder returned successfully!")
    print(f"System Additions length: {len(sys_add)}")
    print(f"User Message length: {len(user_msg)}")
    if "─── RECENT PRICE ACTION SUMMARY (15m) ───────────────" in user_msg:
        print("SUCCESS: Compression block found in User Message")
    else:
        print("WARNING: Compression block NOT found")
except Exception as e:
    print(f"FAILED: Prompt Builder raised {e}")

# 3. Test API Response Cache Logic compilation
print("\n--- TESTING API CACHE SYNTAX ---")
import py_compile
try:
    py_compile.compile("main.py", doraise=True)
    print("SUCCESS: main.py compiles cleanly. Cache variables are syntactically sound.")
except Exception as e:
    print(f"FAILED: main.py syntax error {e}")
