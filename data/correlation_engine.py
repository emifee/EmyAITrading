"""
correlation_engine.py — Calculates global Dollar Strength using EURUSD and USDJPY trends.
"""

from utils.logger import log
from trading.indicators import calculate_all

def get_dollar_strength(tick_aggregators):
    """
    Analyzes EURUSD and USDJPY to determine global Dollar Strength.
    
    Args:
        tick_aggregators (dict): Dictionary of TickAggregators for all symbols.
        
    Returns:
        str: A formatted string describing the Dollar Strength, or empty string if unavailable.
    """
    try:
        eurusd_agg = tick_aggregators.get("EURUSD")
        usdjpy_agg = tick_aggregators.get("USDJPY")
        
        if not eurusd_agg or not usdjpy_agg:
            return ""
            
        eurusd_m15 = eurusd_agg.get_candles(15)
        usdjpy_m15 = usdjpy_agg.get_candles(15)
        
        if eurusd_m15.empty or usdjpy_m15.empty:
            return ""
            
        eurusd_ind = calculate_all(eurusd_m15)
        usdjpy_ind = calculate_all(usdjpy_m15)
        
        if not eurusd_ind or not usdjpy_ind:
            return ""
            
        eurusd_trend = eurusd_ind.get("trend", "UNKNOWN")
        usdjpy_trend = usdjpy_ind.get("trend", "UNKNOWN")
        
        if eurusd_trend == "UNKNOWN" or usdjpy_trend == "UNKNOWN":
            return ""
            
        # Calculate Dollar Strength
        # USD is the quote in EURUSD (so Bearish EURUSD = Strong USD)
        # USD is the base in USDJPY (so Bullish USDJPY = Strong USD)
        
        if eurusd_trend == "BEARISH" and usdjpy_trend == "BULLISH":
            strength = "🔥 EXTREMELY STRONG (Bearish for Gold)"
        elif eurusd_trend == "BULLISH" and usdjpy_trend == "BEARISH":
            strength = "🧊 EXTREMELY WEAK (Bullish for Gold)"
        elif eurusd_trend == "BEARISH" or usdjpy_trend == "BULLISH":
            strength = "📈 MODERATELY STRONG"
        elif eurusd_trend == "BULLISH" or usdjpy_trend == "BEARISH":
            strength = "📉 MODERATELY WEAK"
        else:
            strength = "⚖️ MIXED / CHOPPY"
            
        report = (
            f"─── 🌍 GLOBAL DOLLAR CORRELATION ──────────────────\n"
            f"EURUSD (Inverse): {eurusd_trend}\n"
            f"USDJPY (Direct):  {usdjpy_trend}\n"
            f"Dollar Strength:  {strength}\n"
            f"⚡ CORRELATION RULE: If Dollar is extremely strong, AVOID buying Gold. If Dollar is extremely weak, AVOID shorting Gold.\n\n"
        )
        return report
        
    except Exception as e:
        log.error(f"Failed to calculate dollar strength: {e}")
        return ""
