"""
test_indicators.py — Tests for technical indicator calculations.
"""

import sys
import os
import pytest
import pandas as pd
import numpy as np

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def create_sample_candles(n=60):
    """Create sample OHLCV data for testing."""
    np.random.seed(42)
    base_price = 3200.0
    prices = base_price + np.cumsum(np.random.randn(n) * 5)

    df = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=n, freq="15min"),
        "open": prices + np.random.randn(n) * 2,
        "high": prices + abs(np.random.randn(n) * 8),
        "low": prices - abs(np.random.randn(n) * 8),
        "close": prices,
        "volume": np.random.randint(100, 10000, n).astype(float),
    })

    # Ensure high >= open, close and low <= open, close
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)

    return df


class TestIndicators:
    """Tests for the indicators module."""

    def test_calculate_all_returns_dict(self):
        """calculate_all should return a dictionary of indicators."""
        from data.indicators import calculate_all

        df = create_sample_candles(60)
        result = calculate_all(df)

        assert isinstance(result, dict)
        assert len(result) > 0

    def test_rsi_in_range(self):
        """RSI should be between 0 and 100."""
        from data.indicators import calculate_all

        df = create_sample_candles(60)
        result = calculate_all(df)

        assert result["rsi"] is not None
        assert 0 <= result["rsi"] <= 100

    def test_ema_values_present(self):
        """EMA20 and EMA50 should be calculated."""
        from data.indicators import calculate_all

        df = create_sample_candles(60)
        result = calculate_all(df)

        assert result["ema20"] is not None
        assert result["ema50"] is not None
        assert result["ema20"] > 0
        assert result["ema50"] > 0

    def test_trend_direction(self):
        """Trend should be BULLISH or BEARISH."""
        from data.indicators import calculate_all
    
        df = create_sample_candles(60)
        result = calculate_all(df)
    
        assert result["trend"] in ["BULLISH", "BEARISH", "NEUTRAL"]

    def test_atr_positive(self):
        """ATR should be a positive number."""
        from data.indicators import calculate_all

        df = create_sample_candles(60)
        result = calculate_all(df)

        assert result["atr"] is not None
        assert result["atr"] > 0

    def test_macd_present(self):
        """MACD, signal, and histogram should be calculated."""
        from data.indicators import calculate_all

        df = create_sample_candles(60)
        result = calculate_all(df)

        assert result["macd"] is not None
        assert result["signal"] is not None
        assert result["macd_hist"] is not None

    def test_bollinger_bands_ordered(self):
        """BB Upper > BB Middle > BB Lower."""
        from data.indicators import calculate_all

        df = create_sample_candles(60)
        result = calculate_all(df)

        assert result["bb_upper"] > result["bb_middle"]
        assert result["bb_middle"] > result["bb_lower"]

    def test_swing_levels(self):
        """Swing high should be >= swing low."""
        from data.indicators import calculate_all

        df = create_sample_candles(60)
        result = calculate_all(df)

        assert result["swing_high"] >= result["swing_low"]

    def test_insufficient_data_returns_empty(self):
        """Should return empty dict if not enough candles."""
        from data.indicators import calculate_all

        df = create_sample_candles(10)  # Need at least 50
        result = calculate_all(df)

        assert result == {}

    def test_empty_dataframe_returns_empty(self):
        """Should return empty dict for empty DataFrame."""
        from data.indicators import calculate_all

        df = pd.DataFrame()
        result = calculate_all(df)

        assert result == {}
