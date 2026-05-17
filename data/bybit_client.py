"""
bybit_client.py — Bybit API connection and account operations.

Handles session creation, account info retrieval, and health checks.
"""

from pybit.unified_trading import HTTP
from utils.logger import log
import config


def get_bybit_session() -> HTTP:
    """
    Create an authenticated Bybit HTTP session.

    Uses testnet or mainnet based on config.BYBIT_TESTNET.

    Returns:
        HTTP: Authenticated pybit session.
    """
    session = HTTP(
        testnet=config.BYBIT_TESTNET,
        api_key=config.BYBIT_API_KEY,
        api_secret=config.BYBIT_API_SECRET,
    )

    mode = "TESTNET" if config.BYBIT_TESTNET else "MAINNET"
    log.info(f"Bybit session created ({mode})")

    return session


def get_account_info(session: HTTP) -> dict:
    """
    Get account balance and position summary.

    Returns:
        dict: {
            'balance': float,       # Available USDT balance
            'equity': float,        # Total equity
            'positions': list,      # Open positions summary
        }
    """
    try:
        # Get wallet balance
        wallet = session.get_wallet_balance(
            accountType="UNIFIED",
            coin="USDT",
        )

        coins = wallet["result"]["list"][0]["coin"]
        usdt_info = next((c for c in coins if c["coin"] == "USDT"), None)

        balance = float(usdt_info["availableToWithdraw"]) if usdt_info and usdt_info["availableToWithdraw"] else 0.0
        equity = float(usdt_info["equity"]) if usdt_info and usdt_info["equity"] else 0.0

        # Get open positions
        positions = session.get_positions(
            category=config.TRADING_CATEGORY,
            symbol=config.TRADING_SYMBOL,
        )

        open_positions = [
            p for p in positions["result"]["list"]
            if float(p.get("size", 0)) > 0
        ]

        account = {
            "balance": balance,
            "equity": equity,
            "positions": open_positions,
        }

        log.debug(f"Account info: balance=${balance:,.2f}, equity=${equity:,.2f}, "
                   f"positions={len(open_positions)}")

        return account

    except Exception as e:
        log.error(f"Failed to get account info: {e}")
        raise


def get_server_time(session: HTTP) -> dict:
    """
    Health check — get Bybit server time.

    Returns:
        dict: Server time response.
    """
    try:
        result = session.get_server_time()
        log.debug(f"Bybit server time: {result['result']['timeSecond']}")
        return result
    except Exception as e:
        log.error(f"Bybit connection health check failed: {e}")
        raise
