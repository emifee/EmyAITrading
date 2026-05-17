"""
test_telegram_live.py — Run the Telegram bot interactively (no trading).

Use this to test all bot commands (/start, /help, /status, /pause, /resume)
without needing Bybit or Anthropic API keys.
"""

import sys
import os
import asyncio
import signal

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

import config
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *Emy AI Trading System*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "I'm your XAUUSD trading assistant powered by Claude AI.\n\n"
        "📋 *Commands:*\n"
        "/status — System status\n"
        "/balance — Account balance\n"
        "/position — Open positions\n"
        "/trades — Today's trades\n"
        "/indicators — Latest indicators\n"
        "/decide — Force market analysis\n"
        "/pause — Pause trading\n"
        "/resume — Resume trading\n"
        "/close — Emergency close position\n"
        "/help — Show this message\n\n"
        f"📊 Symbol: `{config.TRADING_SYMBOL}`\n"
        f"⏱️ Interval: `{config.TRADING_TIMEFRAME}m`\n"
        f"🧪 Mode: `{'TESTNET' if config.BYBIT_TESTNET else '🔴 MAINNET'}`\n\n"
        "⚠️ _Running in TEST MODE — no trading active_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📊 *System Status*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Status: 🧪 TEST MODE\n"
        "Cycles: `0`\n"
        "Trades today: `0`\n\n"
        "⚠️ _Full trading not active — run `python main.py` with all API keys_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⏸️ *Trading Paused*\n\nThe bot will skip all cycles until you `/resume`.",
        parse_mode="Markdown"
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "▶️ *Trading Resumed*\n\nReady for next cycle.",
        parse_mode="Markdown"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower().strip()
    responses = {
        "hi": "👋 Hey! I'm Emy AI Trading Bot. Use /help to see commands.",
        "hello": "👋 Hello! Use /help to see what I can do.",
        "how are you": "🤖 Running smoothly! Use /status for system details.",
    }
    if text in responses:
        await update.message.reply_text(responses[text])
    else:
        await update.message.reply_text(
            "🤖 I didn't understand that. Use /help to see available commands."
        )


async def post_init(app: Application):
    commands = [
        BotCommand("start", "🤖 Welcome & commands"),
        BotCommand("status", "📊 System status"),
        BotCommand("pause", "⏸️ Pause trading"),
        BotCommand("resume", "▶️ Resume trading"),
        BotCommand("help", "❓ Show commands"),
    ]
    await app.bot.set_my_commands(commands)

    # Send startup message
    if config.TELEGRAM_CHAT_ID:
        await app.bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                "🤖 *Emy AI Bot is ONLINE*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ Ready to receive commands\n"
                "Type /start to begin!"
            ),
            parse_mode="Markdown",
        )

    print("✅ Bot is running! Send commands in Telegram.")
    print("Press Ctrl+C to stop.\n")


def main():
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN not set in .env")
        return

    print("🤖 Starting Emy AI Telegram Bot (TEST MODE)...")
    print(f"   Token: {token[:10]}...{token[-5:]}")
    print(f"   Chat ID: {config.TELEGRAM_CHAT_ID}\n")

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
