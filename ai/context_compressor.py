"""
context_compressor.py — Summarizes raw OHLCV data into natural language.
"""

import pandas as pd

def compress_candle_history(df: pd.DataFrame, atr: float, avg_volume: float = None) -> str:
    """
    Analyzes the last N candles and returns a natural language summary
    of price action, momentum, wicks, and volume.
    
    Args:
        df: DataFrame with OHLCV data. Should be at least 3 rows.
        atr: Current Average True Range for scaling thresholds.
        avg_volume: Average volume for comparison (optional).
        
    Returns:
        str: English summary of the price action.
    """
    if df.empty or len(df) < 3:
        return "Insufficient data for summary."
        
    # Default ATR if missing or 0
    if not atr or atr <= 0:
        atr = (df['high'] - df['low']).mean()
        if atr == 0: atr = 0.001
        
    # Default avg_vol if missing
    if not avg_volume or avg_volume <= 0:
        avg_volume = df['volume'].mean()
        if avg_volume == 0: avg_volume = 1

    # Look at last 3 candles
    recent = df.tail(3).copy()
    
    summary_lines = []
    
    for i, (_, row) in enumerate(recent.iterrows()):
        candle_idx = 3 - i
        label = f"Candle -{candle_idx}" if candle_idx > 1 else "Current"
        
        o, h, l, c, v = row['open'], row['high'], row['low'], row['close'], row['volume']
        
        # Dimensions
        body_size = abs(c - o)
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        
        # Direction
        is_bullish = c > o
        direction = "Bullish" if is_bullish else "Bearish"
        if body_size < (atr * 0.1):
            direction = "Doji"
            
        # Strength
        strength = ""
        if body_size > (atr * 0.5):
            strength = "Strong "
        elif body_size < (atr * 0.2) and direction != "Doji":
            strength = "Weak "
            
        # Volume
        vol_desc = ""
        if v > avg_volume * 1.5:
            vol_desc = f" on SURGING volume ({v/avg_volume:.1f}x avg)"
        elif v < avg_volume * 0.5:
            vol_desc = f" on low volume"
            
        # Wicks (Rejection logic)
        wick_desc = ""
        if upper_wick > (body_size * 1.5) and upper_wick > (atr * 0.2):
            wick_desc += " with massive UPPER rejection wick"
        if lower_wick > (body_size * 1.5) and lower_wick > (atr * 0.2):
            if wick_desc: wick_desc += " and "
            else: wick_desc += " with "
            wick_desc += "massive LOWER rejection wick"
            
        # Format string
        if direction == "Doji":
            line = f"  {label}: Doji{wick_desc}{vol_desc}."
        else:
            line = f"  {label}: {strength}{direction} body{wick_desc}{vol_desc}."
            
        summary_lines.append(line)
        
    return "\n".join(summary_lines)
