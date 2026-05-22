"""
indicators.py — Technical indicator calculations for Trend + Liquidity Sweep strategy.

Computes: EMA50/200 trend, session levels, liquidity sweep detection,
key levels, round numbers, and volume analysis.
"""

import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.volatility import AverageTrueRange, BollingerBands
from utils.logger import log
import config


def calculate_all(df: pd.DataFrame) -> dict:
    """
    Calculate all technical indicators from candle data.

    Args:
        df: DataFrame with columns [timestamp, open, high, low, close, volume].

    Returns:
        dict: All indicator values for the most recent candle.
    """
    min_required = 15
    if df.empty or len(df) < min_required:
        log.debug(f"Insufficient data for indicators (need {min_required} candles, "
                     f"have {len(df)})")
        return {}

    try:
        indicators = {}

        close = df["close"]
        high = df["high"]
        low = df["low"]
        open_p = df["open"]

        # ═══════════════════════════════════════════════════════
        # STEP 1: TREND DIRECTION (EMA 50 / 200)
        # ═══════════════════════════════════════════════════════

        # EMA 50
        ema50_indicator = EMAIndicator(close=close, window=min(config.EMA_LONG, len(df)))
        ema50 = ema50_indicator.ema_indicator()
        indicators["ema50"] = round(ema50.iloc[-1], 2) if not ema50.empty and not pd.isna(ema50.iloc[-1]) else None

        # EMA 200 (use available data, may be approximate with fewer candles)
        ema200_window = min(200, len(df))
        ema200_indicator = EMAIndicator(close=close, window=ema200_window)
        ema200 = ema200_indicator.ema_indicator()
        indicators["ema200"] = round(ema200.iloc[-1], 2) if not ema200.empty and not pd.isna(ema200.iloc[-1]) else None

        # EMA 20 (for short-term reference)
        ema20_indicator = EMAIndicator(close=close, window=min(config.EMA_SHORT, len(df)))
        ema20 = ema20_indicator.ema_indicator()
        indicators["ema20"] = round(ema20.iloc[-1], 2) if not ema20.empty and not pd.isna(ema20.iloc[-1]) else None

        # ADX (Average Directional Index) for Trend Strength
        adx_window = min(14, len(df))
        if len(df) >= adx_window * 2: # ADX needs more data to smooth
            adx_indicator = ADXIndicator(high=high, low=low, close=close, window=adx_window)
            adx = adx_indicator.adx()
            indicators["adx"] = round(adx.iloc[-1], 2) if not adx.empty and not pd.isna(adx.iloc[-1]) else None
        else:
            indicators["adx"] = None

        # Trend determination
        if indicators["ema50"] and indicators["ema200"]:
            if indicators["ema50"] > indicators["ema200"]:
                indicators["trend"] = "BULLISH"
            elif indicators["ema50"] < indicators["ema200"]:
                indicators["trend"] = "BEARISH"
            else:
                indicators["trend"] = "NEUTRAL"
        else:
            indicators["trend"] = "UNKNOWN"

        # Higher Highs / Higher Lows detection (last 10 candles)
        recent = df.tail(10)
        swing_highs = recent["high"].values
        swing_lows = recent["low"].values
        hh = all(swing_highs[i] >= swing_highs[i-1] for i in range(max(1, len(swing_highs)-3), len(swing_highs)))
        hl = all(swing_lows[i] >= swing_lows[i-1] for i in range(max(1, len(swing_lows)-3), len(swing_lows)))
        lh = all(swing_highs[i] <= swing_highs[i-1] for i in range(max(1, len(swing_highs)-3), len(swing_highs)))
        ll = all(swing_lows[i] <= swing_lows[i-1] for i in range(max(1, len(swing_lows)-3), len(swing_lows)))

        if hh and hl:
            indicators["structure"] = "HH/HL (Bullish)"
        elif lh and ll:
            indicators["structure"] = "LH/LL (Bearish)"
        else:
            indicators["structure"] = "Mixed/Consolidating"

        # ═══════════════════════════════════════════════════════
        # STEP 2: KEY LEVELS
        # ═══════════════════════════════════════════════════════

        current_price = close.iloc[-1]

        # Swing highs and lows (last 20 candles)
        lookback = min(20, len(df))
        recent_20 = df.tail(lookback)
        indicators["swing_high"] = round(recent_20["high"].max(), 2)
        indicators["swing_low"] = round(recent_20["low"].min(), 2)

        # Session highs/lows (approximate from last 4-8 candles = ~1-2 hours)
        session_candles = df.tail(min(8, len(df)))
        indicators["session_high"] = round(session_candles["high"].max(), 2)
        indicators["session_low"] = round(session_candles["low"].min(), 2)

        # Round number levels near current price
        base = int(current_price / 50) * 50  # Round to nearest $50
        round_levels = [base - 50, base, base + 50, base + 100]
        indicators["round_levels"] = [float(r) for r in round_levels]

        # Nearest key level (closest round number)
        nearest = min(round_levels, key=lambda x: abs(x - current_price))
        indicators["nearest_round"] = float(nearest)
        indicators["distance_to_round"] = round(abs(current_price - nearest), 2)

        # Daily high/low range
        indicators["24h_high"] = round(df["high"].max(), 2)
        indicators["24h_low"] = round(df["low"].min(), 2)
        
        # Fibonacci Retracements (based on recent swing high/low)
        swing_range = indicators["swing_high"] - indicators["swing_low"]
        if swing_range > 0:
            if current_price > (indicators["swing_high"] + indicators["swing_low"]) / 2:
                # Price is in upper half (bullish pullback)
                indicators["fib_0382"] = round(indicators["swing_high"] - (swing_range * 0.382), 2)
                indicators["fib_0500"] = round(indicators["swing_high"] - (swing_range * 0.500), 2)
                indicators["fib_0618"] = round(indicators["swing_high"] - (swing_range * 0.618), 2)
            else:
                # Price is in lower half (bearish pullback)
                indicators["fib_0382"] = round(indicators["swing_low"] + (swing_range * 0.382), 2)
                indicators["fib_0500"] = round(indicators["swing_low"] + (swing_range * 0.500), 2)
                indicators["fib_0618"] = round(indicators["swing_low"] + (swing_range * 0.618), 2)
        else:
            indicators["fib_0382"] = indicators["fib_0500"] = indicators["fib_0618"] = None

        # Rolling Session VWAP (last 40 candles = ~10 hours on 15m)
        vwap_lookback = min(40, len(df))
        vwap_df = df.tail(vwap_lookback).copy()
        typical_price = (vwap_df["high"] + vwap_df["low"] + vwap_df["close"]) / 3
        vwap = (typical_price * vwap_df["volume"]).cumsum() / vwap_df["volume"].cumsum()
        indicators["vwap"] = round(vwap.iloc[-1], 2) if not vwap.empty and not pd.isna(vwap.iloc[-1]) else None

        # ═══════════════════════════════════════════════════════
        # STEP 3: LIQUIDITY SWEEP DETECTION
        # ═══════════════════════════════════════════════════════

        # Check last 3 candles for sweep patterns
        sweep_info = _detect_liquidity_sweep(df, indicators)
        indicators.update(sweep_info)

        # ═══════════════════════════════════════════════════════
        # STEP 4: SUPPORTING INDICATORS
        # ═══════════════════════════════════════════════════════

        # RSI (14) — for context, not entry trigger
        rsi_indicator = RSIIndicator(close=close, window=min(config.RSI_PERIOD, len(df) - 1))
        rsi_series = rsi_indicator.rsi()
        indicators["rsi"] = round(rsi_series.iloc[-1], 2) if not rsi_series.empty and not pd.isna(rsi_series.iloc[-1]) else None

        # ATR (14) — for stop loss / volatility measurement
        atr_indicator = AverageTrueRange(high=high, low=low, close=close, window=min(config.ATR_PERIOD, len(df) - 1))
        atr_series = atr_indicator.average_true_range()
        indicators["atr"] = round(atr_series.iloc[-1], 2) if not atr_series.empty and not pd.isna(atr_series.iloc[-1]) else None

        # MACD — momentum confirmation
        if len(df) >= 26:
            macd_indicator = MACD(close=close)
            macd_line = macd_indicator.macd()
            signal_line = macd_indicator.macd_signal()
            macd_hist = macd_indicator.macd_diff()
            indicators["macd"] = round(macd_line.iloc[-1], 2) if not pd.isna(macd_line.iloc[-1]) else None
            indicators["signal"] = round(signal_line.iloc[-1], 2) if not pd.isna(signal_line.iloc[-1]) else None
            indicators["macd_hist"] = round(macd_hist.iloc[-1], 2) if not pd.isna(macd_hist.iloc[-1]) else None
        else:
            indicators["macd"] = None
            indicators["signal"] = None
            indicators["macd_hist"] = None

        # Bollinger Bands
        bb_window = min(config.BB_PERIOD, len(df))
        bb_indicator = BollingerBands(close=close, window=bb_window, window_dev=config.BB_STD)
        indicators["bb_upper"] = round(bb_indicator.bollinger_hband().iloc[-1], 2) if not pd.isna(bb_indicator.bollinger_hband().iloc[-1]) else None
        indicators["bb_middle"] = round(bb_indicator.bollinger_mavg().iloc[-1], 2) if not pd.isna(bb_indicator.bollinger_mavg().iloc[-1]) else None
        indicators["bb_lower"] = round(bb_indicator.bollinger_lband().iloc[-1], 2) if not pd.isna(bb_indicator.bollinger_lband().iloc[-1]) else None

        if indicators["bb_middle"] and indicators["bb_middle"] > 0:
            indicators["bb_bandwidth"] = round(
                (indicators["bb_upper"] - indicators["bb_lower"]) / indicators["bb_middle"] * 100, 4
            )
        else:
            indicators["bb_bandwidth"] = None

        # Volume analysis
        df_temp = df.copy()
        df_temp["vol_delta"] = df_temp.apply(
            lambda row: row["volume"] if row["close"] >= row["open"] else -row["volume"],
            axis=1
        )
        indicators["volume_delta"] = round(df_temp["vol_delta"].tail(5).sum(), 2)
        indicators["avg_volume"] = round(df["volume"].tail(20).mean(), 2)

        # Current candle volume vs average
        current_vol = df["volume"].iloc[-1]
        prev_vol = df["volume"].iloc[-2] if len(df) > 1 else 0
        avg_vol = indicators["avg_volume"]
        
        if avg_vol > 0:
            indicators["volume_ratio"] = round(current_vol / avg_vol, 2)
            indicators["prev_volume_ratio"] = round(prev_vol / avg_vol, 2)
        else:
            indicators["volume_ratio"] = 0
            indicators["prev_volume_ratio"] = 0

        # Current price
        indicators["current_price"] = round(current_price, 2)

        log.debug(
            f"Indicators | Trend: {indicators['trend']} | "
            f"EMA50: {indicators['ema50']} | EMA200: {indicators['ema200']} | "
            f"Sweep: {indicators.get('sweep_type', 'None')} | "
            f"ATR: {indicators['atr']}"
        )

        return indicators

    except Exception as e:
        log.error(f"Indicator calculation failed: {e}")
        import traceback
        traceback.print_exc()
        raise


def _detect_liquidity_sweep(df: pd.DataFrame, indicators: dict) -> dict:
    """
    Detect liquidity sweep patterns in recent candles.

    A sweep occurs when price wicks aggressively through a key level
    but closes back inside — indicating stop hunts before reversal.

    Returns:
        dict: Sweep detection data.
    """
    result = {
        "sweep_detected": False,
        "sweep_type": "None",
        "sweep_level": 0.0,
        "sweep_candle_idx": -1,
        "engulfing_detected": False,
    }

    if len(df) < 5:
        return result

    # Get key reference levels
    session_low = indicators.get("session_low", 0)
    session_high = indicators.get("session_high", 0)
    swing_low = indicators.get("swing_low", 0)
    swing_high = indicators.get("swing_high", 0)
    avg_volume = indicators.get("avg_volume", 1)

    # Check last 3 candles for sweep patterns
    for i in range(-3, 0):
        if abs(i) > len(df):
            continue

        candle = df.iloc[i]
        prev = df.iloc[i - 1] if abs(i - 1) <= len(df) else None

        body = abs(candle["close"] - candle["open"])
        upper_wick = candle["high"] - max(candle["close"], candle["open"])
        lower_wick = min(candle["close"], candle["open"]) - candle["low"]
        total_range = candle["high"] - candle["low"]

        if total_range == 0:
            continue

        # ─── Bullish Sweep (sweep below support, close above) ─────
        # Long lower wick, price dipped below a key level but closed above it
        if lower_wick > body * 2.5 and lower_wick > upper_wick * 2.5:
            # Check if the wick went below a key level
            levels_to_check = [session_low, swing_low]
            for level in levels_to_check:
                if level > 0 and candle["low"] < level and candle["close"] > level:
                    # Volume confirmation
                    vol_spike = candle["volume"] > avg_volume * 1.5 if avg_volume > 0 else True

                    if vol_spike:
                        result["sweep_detected"] = True
                        result["sweep_type"] = "BULLISH_SWEEP"
                        result["sweep_level"] = round(level, 2)
                        result["sweep_candle_idx"] = i

        # ─── Bearish Sweep (sweep above resistance, close below) ───
        # Long upper wick, price spiked above a key level but closed below it
        if upper_wick > body * 2.5 and upper_wick > lower_wick * 2.5:
            levels_to_check = [session_high, swing_high]
            for level in levels_to_check:
                if level > 0 and candle["high"] > level and candle["close"] < level:
                    vol_spike = candle["volume"] > avg_volume * 1.5 if avg_volume > 0 else True

                    if vol_spike:
                        result["sweep_detected"] = True
                        result["sweep_type"] = "BEARISH_SWEEP"
                        result["sweep_level"] = round(level, 2)
                        result["sweep_candle_idx"] = i

    # ─── Engulfing candle detection (entry trigger) ─────────────
    if result["sweep_detected"] and len(df) >= 2:
        last = df.iloc[-1]
        prev = df.iloc[-2]

        # Bullish engulfing: current candle body engulfs previous
        if (result["sweep_type"] == "BULLISH_SWEEP" and
                last["close"] > last["open"] and
                last["close"] > prev["open"] and
                last["open"] < prev["close"]):
            result["engulfing_detected"] = True

        # Bearish engulfing
        elif (result["sweep_type"] == "BEARISH_SWEEP" and
                last["close"] < last["open"] and
                last["close"] < prev["open"] and
                last["open"] > prev["close"]):
            result["engulfing_detected"] = True

    return result
