"""
market_data.py — Market data fetching (supports Bybit and cTrader).
"""

import pandas as pd
from datetime import datetime
from utils.logger import log
import config


def get_candles(session, symbol=None, timeframe=None, limit=50):
    """
    Fetch OHLCV candle data from Bybit.

    Args:
        session: Authenticated Bybit session.
        symbol: Trading symbol (default: config.TRADING_SYMBOL).
        timeframe: Timeframe string (default: config.TRADING_TIMEFRAME).
        limit: Number of candles.

    Returns:
        pd.DataFrame: OHLCV data.
    """
    symbol = symbol or config.TRADING_SYMBOL
    timeframe = timeframe or str(config.TRADING_TIMEFRAME)

    try:
        result = session.get_kline(
            category=config.TRADING_CATEGORY,
            symbol=symbol,
            interval=str(timeframe),
            limit=limit,
        )

        raw_candles = result["result"]["list"]

        if not raw_candles:
            log.warning(f"No candle data for {symbol}")
            return pd.DataFrame()

        # Bybit returns newest first — reverse to oldest first
        raw_candles.reverse()

        df = pd.DataFrame(raw_candles, columns=[
            "timestamp", "open", "high", "low", "close",
            "volume", "turnover"
        ])

        # Convert types
        df["timestamp"] = pd.to_datetime(
            df["timestamp"].astype(float), unit="ms", utc=True
        )
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df[["timestamp", "open", "high", "low", "close", "volume"]]

        log.info(
            f"Fetched {len(df)} candles for {symbol} ({timeframe}m) | "
            f"Latest close: ${df['close'].iloc[-1]:,.2f}"
        )

        return df

    except Exception as e:
        log.error(f"Failed to fetch candles for {symbol}: {e}")
        raise


def get_current_price(session, symbol=None):
    """
    Get the current ticker price from Bybit.

    Args:
        session: Authenticated Bybit session.
        symbol: Trading symbol.

    Returns:
        dict: Price data.
    """
    symbol = symbol or config.TRADING_SYMBOL

    try:
        result = session.get_tickers(
            category=config.TRADING_CATEGORY,
            symbol=symbol,
        )

        ticker = result["result"]["list"][0]

        price_data = {
            "last_price": float(ticker["lastPrice"]),
            "bid": float(ticker["bid1Price"]),
            "ask": float(ticker["ask1Price"]),
            "24h_high": float(ticker["highPrice24h"]),
            "24h_low": float(ticker["lowPrice24h"]),
            "24h_volume": float(ticker.get("volume24h", 0)),
        }

        log.debug(f"Ticker {symbol}: ${price_data['last_price']:,.2f}")
        return price_data

    except Exception as e:
        log.error(f"Failed to fetch ticker for {symbol}: {e}")
        raise
