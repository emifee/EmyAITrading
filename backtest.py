import pandas as pd
import numpy as np
import yfinance as yf
from ta.trend import EMAIndicator, ADXIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

def backtest_prefilter():
    print("📥 Downloading 60 days of 15m XAUUSD data...")
    df = yf.download("GC=F", interval="15m", period="60d", progress=False)
    
    if len(df) == 0:
        print("❌ Error: yfinance returned no data. Market might be closed or symbol changed.")
        return

    # Handle yfinance multi-index columns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
        
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
    df = df.dropna()

    print(f"📊 Calculating indicators for {len(df)} candles...")
    
    # Indicators
    df["ema50"] = EMAIndicator(close=df["close"], window=50).ema_indicator()
    df["ema200"] = EMAIndicator(close=df["close"], window=200).ema_indicator()
    
    adx_ind = ADXIndicator(high=df["high"], low=df["low"], close=df["close"], window=14)
    df["adx"] = adx_ind.adx()
    
    macd_ind = MACD(close=df["close"])
    df["macd_hist"] = macd_ind.macd_diff()
    
    df["rsi"] = RSIIndicator(close=df["close"]).rsi()
    df["atr"] = AverageTrueRange(high=df["high"], low=df["low"], close=df["close"], window=14).average_true_range()
    
    # Moving volume average
    df["avg_volume"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["avg_volume"]
    
    df = df.dropna()
    
    print("⚙️ Simulating Python Pre-Filter logic...")
    
    # Vectorized Scoring
    score = np.zeros(len(df))
    
    # 1. Trend Flatness
    trend_condition = (df["ema50"] > df["ema200"]) | (df["ema50"] < df["ema200"])
    score = np.where(trend_condition, score + 15, score)
    
    # 2. ADX
    score = np.where(df["adx"] > 25, score + 15, score)
    score = np.where((df["adx"] <= 25) & (df["adx"] > 20), score + 10, score)
    
    # 3. Volume
    score = np.where(df["volume_ratio"] > 1.5, score + 15, score)
    score = np.where((df["volume_ratio"] <= 1.5) & (df["volume_ratio"] > 1.0), score + 10, score)
    score = np.where((df["volume_ratio"] <= 1.0) & (df["volume_ratio"] > 0.5), score + 5, score)
    
    # 4. Sweep Detection
    body = abs(df["close"] - df["open"])
    upper_wick = df["high"] - np.maximum(df["close"], df["open"])
    lower_wick = np.minimum(df["close"], df["open"]) - df["low"]
    
    sweep_condition = ((upper_wick > body * 1.5) & (upper_wick > df["atr"] * 0.3)) | \
                      ((lower_wick > body * 1.5) & (lower_wick > df["atr"] * 0.3))
    score = np.where(sweep_condition, score + 25, score)
    
    # 5. RSI
    score = np.where((df["rsi"] < 30) | (df["rsi"] > 70), score + 15, score)
    score = np.where(((df["rsi"] >= 30) & (df["rsi"] < 35)) | ((df["rsi"] <= 70) & (df["rsi"] > 65)), score + 10, score)
    score = np.where(((df["rsi"] >= 35) & (df["rsi"] < 45)) | ((df["rsi"] <= 65) & (df["rsi"] > 55)), score + 5, score)
    
    # 6. MACD Hist
    score = np.where(abs(df["macd_hist"]) > 2, score + 10, score)
    score = np.where((abs(df["macd_hist"]) <= 2) & (abs(df["macd_hist"]) > 1), score + 5, score)
    
    df["py_score"] = score
    
    # Only execute where score >= 65
    signals = df[(df["py_score"] >= 65) & (df["volume_ratio"] >= 0.3)].copy()
    print(f"🎯 Found {len(signals)} setups out of {len(df)} candles.")
    
    # Simulate execution (R/R = 1.5)
    wins = 0
    losses = 0
    pnl = 0.0
    
    for idx in signals.index:
        pos_idx = df.index.get_loc(idx)
        if pos_idx >= len(df) - 1:
            continue
            
        entry_price = df.iloc[pos_idx]["close"]
        atr = df.iloc[pos_idx]["atr"]
        is_bullish = df.iloc[pos_idx]["macd_hist"] > 0
        
        if is_bullish:
            tp = entry_price + (atr * 1.5)
            sl = entry_price - (atr * 1.0)
        else:
            tp = entry_price - (atr * 1.5)
            sl = entry_price + (atr * 1.0)
            
        trade_result = None
        for fwd_idx in range(pos_idx + 1, min(len(df), pos_idx + 48)): # 12 hr max
            future_high = df.iloc[fwd_idx]["high"]
            future_low = df.iloc[fwd_idx]["low"]
            
            if is_bullish:
                if future_low <= sl:
                    trade_result = "LOSS"
                    pnl -= 1.0
                    break
                elif future_high >= tp:
                    trade_result = "WIN"
                    pnl += 1.5
                    break
            else:
                if future_high >= sl:
                    trade_result = "LOSS"
                    pnl -= 1.0
                    break
                elif future_low <= tp:
                    trade_result = "WIN"
                    pnl += 1.5
                    break
                    
        if trade_result == "WIN": wins += 1
        elif trade_result == "LOSS": losses += 1
        
    total_trades = wins + losses
    win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0
    
    print("\n" + "="*40)
    print("📈 PYTHON PRE-FILTER BACKTEST RESULTS")
    print("="*40)
    print(f"Dataset: Last 60 Days (15m candles)")
    print(f"Total Evaluated Candles: {len(df)}")
    print(f"Total Trades Taken:      {total_trades}")
    print(f"Win Rate (1.5 R/R):      {win_rate:.1f}%")
    print(f"Net Profit (Risk Units): {pnl:+.2f} R")
    print(f"Average Trades/Day:      {total_trades / 60:.1f}")
    print("="*40)

if __name__ == "__main__":
    backtest_prefilter()
