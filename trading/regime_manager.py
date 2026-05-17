"""
regime_manager.py — Market Regime Detection Engine.

Tracks market regimes (Trending vs Ranging) using dual-timeframe ADX logic.
Uses M15 for fast response and H1 for high-timeframe confirmation.
"""

from utils.logger import log
from datetime import datetime, timezone

class RegimeManager:
    def __init__(self):
        self.current_regime = "RANGING_CHOPPY"  # Default safe state
        
    def update_regime(self, m15_adx: float, h1_adx: float, current_price: float, ema50: float) -> str:
        """
        Update the regime state based on Dual-Timeframe ADX and Time-of-Day.
        
        Rules:
        - Asian Session (21:00 - 06:00 UTC) -> Forced RANGING_CHOPPY
        - M15 > 25 AND H1 > 20 -> TRENDING
        - M15 < 20 AND H1 < 25 -> RANGING_CHOPPY
        - Otherwise -> TRANSITIONING
        """
        if m15_adx is None or h1_adx is None or ema50 is None:
            return self.current_regime
            
        new_regime = "TRANSITIONING"
        
        # ─── TIME-OF-DAY KILL ZONE ───────────────
        now_utc = datetime.now(timezone.utc)
        is_asian_session = (now_utc.hour >= 21) or (now_utc.hour < 6)
        
        if is_asian_session:
            new_regime = "RANGING_CHOPPY"
            log.debug("🌙 Asian Session Kill Zone: Forcing RANGING_CHOPPY to prevent fake-outs.")
        else:
            if m15_adx > 25 and h1_adx > 20:
                if current_price > ema50:
                    new_regime = "TRENDING_BULLISH"
                else:
                    new_regime = "TRENDING_BEARISH"
            elif m15_adx < 20 and h1_adx < 25:
                new_regime = "RANGING_CHOPPY"
            
        if new_regime != self.current_regime:
            log.info(f"🔄 REGIME SHIFT: Market changed from {self.current_regime} to {new_regime} (M15 ADX: {m15_adx:.1f} | H1 ADX: {h1_adx:.1f})")
            self.current_regime = new_regime
            
        return self.current_regime

# Global singleton
regime_manager = RegimeManager()
