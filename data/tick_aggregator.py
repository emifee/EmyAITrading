"""
tick_aggregator.py — Real-time tick-to-candle aggregation.

Collects live price ticks from cTrader and builds OHLCV candles locally.
Maintains rolling buffers for multiple timeframes (1m, 5m, 15m).
Thread-safe for concurrent access from analysis and monitor loops.
"""

import time
import threading
from datetime import datetime, timezone
from collections import deque
import pandas as pd
from utils.logger import log


class TickAggregator:
    """
    Aggregates real-time ticks into OHLCV candles.

    Usage:
        agg = TickAggregator()
        agg.on_tick(bid=3340.50, ask=3340.80)  # called on every spot event
        candles_15m = agg.get_candles(15)  # returns DataFrame
    """

    def __init__(self, max_candles=200):
        """
        Args:
            max_candles: Maximum candles to store per timeframe.
        """
        self.max_candles = max_candles
        self._lock = threading.Lock()

        # Current tick data
        self.last_bid = 0.0
        self.last_ask = 0.0
        self.last_mid = 0.0
        self.last_tick_time = 0
        self.tick_count = 0

        # Supported timeframes (in minutes)
        self._timeframes = [1, 5, 15, 60, 240]

        # Current building candles: {timeframe: {open, high, low, close, volume, start_time}}
        self._building = {}

        # Completed candle buffers: {timeframe: deque of dicts}
        self._candles = {}

        for tf in self._timeframes:
            self._building[tf] = None
            self._candles[tf] = deque(maxlen=max_candles)

        log.info(f"📊 Tick aggregator initialized (timeframes: {self._timeframes}, max: {max_candles})")

    def on_tick(self, bid: float, ask: float, timestamp_ms: int = None):
        """
        Process an incoming price tick.

        Args:
            bid: Current bid price.
            ask: Current ask price.
            timestamp_ms: Tick timestamp in ms (uses current time if None).
        """
        if bid <= 0 or ask <= 0:
            return

        mid = (bid + ask) / 2.0
        ts = timestamp_ms or int(time.time() * 1000)

        with self._lock:
            self.last_bid = bid
            self.last_ask = ask
            self.last_mid = mid
            self.last_tick_time = ts
            self.tick_count += 1

            # Update all timeframe candles
            for tf in self._timeframes:
                self._update_candle(tf, mid, ts)

    def _update_candle(self, timeframe: int, price: float, timestamp_ms: int):
        """Update or create a candle for the given timeframe."""
        # Calculate candle start time (floor to timeframe boundary)
        ts_sec = timestamp_ms / 1000.0
        period_sec = timeframe * 60
        candle_start = int(ts_sec // period_sec) * period_sec

        current = self._building[timeframe]

        if current is None or current["start_time"] != candle_start:
            # Finalize the previous candle if it exists
            if current is not None:
                self._candles[timeframe].append(current.copy())

            # Start a new candle
            self._building[timeframe] = {
                "start_time": candle_start,
                "timestamp": datetime.fromtimestamp(candle_start, tz=timezone.utc),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 1,
            }
        else:
            # Update the current candle
            current["high"] = max(current["high"], price)
            current["low"] = min(current["low"], price)
            current["close"] = price
            current["volume"] += 1

    def get_candles(self, timeframe: int = 15, include_building: bool = True) -> pd.DataFrame:
        """
        Get aggregated candles as a DataFrame.

        Args:
            timeframe: Candle timeframe in minutes (1, 5, or 15).
            include_building: Whether to include the currently building candle.

        Returns:
            pd.DataFrame: OHLCV data with timestamp, open, high, low, close, volume.
        """
        if timeframe not in self._timeframes:
            log.warning(f"Timeframe {timeframe}m not tracked, using 15m")
            timeframe = 15

        with self._lock:
            candles = list(self._candles[timeframe])

            if include_building and self._building[timeframe] is not None:
                candles.append(self._building[timeframe].copy())

        if not candles:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        df = pd.DataFrame(candles)
        df = df[["timestamp", "open", "high", "low", "close", "volume"]]

        return df

    def load_historical(self, candles_data: list, timeframe: int = 15):
        """
        Preload historical candles from cTrader API response.

        Args:
            candles_data: List of dicts with OHLCV data.
            timeframe: Timeframe in minutes.
        """
        if timeframe not in self._timeframes:
            return

        with self._lock:
            self._candles[timeframe].clear()

            for c in candles_data:
                ts = c.get("timestamp", 0)
                if isinstance(ts, (int, float)):
                    ts_dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
                else:
                    ts_dt = ts

                self._candles[timeframe].append({
                    "start_time": int(ts_dt.timestamp()) if hasattr(ts_dt, 'timestamp') else 0,
                    "timestamp": ts_dt,
                    "open": c["open"],
                    "high": c["high"],
                    "low": c["low"],
                    "close": c["close"],
                    "volume": c.get("volume", 0),
                })

        log.info(f"📊 Loaded {len(candles_data)} historical {timeframe}m candles")

    def get_current_price(self) -> dict:
        """Get the latest tick price data."""
        with self._lock:
            return {
                "bid": self.last_bid,
                "ask": self.last_ask,
                "mid": self.last_mid,
                "spread": round(self.last_ask - self.last_bid, 2),
                "tick_count": self.tick_count,
                "last_update": self.last_tick_time,
            }

    def get_summary(self) -> str:
        """Get a human-readable summary of the aggregator state."""
        with self._lock:
            lines = [f"Tick count: {self.tick_count} | Last: ${self.last_mid:,.2f}"]
            for tf in self._timeframes:
                n = len(self._candles[tf])
                building = "building" if self._building[tf] else "waiting"
                lines.append(f"  {tf}m: {n} candles ({building})")
            return "\n".join(lines)
