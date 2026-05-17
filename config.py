"""
config.py — Centralized configuration for Emy AI Trading System.

Supports both Bybit and cTrader brokers via BROKER env var.
Three-tier architecture: real-time ticks → 2-hour Claude → 15-min monitor.
"""

import os
import sys
from dotenv import load_dotenv

# Load .env before anything else
load_dotenv()

# ─── Broker Selection ─────────────────────────────────────────
BROKER = os.getenv("BROKER", "bybit")  # "bybit" or "ctrader"

# ─── Bybit Configuration ─────────────────────────────────────
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

# ─── cTrader Configuration ───────────────────────────────────
CTRADER_CLIENT_ID = os.getenv("CTRADER_CLIENT_ID", "")
CTRADER_CLIENT_SECRET = os.getenv("CTRADER_CLIENT_SECRET", "")
CTRADER_ACCESS_TOKEN = os.getenv("CTRADER_ACCESS_TOKEN", "")
CTRADER_ACCOUNT_ID = os.getenv("CTRADER_ACCOUNT_ID", "")
CTRADER_HOST = os.getenv("CTRADER_HOST", "demo")  # "demo" or "live"

# ─── AI Configuration ─────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
ENABLE_PROMPT_CACHING = os.getenv("ENABLE_PROMPT_CACHING", "true").lower() == "true"

# ─── Scheduling ───────────────────────────────────────────────
ANALYSIS_INTERVAL_MINUTES = int(os.getenv("ANALYSIS_INTERVAL_MINUTES", "30"))  # Claude analysis every 30 min
MONITOR_INTERVAL_MINUTES = int(os.getenv("MONITOR_INTERVAL_MINUTES", "5"))  # Local position check every 5 min

# ─── Trading Parameters ───────────────────────────────────────
TRADING_SYMBOL = os.getenv("TRADING_SYMBOL", "XAUUSDT")
TRADING_CATEGORY = os.getenv("TRADING_CATEGORY", "linear")
TRADING_TIMEFRAME = int(os.getenv("TRADING_TIMEFRAME", "15"))

# ─── Risk Management ─────────────────────────────────────────
MAX_RISK_PER_TRADE = float(os.getenv("MAX_RISK_PER_TRADE", "2.0"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "6.0"))
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "15.0"))
MIN_RR_RATIO = float(os.getenv("MIN_RR_RATIO", "0.85"))

# Aliases used by risk_manager.py and decision_parser.py
MAX_DAILY_LOSS = MAX_DAILY_LOSS_PCT
MAX_DRAWDOWN = MAX_DRAWDOWN_PCT
MIN_RISK_REWARD = MIN_RR_RATIO
MIN_CONFIDENCE = 60  # Minimum AI confidence to execute a trade (adjusted for LLM conservative scoring)
COOLDOWN_MINUTES = 15  # Cooldown after a losing trade
MAX_LOT_SIZE = float(os.getenv("MAX_LOT_SIZE", "2.0"))  # Max lot size per trade (increased for dynamic sizing)
BREAKEVEN_BUFFER = float(os.getenv("BREAKEVEN_BUFFER", "1.00"))  # Buffer in price units (e.g. $1.00) to ensure +$10 net profit after commission

# ─── Event-Driven Wakeup & Automation ────────────────────────
DANGER_ZONE_PCT = float(os.getenv("DANGER_ZONE_PCT", "0.15"))  # Wake Claude if price is within 15% of SL/TP
WAKEUP_COOLDOWN_MINUTES = int(os.getenv("WAKEUP_COOLDOWN_MINUTES", "30"))  # Don't wake Claude repeatedly
TRAILING_ACTIVATION = float(os.getenv("TRAILING_ACTIVATION", "10.0"))  # Price must move $10.00 in profit to activate
TRAILING_STOP_DISTANCE = float(os.getenv("TRAILING_STOP_DISTANCE", "5.0"))  # Trail the Stop Loss $5.00 behind the current price
WEEKEND_CLOSE_ENABLED = os.getenv("WEEKEND_CLOSE_ENABLED", "true").lower() == "true"  # Close all trades on Friday at 20:00 UTC

# ─── Indicator Parameters ─────────────────────────────────────
RSI_PERIOD = 14
EMA_SHORT = 20
EMA_LONG = 50
ATR_PERIOD = 14
BB_PERIOD = 20
BB_STD = 2

# ─── Telegram ─────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── Logging ──────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
LOG_ROTATION = "10 MB"
LOG_RETENTION = "30 days"


def validate_config():
    """Ensure all required configuration is present."""
    from utils.logger import log

    errors = []

    if BROKER == "bybit":
        if not BYBIT_API_KEY or "your_" in BYBIT_API_KEY:
            errors.append("BYBIT_API_KEY")
        if not BYBIT_API_SECRET or "your_" in BYBIT_API_SECRET:
            errors.append("BYBIT_API_SECRET")
    elif BROKER == "ctrader":
        if not CTRADER_CLIENT_ID or "your_" in CTRADER_CLIENT_ID:
            errors.append("CTRADER_CLIENT_ID")
        if not CTRADER_CLIENT_SECRET or "your_" in CTRADER_CLIENT_SECRET:
            errors.append("CTRADER_CLIENT_SECRET")
        if not CTRADER_ACCESS_TOKEN or "your_" in CTRADER_ACCESS_TOKEN:
            errors.append("CTRADER_ACCESS_TOKEN")

    if not ANTHROPIC_API_KEY or "your_" in ANTHROPIC_API_KEY:
        errors.append("ANTHROPIC_API_KEY")

    if errors:
        log.error(f"Missing configuration: {', '.join(errors)}")
        log.error("Please update your .env file with valid credentials")
        sys.exit(1)

    log.info("✅ Configuration validated")


def print_config_summary():
    """Print a startup config banner."""
    from utils.logger import log

    if BROKER == "bybit":
        broker_label = f"Bybit ({'🧪 TESTNET' if BYBIT_TESTNET else '🔴 MAINNET'})"
    else:
        broker_label = f"cTrader ({'🧪 DEMO' if CTRADER_HOST == 'demo' else '🔴 LIVE'})"

    log.info("╔══════════════════════════════════════════╗")
    log.info("║      🤖 Emy AI Trading System            ║")
    log.info("╠══════════════════════════════════════════╣")
    log.info(f"║  Broker:    {broker_label:<28} ║")
    log.info(f"║  Symbol:    {TRADING_SYMBOL:<28} ║")
    log.info(f"║  AI Model:  {CLAUDE_MODEL:<28} ║")
    log.info(f"║  Analysis:  Every {ANALYSIS_INTERVAL_MINUTES}min{' ' * (23 - len(str(ANALYSIS_INTERVAL_MINUTES)))}║")
    log.info(f"║  Monitor:   Every {MONITOR_INTERVAL_MINUTES}min{' ' * (23 - len(str(MONITOR_INTERVAL_MINUTES)))}║")
    log.info(f"║  Max Risk:  {MAX_RISK_PER_TRADE}% per trade{' ' * 17}║")
    log.info(f"║  Telegram:  {'✅ ON' if TELEGRAM_BOT_TOKEN else '❌ OFF'}{' ' * 23}║")
    log.info("╚══════════════════════════════════════════╝")
