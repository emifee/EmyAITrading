"""
order_executor.py — Trade execution via Bybit API.

Places market orders with stop loss and take profit.
"""

from pybit.unified_trading import HTTP
from utils.logger import log
from utils.notifier import send_alert
import config


def execute_trade(session: HTTP, decision: dict, qty: float) -> dict:
    """
    Execute a trade based on Claude's validated decision.

    Args:
        session: Authenticated Bybit session.
        decision: Validated decision dict from Claude.
        qty: Position size from risk manager.

    Returns:
        dict: Order result or None.
    """
    try:
        symbol = config.TRADING_SYMBOL
        side = decision["action"]  # "BUY" or "SELL"
        sl = str(decision["stop_loss"])
        tp = str(decision["take_profit"])

        # Map BUY/SELL to Bybit's Buy/Sell
        bybit_side = "Buy" if side == "BUY" else "Sell"

        log.info(
            f"🚀 Executing {side} {symbol} | "
            f"Qty: {qty} | SL: ${decision['stop_loss']:,.2f} | TP: ${decision['take_profit']:,.2f}"
        )

        result = session.place_order(
            category=config.TRADING_CATEGORY,
            symbol=symbol,
            side=bybit_side,
            orderType="Market",
            qty=str(qty),
            stopLoss=sl,
            takeProfit=tp,
            timeInForce="GTC",
        )

        if result["retCode"] == 0:
            order_id = result["result"]["orderId"]
            log.info(f"✅ Order placed: {side} {symbol} x{qty} | ID: {order_id}")

            send_alert(
                f"🚀 *Trade Executed*\n"
                f"Symbol: {symbol}\n"
                f"Side: {side}\n"
                f"Qty: {qty}\n"
                f"SL: ${decision['stop_loss']:,.2f}\n"
                f"TP: ${decision['take_profit']:,.2f}\n"
                f"Confidence: {decision.get('confidence', 'N/A')}%\n"
                f"Order ID: {order_id}"
            )

            return result["result"]
        else:
            log.error(f"Order failed: {result['retMsg']}")
            send_alert(f"❌ Order failed: {result['retMsg']}")
            return None

    except Exception as e:
        log.error(f"❌ Order execution failed: {e}")
        send_alert(f"❌ Order failed: {e}")
        return None
