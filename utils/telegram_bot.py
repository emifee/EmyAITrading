"""
telegram_bot.py — Interactive Telegram bot for Emy AI Trading System.

Controls and monitors the trading system from your phone with cTrader.
Uses thread-safe asyncio loop to work alongside Twisted reactor.

Commands:
  /start      — Welcome message & command list
  /status     — System status (running/paused, last cycle)
  /balance    — Account balance & equity
  /positions  — Open position details with P&L
  /analyze    — Force Claude analysis (analysis only)
  /trade      — Force Claude to analyze AND execute
  /close      — Close all open positions
  /stats      — Trading performance stats
  /journal    — Last 10 trades
  /indicators — Latest indicator values
  /pause      — Pause auto-trading
  /resume     — Resume auto-trading
  /help       — Show all commands
"""

import asyncio
import threading
import time
from datetime import datetime, timezone
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from utils.logger import log
import config


# ─── Bot State ────────────────────────────────────────────────
class BotState:
    """Shared state between the trading system and Telegram bot."""

    def __init__(self):
        self.is_paused = False
        self.in_dead_zone = False
        self.last_cycle_time = None
        self.last_decision = {}  # symbol -> decision
        self.last_indicators = {}  # symbol -> indicator
        self.cycle_count = 0
        self.trades_today = 0
        self.start_time = datetime.now(timezone.utc)
        # Callback functions — set by main.py to trigger actions
        self.on_analyze = None      # Callable to run analysis cycle
        self.on_force_trade = None  # Callable to force a trade
        self.on_close_all = None    # Callable to close all positions
        self.on_get_positions = None  # Callable to get positions
        self.on_get_balance = None    # Callable to get balance

    def update_cycle(self, symbol="XAUUSD", decision=None, indicators=None):
        self.last_cycle_time = datetime.now(timezone.utc)
        self.cycle_count += 1
        if decision:
            self.last_decision[symbol] = decision
        if indicators:
            self.last_indicators[symbol] = indicators


# Global bot state
bot_state = BotState()

# Store the application for sending messages from outside
_app = None
_bot_loop = None


def _is_authorized(update: Update) -> bool:
    """Check if the message is from the authorized chat."""
    chat_id = str(update.effective_chat.id)
    authorized = config.TELEGRAM_CHAT_ID
    if not authorized:
        return True
    return chat_id == authorized


# ─── Command Handlers ────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message."""
    if not _is_authorized(update):
        return

    uptime = datetime.now(timezone.utc) - bot_state.start_time
    hours = int(uptime.total_seconds() // 3600)
    mins = int((uptime.total_seconds() % 3600) // 60)

    msg = (
        "🤖 *Emy AI Trading System*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "XAUUSD Trend + Liquidity Sweep Strategy\n"
        f"Powered by {config.CLAUDE_MODEL}\n\n"
        "📋 *Commands:*\n"
        "/status — System health & uptime\n"
        "/balance — Account balance\n"
        "/positions — Open positions with P&L\n"
        "/analyze — Force Claude analysis\n"
        "/trade — Force Claude trade\n"
        "/close — Close all positions\n"
        "/stats — Win rate & P&L stats\n"
        "/journal — Last 10 trades\n"
        "/indicators — Technical indicators\n"
        "/pause — Pause auto-trading\n"
        "/resume — Resume trading\n"
        "/reset_risk — Reset streak and circuit breaker\n\n"
        f"📊 Symbols: `{', '.join(config.TRADING_SYMBOLS)}`\n"
        f"⏱️ Analysis: every `{config.ANALYSIS_INTERVAL_MINUTES}` min\n"
        f"🧪 Mode: `cTrader DEMO`\n"
        f"⏰ Uptime: `{hours}h {mins}m`\n\n"
        f"Chat ID: `{update.effective_chat.id}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show system status."""
    if not _is_authorized(update):
        return

    status_emoji = "⏸️ PAUSED" if bot_state.is_paused else "🟢 RUNNING"
    uptime = datetime.now(timezone.utc) - bot_state.start_time
    hours = int(uptime.total_seconds() // 3600)
    mins = int((uptime.total_seconds() % 3600) // 60)

    last_cycle = (
        bot_state.last_cycle_time.strftime("%H:%M:%S UTC")
        if bot_state.last_cycle_time else "Not yet"
    )

    last_action_str = ""
    if bot_state.last_decision:
        for sym, d in bot_state.last_decision.items():
            act = d.get("action", "N/A")
            conf = d.get("confidence", "N/A")
            rsn = d.get("reason", "N/A")[:100]
            last_action_str += f"*{sym}*: `{act}` ({conf}%)\n_{rsn}_\n\n"
    else:
        last_action_str = "N/A\n"

    # Cost stats
    try:
        from ai.claude_client import get_usage_stats
        usage = get_usage_stats()
        cost_str = f"${usage['estimated_cost']:.4f} ({usage['total_calls']} calls)"
    except Exception:
        cost_str = "N/A"

    msg = (
        f"📊 *System Status*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Status: {status_emoji}\n"
        f"Uptime: `{hours}h {mins}m`\n"
        f"Cycles: `{bot_state.cycle_count}`\n"
        f"Trades today: `{bot_state.trades_today}`\n"
        f"Last analysis: `{last_cycle}`\n"
        f"AI cost: `{cost_str}`\n\n"
        f"*Last Decisions:*\n"
        f"{last_action_str}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show account balance."""
    if not _is_authorized(update):
        return

    if not bot_state.on_get_balance:
        await update.message.reply_text("❌ Not connected to cTrader yet")
        return

    try:
        result = bot_state.on_get_balance()
        if asyncio.isfuture(result) or hasattr(result, 'addCallback'):
            await update.message.reply_text("⏳ Fetching balance...")
            return

        msg = (
            f"🏦 *Account Balance*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance: `${result.get('balance', 0):,.2f}`\n"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show open positions with live P&L."""
    if not _is_authorized(update):
        return

    if not bot_state.on_get_positions:
        await update.message.reply_text("❌ Not connected to cTrader yet")
        return

    try:
        positions = bot_state.on_get_positions()

        if not positions:
            await update.message.reply_text("📭 No open positions")
            return

        msg = f"📍 *Open Positions*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        for p in positions:
            side = p.get("side", "?")
            emoji = "🟢" if side == "BUY" else "🔴"
            entry = p.get("entryPrice", 0)
            sl = p.get("stopLoss", 0)
            tp = p.get("takeProfit", 0)
            vol = p.get("volume", 0)
            upnl = p.get("unrealizedPnl", 0)
            pnl_emoji = "📈" if upnl >= 0 else "📉"

            msg += (
                f"\n{emoji} *{side}* `{vol}` lots\n"
                f"📍 Entry: `${entry:,.2f}`\n"
                f"🛑 SL: `${sl:,.2f}` | 🎯 TP: `${tp:,.2f}`\n"
                f"{pnl_emoji} P&L: `${upnl:,.2f}`\n"
            )

        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force Claude to analyze market (no execution)."""
    if not _is_authorized(update):
        return

    if not bot_state.on_analyze:
        await update.message.reply_text("❌ Analysis not available — system not fully initialized")
        return

    await update.message.reply_text(f"🧠 Running Claude analysis ({config.CLAUDE_MODEL})...\nThis takes a few seconds.")

    try:
        # Run analysis in Twisted reactor thread
        from twisted.internet import reactor
        reactor.callFromThread(bot_state.on_analyze)

        await update.message.reply_text(
            "✅ Analysis triggered! Check back with /status for results.\n"
            "_The analysis runs in the background via the Twisted reactor._",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force Claude to analyze AND execute trade."""
    if not _is_authorized(update):
        return

    if not bot_state.on_force_trade:
        await update.message.reply_text("❌ Trading not available — system not fully initialized")
        return

    await update.message.reply_text(
        "🚀 *Forcing Claude trade...*\n"
        "Claude will analyze the market and execute if conditions are right.",
        parse_mode="Markdown"
    )

    try:
        from twisted.internet import reactor
        reactor.callFromThread(bot_state.on_force_trade)

        await update.message.reply_text(
            "✅ Trade command sent! Check /positions for results.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Close all open positions."""
    if not _is_authorized(update):
        return

    if not bot_state.on_close_all:
        await update.message.reply_text("❌ Not connected to cTrader yet")
        return

    await update.message.reply_text("🚨 Closing all positions...")

    try:
        from twisted.internet import reactor
        reactor.callFromThread(bot_state.on_close_all)

        await update.message.reply_text(
            "✅ Close command sent! Check /positions to confirm.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show trading performance stats."""
    if not _is_authorized(update):
        return

    try:
        from data.trade_journal import get_stats
        from ai.claude_client import get_usage_stats
        s = get_stats()
        usage = get_usage_stats()

        if s["total_trades"] == 0:
            await update.message.reply_text(
                "📭 No closed trades yet.\nThe journal will populate as trades close."
            )
            return

        pnl_emoji = "📈" if s["total_pnl"] >= 0 else "📉"
        today_emoji = "📈" if s["today_pnl"] >= 0 else "📉"

        msg = (
            f"📊 *Trading Statistics*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"*Overall:*\n"
            f"Total trades: `{s['total_trades']}`\n"
            f"✅ Wins: `{s['wins']}` | ❌ Losses: `{s['losses']}`\n"
            f"Win rate: `{s['win_rate']}%`\n"
            f"{pnl_emoji} Total P&L: `${s['total_pnl']:,.2f}`\n"
            f"Avg P&L: `${s['avg_pnl']:,.2f}`\n"
            f"Avg R:R: `{s['avg_rr']}:1`\n"
            f"Best: `${s['best_trade']:,.2f}` | Worst: `${s['worst_trade']:,.2f}`\n\n"
            f"*Today:*\n"
            f"{today_emoji} P&L: `${s['today_pnl']:,.2f}` ({s['today_trades']} trades)\n\n"
            f"*AI Cost:*\n"
            f"Calls: `{usage['total_calls']}` | Cost: `${usage['estimated_cost']:.4f}`\n"
            f"Open positions: `{s['open_trades']}`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show time-based performance reports."""
    if not _is_authorized(update):
        return

    try:
        from data.trade_journal import get_time_reports
        reports = get_time_reports()

        msg = (
            f"📅 *Performance Reports*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"*Today:*\n"
            f"Trades: `{reports['today']['trades']}` | WR: `{reports['today']['win_rate']}%`\n"
            f"P&L: `${reports['today']['pnl']:,.2f}`\n\n"
            f"*This Week:*\n"
            f"Trades: `{reports['week']['trades']}` | WR: `{reports['week']['win_rate']}%`\n"
            f"P&L: `${reports['week']['pnl']:,.2f}`\n\n"
            f"*This Month:*\n"
            f"Trades: `{reports['month']['trades']}` | WR: `{reports['month']['win_rate']}%`\n"
            f"P&L: `${reports['month']['pnl']:,.2f}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show last 10 trades."""
    if not _is_authorized(update):
        return

    try:
        from data.trade_journal import get_recent_trades
        trades = get_recent_trades(10)

        if not trades:
            await update.message.reply_text("📭 No closed trades in journal yet")
            return

        msg = "📓 *Trade Journal — Last 10*\n━━━━━━━━━━━━━━━━━━━━━━\n"
        for t in trades:
            pnl = t["pnl_dollars"]
            emoji = "🟢" if pnl > 0 else "🔴"
            side = t["side"]
            closed = t.get("closed_at", "")[:16] if t.get("closed_at") else "?"
            reason = t.get("exit_reason", "?")

            msg += (
                f"\n{emoji} {side} @ `${t['entry_price']:,.2f}`"
                f" → `${t.get('exit_price', 0):,.2f}`\n"
                f"   P&L: `${pnl:,.2f}` | R:R: `{t.get('risk_reward', 0)}` | {reason}\n"
            )

        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def cmd_indicators(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show latest indicator values."""
    if not _is_authorized(update):
        return

    inds = bot_state.last_indicators
    if not inds:
        await update.message.reply_text("📭 No indicator data yet — waiting for first analysis cycle")
        return

    msg = "📐 *Latest Indicators*\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for sym, ind in inds.items():
        sweep_str = "None"
        if ind.get("sweep_detected"):
            sweep_str = f"🚨 {ind.get('sweep_type', 'Unknown')} @ ${ind.get('sweep_level', 0):,.2f}"

        msg += (
            f"*{sym}*\n"
            f"💲 Price: `${ind.get('current_price', 0):,.2f}` | 📈 Trend: `{ind.get('trend', 'N/A')}`\n"
            f"🏗 Structure: `{ind.get('structure', 'N/A')}`\n"
            f"*EMAs*: 50=`${ind.get('ema50', 0):,.2f}` | 200=`${ind.get('ema200', 0):,.2f}`\n"
            f"*Sweep*: {sweep_str}\n"
            f"*Vol*: ATR=`${ind.get('atr', 0):,.2f}` | RSI=`{ind.get('rsi', 'N/A')}` | VolRatio=`{ind.get('volume_ratio', 0)}x`\n\n"
        )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause the trading system."""
    if not _is_authorized(update):
        return
    bot_state.is_paused = True
    log.warning("Trading PAUSED via Telegram")
    await update.message.reply_text(
        "⏸️ *Trading Paused*\n\nAuto-trading will skip until you `/resume`.\n"
        "You can still use `/analyze` and `/trade` manually.",
        parse_mode="Markdown"
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume the trading system."""
    if not _is_authorized(update):
        return
    bot_state.is_paused = False
    log.info("Trading RESUMED via Telegram")
    await update.message.reply_text(
        "▶️ *Trading Resumed*\n\nAuto-trading will execute on the next cycle.",
        parse_mode="Markdown"
    )

async def cmd_reset_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually reset the risk streak and circuit breaker."""
    if not _is_authorized(update):
        return
    try:
        from trading.risk_manager import risk_state
        risk_state.reset()
        await update.message.reply_text(
            "🔄 *Risk Reset*\n\nThe losing streak has been cleared and the circuit breaker (if active) is disarmed. Risk size is back to 100%.",
            parse_mode="Markdown"
        )
    except Exception as e:
        log.error(f"Failed to reset risk: {e}")
        await update.message.reply_text("❌ Failed to reset risk.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle natural language messages — talk to the bot like a person."""
    if not _is_authorized(update):
        return

    text = update.message.text.lower().strip()

    # ─── Greetings (only if no action words in the message) ──
    greetings = ["hi", "hello", "hey", "yo", "sup", "good morning", "good evening", "gm", "wassup"]
    action_words = ["trade", "buy", "sell", "close", "balance", "position", "status", "analyze",
                    "market", "price", "stats", "journal", "pause", "resume", "signal", "opportunity",
                    "going on", "happening", "open", "place", "execute", "profit", "loss", "money"]
    is_greeting = any(text == g or text.startswith(g + " ") or text.startswith(g + "!") for g in greetings)
    has_action = any(w in text for w in action_words)

    if is_greeting and not has_action:
        await update.message.reply_text(
            "👋 Hey! I'm Emy AI Trading Bot.\n\n"
            "Just talk to me like normal:\n"
            "• _\"What's going on with the trades?\"_\n"
            "• _\"Any opportunities right now?\"_\n"
            "• _\"Show my balance\"_\n"
            "• _\"Place a trade\"_\n"
            "• _\"Close everything\"_",
            parse_mode="Markdown"
        )
        return

    # ─── "What's going on" / Overview / Update me ─────────
    # These are general "give me an update" questions
    overview_phrases = [
        "what's going on", "whats going on", "what is going on",
        "what's happening", "whats happening", "what is happening",
        "update me", "give me an update", "any update",
        "how are things", "how's it going", "hows it going",
        "what's new", "whats new", "anything new",
        "summary", "overview",
        "what's the situation", "whats the situation",
    ]
    if any(p in text for p in overview_phrases):
        # Give a combined overview: status + positions
        await _send_overview(update, context)
        return

    # ─── Close (check BEFORE trade to avoid "close trade" → analyze) ─
    close_phrases = ["close", "exit", "get out", "shut", "kill", "cut loss", "cut it"]
    if any(w in text for w in close_phrases):
        await cmd_close(update, context)
        return

    # ─── Pause / Resume (check before generic trade words) ────
    if any(w in text for w in ["pause", "stop trading", "take a break", "hold off", "stop the bot"]):
        await cmd_pause(update, context)
        return
    if any(w in text for w in ["resume", "start trading", "go again", "unpause", "turn on"]):
        await cmd_resume(update, context)
        return

    # ─── Force trade execution ────────────────────────────
    execute_phrases = [
        "place a trade", "execute a trade", "force a trade", "make a trade",
        "place trade", "execute trade", "force trade",
        "buy now", "sell now", "enter now", "trade now",
        "do it", "let's go", "pull the trigger",
        "open a position", "open position",
    ]
    if any(p in text for p in execute_phrases):
        await cmd_trade(update, context)
        return

    # ─── Ask about trades / opportunities / signals ───────
    opportunity_words = ["opportunity", "signal", "setup", "available", "any trade", "find a trade",
                         "should i trade", "can we trade", "is there a trade", "good time to trade"]
    if any(w in text for w in opportunity_words):
        await cmd_analyze(update, context)
        return

    # ─── Positions / My trades ────────────────────────────
    position_phrases = [
        "position", "open trade", "my trade", "open order",
        "what do i have", "active trade", "current trade",
        "am i in a trade", "do i have a trade", "any open",
        "what's open", "whats open",
    ]
    if any(p in text for p in position_phrases):
        await cmd_positions(update, context)
        return

    # ─── Market / Price / Analysis ────────────────────────
    market_words = ["market", "price", "analysis", "analyze", "gold",
                    "xauusd", "chart", "trend", "ema", "indicator",
                    "what's the price", "how's the market", "how is the market",
                    "show me the market", "technical"]
    if any(w in text for w in market_words):
        if bot_state.last_indicators:
            await cmd_indicators(update, context)
        else:
            await cmd_analyze(update, context)
        return

    # ─── Balance / Money / Account ────────────────────────
    money_words = ["balance", "money", "account", "equity", "funds", "capital",
                   "how much do i have", "my money", "wallet"]
    if any(w in text for w in money_words):
        await cmd_balance(update, context)
        return

    # ─── Stats / Performance / P&L ────────────────────────
    stats_words = ["stats", "statistics", "performance", "win rate", "profit", "loss",
                   "p&l", "pnl", "how am i doing", "how did i do", "results",
                   "track record", "how much did i make", "am i profitable",
                   "am i winning", "am i losing", "score"]
    if any(w in text for w in stats_words):
        await cmd_stats(update, context)
        return

    # ─── Reports / Daily / Weekly ─────────────────────────
    reports_words = ["report", "daily", "weekly", "monthly", "today",
                     "this week", "this month", "how are we today",
                     "are we bleeding", "drawdown", "how is today"]
    if any(w in text for w in reports_words):
        await cmd_reports(update, context)
        return

    # ─── Journal / History ────────────────────────────────
    journal_words = ["journal", "history", "past trade", "last trade", "previous",
                     "recent trade", "trade log", "what happened", "show me trades",
                     "trade history"]
    if any(w in text for w in journal_words):
        await cmd_journal(update, context)
        return

    # ─── Status / System ──────────────────────────────────
    status_words = ["status", "system", "alive", "uptime", "health",
                    "are you running", "you alive", "you there", "you on"]
    if any(w in text for w in status_words):
        await cmd_status(update, context)
        return

    # ─── Generic trade word (after all specific checks) ───
    if any(w in text for w in ["trade", "buy", "sell", "entry"]):
        await cmd_analyze(update, context)
        return

    # ─── Thanks / Positive ────────────────────────────────
    if any(w in text for w in ["thanks", "thank you", "nice", "great", "awesome", "good job",
                                "perfect", "cool", "love it", "amazing"]):
        await update.message.reply_text("🤖💪 Glad to help! I'm always watching the market for you.")
        return

    # ─── Negative / Frustration ───────────────────────────
    if any(w in text for w in ["bad", "terrible", "losing", "wtf", "why"]):
        await update.message.reply_text(
            "😤 I hear you. Let me check what's going on..."
        )
        await _send_overview(update, context)
        return

    # ─── Fallback — friendly ──────────────────────────────
    await update.message.reply_text(
        "🤖 I got you! Try asking me:\n\n"
        "📊 _\"What's going on with the trades?\"_\n"
        "💰 _\"How's my account?\"_\n"
        "📍 _\"Any open positions?\"_\n"
        "🚀 _\"Place a trade for me\"_\n"
        "📈 _\"How am I doing?\"_\n"
        "🔒 _\"Close everything\"_",
        parse_mode="Markdown"
    )


async def _send_overview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a combined overview — status + positions + latest info."""
    uptime = datetime.now(timezone.utc) - bot_state.start_time
    hours = int(uptime.total_seconds() // 3600)
    mins = int((uptime.total_seconds() % 3600) // 60)

    status = "⏸️ Paused" if bot_state.is_paused else "🟢 Running"

    # Last decision
    last_action_str = ""
    if bot_state.last_decision:
        for sym, d in bot_state.last_decision.items():
            action_emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⏸️"}.get(d.get("action", ""), "❓")
            act = f"{action_emoji} {d.get('action', '?')} ({d.get('confidence', '?')}%)"
            rsn = d.get("reason", "")[:150]
            last_action_str += f"*{sym}*: {act}\n_{rsn}_\n\n"
    else:
        last_action_str = "None yet\n"

    msg = (
        f"📊 *Here's what's going on:*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"System: {status} | Uptime: `{hours}h {mins}m`\n"
        f"Cycles: `{bot_state.cycle_count}` | Trades today: `{bot_state.trades_today}`\n\n"
        f"*Last Claude decisions:*\n{last_action_str}"
    )

    # Positions
    positions = getattr(bot_state, '_cached_positions', [])
    if positions:
        msg += f"\n*Open positions:*\n"
        for p in positions:
            side = p.get("side", "?")
            emoji = "🟢" if side == "BUY" else "🔴"
            upnl = p.get("unrealizedPnl", 0)
            pnl_emoji = "📈" if upnl >= 0 else "📉"
            msg += (
                f"{emoji} {side} @ `${p.get('entryPrice', 0):,.2f}` | "
                f"{pnl_emoji} `${upnl:,.2f}`\n"
            )
    else:
        msg += "\n📭 No open positions right now\n"

    # Journal stats
    try:
        from data.trade_journal import get_stats
        s = get_stats()
        if s["total_trades"] > 0:
            pnl_emoji = "📈" if s["total_pnl"] >= 0 else "📉"
            msg += (
                f"\n*Track record:*\n"
                f"Trades: `{s['total_trades']}` | Win rate: `{s['win_rate']}%`\n"
                f"{pnl_emoji} Total P&L: `${s['total_pnl']:,.2f}`\n"
            )
    except Exception:
        pass

    await update.message.reply_text(msg, parse_mode="Markdown")


# ─── Bot Setup & Lifecycle ───────────────────────────────────

async def _post_init(application: Application):
    """Set up bot commands menu after initialization."""
    commands = [
        BotCommand("status", "📊 System status & uptime"),
        BotCommand("balance", "🏦 Account balance"),
        BotCommand("positions", "📍 Open positions & P&L"),
        BotCommand("analyze", "🧠 Force Claude analysis"),
        BotCommand("trade", "🚀 Force Claude trade"),
        BotCommand("close", "🚨 Close all positions"),
        BotCommand("stats", "📊 Win rate & P&L stats"),
        BotCommand("reports", "📅 Daily/Weekly/Monthly reports"),
        BotCommand("journal", "📓 Last 10 trades"),
        BotCommand("indicators", "📐 Technical indicators"),
        BotCommand("pause", "⏸️ Pause auto-trading"),
        BotCommand("resume", "▶️ Resume trading"),
        BotCommand("help", "❓ Show all commands"),
    ]
    await application.bot.set_my_commands(commands)
    log.info("Telegram bot commands menu set")


def start_telegram_bot():
    """
    Start the Telegram bot in a background thread.

    Uses manual asyncio loop management to avoid the
    set_wakeup_fd threading issue with run_polling().
    """
    global _app, _bot_loop

    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        log.info("Telegram bot not configured — skipping")
        return None

    # Prevent multiple threads on reconnection
    if getattr(bot_state, '_is_running', False):
        log.info("Telegram bot is already running — skipping duplicate start")
        return None
    bot_state._is_running = True

    def run_bot():
        global _app, _bot_loop

        _bot_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_bot_loop)

        def _build_app():
            app = Application.builder().token(token).post_init(_post_init).build()

            # Register all command handlers
            app.add_handler(CommandHandler("start", cmd_start))
            app.add_handler(CommandHandler("help", cmd_help))
            app.add_handler(CommandHandler("status", cmd_status))
            app.add_handler(CommandHandler("balance", cmd_balance))
            app.add_handler(CommandHandler("positions", cmd_positions))
            app.add_handler(CommandHandler("analyze", cmd_analyze))
            app.add_handler(CommandHandler("trade", cmd_trade))
            app.add_handler(CommandHandler("close", cmd_close))
            app.add_handler(CommandHandler("stats", cmd_stats))
            app.add_handler(CommandHandler("reports", cmd_reports))
            app.add_handler(CommandHandler("journal", cmd_journal))
            app.add_handler(CommandHandler("indicators", cmd_indicators))
            app.add_handler(CommandHandler("pause", cmd_pause))
            app.add_handler(CommandHandler("resume", cmd_resume))
            app.add_handler(CommandHandler("reset_risk", cmd_reset_risk))
            # Legacy aliases
            app.add_handler(CommandHandler("position", cmd_positions))
            app.add_handler(CommandHandler("decide", cmd_analyze))

            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            return app

        log.info("🤖 Telegram bot started — listening for commands")

        # ─── Thread-safe polling (fixes set_wakeup_fd crash) ──
        # Instead of run_polling() which calls signal handlers,
        # we manually initialize, start polling, and run the loop.
        async def _run():
            global _app
            _app = _build_app()
            await _app.initialize()
            # Clear any stale Telegram connections before polling
            try:
                await _app.bot.delete_webhook(drop_pending_updates=True)
                await asyncio.sleep(2)  # Wait for Telegram to release the old connection
            except Exception as e:
                log.warning(f"Webhook cleanup warning: {e}")
            await _app.start()
            await _app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,  # Prevents conflict on restart
            )
            log.debug("🤖 Telegram polling active")

            # Keep running until stopped
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            finally:
                await _app.updater.stop()
                await _app.stop()
                await _app.shutdown()

        while True:
            try:
                _bot_loop.run_until_complete(_run())
            except Exception as e:
                log.error(f"Telegram bot error: {type(e).__name__}: {e}. Retrying in 10 seconds...")
                import time
                time.sleep(10)

    bot_thread = threading.Thread(target=run_bot, daemon=True, name="telegram-bot")
    bot_thread.start()

    log.info("Telegram bot thread started")
    return bot_thread


def _sync_fallback_send(message: str):
    """Synchronous fallback to ensure critical messages like shutdown always send."""
    try:
        import requests
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }
        requests.post(url, json=payload, timeout=3)
    except Exception as e:
        pass  # Just drop it silently if internet is completely dead

def send_bot_message(message: str):
    """
    Send a message from the trading system to the Telegram chat.
    Thread-safe — can be called from Twisted reactor thread.
    """
    if not _app or not _bot_loop or not config.TELEGRAM_CHAT_ID:
        return

    async def _send():
        try:
            await _app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="Markdown",
            )
        except Exception as e:
            # Fallback for shutdown / closed loops
            _sync_fallback_send(message)

    try:
        if _bot_loop.is_closed():
            _sync_fallback_send(message)
        else:
            asyncio.run_coroutine_threadsafe(_send(), _bot_loop)
    except Exception as e:
        _sync_fallback_send(message)
