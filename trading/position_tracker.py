"""
position_tracker.py — Open position monitoring via Bybit API.

Tracks currently open positions for the configured trading symbol.
"""

from pybit.unified_trading import HTTP
from utils.logger import log
import config


def has_open_position(session: HTTP) -> bool:
    """
    Check if there is an open position for our symbol.

    Args:
        session: Authenticated Bybit session.

    Returns:
        bool: True if a position exists for TRADING_SYMBOL.
    """
    try:
        result = session.get_positions(
            category=config.TRADING_CATEGORY,
            symbol=config.TRADING_SYMBOL,
        )

        positions = result["result"]["list"]
        open_positions = [
            p for p in positions
            if float(p.get("size", 0)) > 0
        ]

        if open_positions:
            pos = open_positions[0]
            log.info(
                f"📍 Open position: {pos['side']} {config.TRADING_SYMBOL} | "
                f"Size: {pos['size']} | Entry: ${float(pos['avgPrice']):,.2f} | "
                f"P&L: ${float(pos.get('unrealisedPnl', 0)):,.2f}"
            )
            return True

        return False

    except Exception as e:
        log.error(f"Failed to check positions: {e}")
        # Default to True (assume position exists) for safety
        return True
