"""
notifier.py — Optional Telegram alert system.

Sends real-time trade notifications to your phone.
Gracefully skips if Telegram is not configured.
"""

import os
import requests
from utils.logger import log
import config


def send_alert(message: str) -> bool:
    """
    Send a Telegram notification.

    Args:
        message: The alert text to send.

    Returns:
        True if sent successfully, False otherwise.
    """
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        log.debug("Telegram not configured — skipping alert")
        return False

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        response = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        response.raise_for_status()
        log.debug(f"Telegram alert sent: {message[:50]}...")
        return True

    except requests.RequestException as e:
        log.warning(f"Telegram alert failed: {e}")
        return False


def send_trade_alert(action: str, symbol: str, qty: float,
                     entry: float, sl: float, tp: float, reason: str):
    """Send a formatted trade execution alert."""
    emoji = "🟢" if action == "BUY" else "🔴" if action == "SELL" else "⏸️"

    message = (
        f"{emoji} *{action} {symbol}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📍 Entry: `${entry:,.2f}`\n"
        f"🛑 Stop Loss: `${sl:,.2f}`\n"
        f"🎯 Take Profit: `${tp:,.2f}`\n"
        f"📦 Qty: `{qty}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💬 _{reason}_"
    )
    return send_alert(message)


def send_error_alert(error_msg: str):
    """Send an error notification."""
    message = f"🚨 *TRADING ERROR*\n\n`{error_msg}`"
    return send_alert(message)


def send_daily_summary(trades: int, pnl: float, win_rate: float, balance: float):
    """Send end-of-day performance summary."""
    emoji = "📈" if pnl >= 0 else "📉"
    message = (
        f"{emoji} *Daily Summary*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 Trades: `{trades}`\n"
        f"💰 P&L: `${pnl:,.2f}`\n"
        f"🎯 Win Rate: `{win_rate:.1f}%`\n"
        f"🏦 Balance: `${balance:,.2f}`\n"
    )
    return send_alert(message)
