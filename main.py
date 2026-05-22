"""
main.py — Emy AI Trading System Entry Point.

Three-tier architecture running on Twisted reactor:
  Tier 1: Real-time tick collection (always running)
  Tier 2: Claude AI analysis (every 2 hours)
  Tier 3: Local position monitoring (every 15 min)
"""

import signal
import sys
import time
import warnings
warnings.filterwarnings("ignore")
from datetime import datetime, timezone

from twisted.internet import reactor, defer, task

from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
from collections import deque

class FixedTcpProtocol(TcpProtocol):
    """
    Fixes a critical bug in ctrader-open-api where _send_queue is a class variable,
    causing multiple connections to steal each other's messages.
    """
    def connectionMade(self):
        self._send_queue = deque([])
        self._send_task = None
        self._lastSendMessageTime = None
        super().connectionMade()

import config
from utils.logger import log
from utils.notifier import send_alert, send_error_alert
from data.ctrader_client import (
    authenticate_app, authenticate_account,
    get_account_info, get_symbol_list, get_symbol_details,
    get_trendbars, get_open_positions, subscribe_to_prices,
    place_market_order, _symbol_cache, _current_price,
    update_demo_price, get_price_offset, get_demo_trendbars,
    get_demo_current_price, _demo_symbol_cache, _demo_current_price,
)
from data.tick_aggregator import TickAggregator
from data.indicators import calculate_all
from data.trade_journal import log_trade_open, log_trade_close, get_stats as get_journal_stats
from ai.prompt_builder import format_for_claude
from ai.claude_client import get_trading_decision, get_usage_stats
from ai.decision_parser import validate_decision
from trading.risk_manager import (
    calculate_position_size, check_cooldown,
    daily_loss_exceeded, check_drawdown, validate_risk_reward,
)

# Try to import Telegram bot
try:
    from utils.telegram_bot import bot_state, start_telegram_bot, send_bot_message
    HAS_TELEGRAM_BOT = True
except ImportError:
    HAS_TELEGRAM_BOT = False
    log.info("Telegram bot not available — running without it")


# ─── Global State ──────────────────────────────────────────────
client = None
# Per-symbol tick aggregators
tick_aggregators = {symbol: TickAggregator(max_candles=200) for symbol in config.TRADING_SYMBOLS}
tick_agg = tick_aggregators.get(config.TRADING_SYMBOL, TickAggregator(max_candles=200))  # backward compat alias
_analysis_loop = None
_monitor_loop = None
_previous_positions = {}  # Track positions to detect closes
_last_wakeup_time = 0.0  # Track when we last forced Claude to wake up
_profit_tiers_triggered = {}  # Track which profit tiers have been triggered per position
_loss_tiers_triggered = {}  # Track which loss tiers have been triggered per position
_auto_lock_tiers_triggered = {}  # Track auto-lock executions
_scale_out_triggered = set()     # Track 50% scale-out completions
_analysis_in_progress = set()  # Prevent duplicate Claude calls for the same symbol

# State cache to prevent redundant AI calls when market is flat
_last_analyzed_candle_time = {}  # symbol -> timestamp of last analyzed 15m candle
_last_analyzed_price = {}        # symbol -> price at last analysis

# Profit protection tiers: Claude wakes at each milestone (once per tier)
_PROFIT_TIERS = [0.50, 0.75]
# Loss protection tiers: Claude wakes to evaluate deep structural invalidation
_LOSS_TIERS = [0.40, 0.60, 0.80]
# Auto-Lock tiers: Mechanically trail Stop Loss silently
_AUTO_LOCK_TIERS = [0.50, 0.75]


def is_in_dead_zone(now_utc: datetime) -> bool:
    """Check if current time is within the dead zone.
    Format is 'Fri 21:00' to 'Sun 21:00'.
    """
    if not config.DEAD_ZONE_START or not config.DEAD_ZONE_END:
        return False
        
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    
    try:
        start_day_str, start_time_str = config.DEAD_ZONE_START.split(" ")
        end_day_str, end_time_str = config.DEAD_ZONE_END.split(" ")
        
        start_day = days.index(start_day_str)
        end_day = days.index(end_day_str)
        
        start_hour, start_minute = map(int, start_time_str.split(":"))
        end_hour, end_minute = map(int, end_time_str.split(":"))
        
        # Convert to a continuous minute scale from start of week
        start_total_mins = start_day * 24 * 60 + start_hour * 60 + start_minute
        end_total_mins = end_day * 24 * 60 + end_hour * 60 + end_minute
        current_total_mins = now_utc.weekday() * 24 * 60 + now_utc.hour * 60 + now_utc.minute
        
        if start_total_mins <= end_total_mins:
            # Dead zone in the same week (e.g. Fri to Sun)
            return start_total_mins <= current_total_mins <= end_total_mins
        else:
            # Dead zone wraps around the week (e.g. Sun to Mon)
            return current_total_mins >= start_total_mins or current_total_mins <= end_total_mins
            
    except Exception as e:
        log.error(f"Error parsing dead zone config: {e}")
        return False

def dead_zone_cycle():
    """Runs every minute to check if bot should hibernate."""
    now_utc = datetime.now(timezone.utc)
    in_zone = is_in_dead_zone(now_utc)
    
    # State change detected
    if in_zone and not getattr(bot_state, 'in_dead_zone', False):
        log.info("🛌 Entering Dead Zone. System hibernating.")
        bot_state.in_dead_zone = True
        bot_state.is_paused = True
        
        if HAS_TELEGRAM_BOT:
            # Check for open positions
            positions = getattr(bot_state, '_cached_positions', [])
            if positions:
                send_bot_message(
                    "⚠️ *DEAD ZONE REACHED*\n\n"
                    "We are entering the Dead Zone (weekend) but you have an open position! "
                    "The bot is going into hibernation and will not monitor this trade.\n\n"
                    "Do you want to close it now (`/close`) or keep it open while the bot sleeps?"
                )
            else:
                send_bot_message("🛌 *Dead Zone Reached* — Bot is hibernating.")
                
    elif not in_zone and getattr(bot_state, 'in_dead_zone', False):
        log.info("🌅 Exiting Dead Zone. System waking up.")
        bot_state.in_dead_zone = False
        bot_state.is_paused = False
        
        if HAS_TELEGRAM_BOT:
            send_bot_message("🌅 *Dead Zone Ended* — Bot is awake and resuming trading!")

# ═══════════════════════════════════════════════════════════════
# TIER 1: Real-Time Tick Handler
# ═══════════════════════════════════════════════════════════════

def handle_message(c, msg):
    """Handle all incoming LIVE cTrader messages.
    
    In 'Analyze Demo, Execute Live' mode, live ticks are ONLY used
    to track live prices for the offset calculator. They do NOT feed
    the candle aggregators (that's the demo handler's job).
    """
    extracted = Protobuf.extract(msg)
    msg_type = type(extracted).__name__

    if msg_type == "ProtoOASpotEvent":
        symbol_id = extracted.symbolId

        # Find symbol name
        symbol_name = None
        for name, sid in _symbol_cache.items():
            if sid == symbol_id:
                symbol_name = name
                break

        if symbol_name:
            bid = extracted.bid / 100000.0 if hasattr(extracted, "bid") and extracted.bid else 0
            ask = extracted.ask / 100000.0 if hasattr(extracted, "ask") and extracted.ask else 0

            if bid > 0 and ask > 0:
                # Update live price cache (for offset calculation and position P&L)
                from data.ctrader_client import _current_price as _cp
                _cp.setdefault(symbol_name, {})["bid"] = bid
                _cp.setdefault(symbol_name, {})["ask"] = ask


def handle_demo_message(c, msg):
    """Handle all incoming DEMO cTrader messages.
    
    Demo ticks are the 'brain' data source — they feed the candle
    aggregators that power the AI analysis. This is the data the
    bot was calibrated and profitable on.
    """
    extracted = Protobuf.extract(msg)
    msg_type = type(extracted).__name__

    if msg_type == "ProtoOASpotEvent":
        symbol_id = extracted.symbolId

        # Find symbol name from DEMO symbol cache
        symbol_name = None
        for name, sid in _demo_symbol_cache.items():
            if sid == symbol_id:
                symbol_name = name
                break

        if symbol_name and symbol_name in tick_aggregators:
            bid = extracted.bid / 100000.0 if hasattr(extracted, "bid") and extracted.bid else 0
            ask = extracted.ask / 100000.0 if hasattr(extracted, "ask") and extracted.ask else 0

            if bid > 0 and ask > 0:
                # Feed demo ticks into candle aggregators (this is the AI's data source)
                tick_aggregators[symbol_name].on_tick(bid, ask)

                # Track demo price and update offset vs live
                update_demo_price(symbol_name, bid, ask)

                # Log every 100th tick
                if tick_aggregators[symbol_name].tick_count % 100 == 0:
                    offset = get_price_offset(symbol_name)
                    log.debug(
                        f"📡 {symbol_name} Demo Tick #{tick_aggregators[symbol_name].tick_count}: "
                        f"${(bid+ask)/2:,.2f} | Offset: {offset['avg_offset']:+.3f}"
                    )


# ═══════════════════════════════════════════════════════════════
# TIER 2: Claude AI Analysis (every 2 hours)
# ═══════════════════════════════════════════════════════════════

@defer.inlineCallbacks
def analysis_cycle(wakeup_reason=None, symbol=None):
    """Full Claude AI analysis cycle — runs every ANALYSIS_INTERVAL_MINUTES."""
    global _analysis_in_progress
    
    # Resolve target symbol
    target_symbol = symbol or config.TRADING_SYMBOL
    symbol_agg = tick_aggregators.get(target_symbol, tick_agg)

    # ─── Prevent duplicate concurrent analysis for the same symbol ───
    if target_symbol in _analysis_in_progress:
        log.info(f"⏳ Analysis already in progress for {target_symbol} — skipping duplicate call")
        return
    _analysis_in_progress.add(target_symbol)

    log.info("═══════════════════════════════════════════════════════")
    log.info(f"        🧠 CLAUDE ANALYSIS CYCLE START — {target_symbol}{' (' + wakeup_reason + ')' if wakeup_reason else ''}")
    log.info("═══════════════════════════════════════════════════════")

    try:
        # ─── Step 0: Check if paused via Telegram ─────────────
        if HAS_TELEGRAM_BOT and bot_state.is_paused:
            log.info("⏸️  System paused via Telegram — skipping analysis")
            return

        # ─── Step 0.5: Market Closed & High Spread Filter ─────
        now_utc = datetime.now(timezone.utc)
        weekday = now_utc.weekday()
        hour = now_utc.hour

        is_blocked = False
        block_reason = ""
        
        if weekday == 4 and hour >= 20:
            is_blocked, block_reason = True, "Market closing for the weekend (High Spread Avoidance)"
        elif weekday == 5:
            is_blocked, block_reason = True, "Market is closed (Weekend)"
        elif weekday == 6 and hour < 23:
            is_blocked, block_reason = True, "Market is closed / just opening (High Spread Avoidance)"
        elif 20 <= hour < 23:
            is_blocked, block_reason = True, "Daily rollover & High spread window (20:00 - 23:00 UTC)"

        if is_blocked:
            log.info(f"🚫 {block_reason} — skipping analysis cycle")
            
            if not hasattr(analysis_cycle, "last_blocked_msg_time") or (now_utc - analysis_cycle.last_blocked_msg_time).total_seconds() > 43200:
                if HAS_TELEGRAM_BOT:
                    send_bot_message(f"🚫 *NO TRADE ZONE*\n{block_reason}")
                analysis_cycle.last_blocked_msg_time = now_utc
            return

        # ─── Step 0.75: High-Impact News Embargo ──────────────
        try:
            from data.news_filter import is_news_embargo_active
            news_blocked, news_reason = is_news_embargo_active()
            if news_blocked:
                log.info(f"📰 News Embargo Active: {news_reason} — skipping analysis cycle")
                
                # Prevent spamming telegram every 15 min
                if not hasattr(analysis_cycle, "last_news_msg_time") or (now_utc - analysis_cycle.last_news_msg_time).total_seconds() > 3600:
                    if HAS_TELEGRAM_BOT:
                        send_bot_message(f"📰 *NEWS EMBARGO ACTIVE*\n{news_reason}\nBot is locked out until the coast is clear.")
                    analysis_cycle.last_news_msg_time = now_utc
                return
        except Exception as e:
            log.error(f"News filter error: {e}")

        # ─── Step 1: Check cooldown timer ─────────────────────
        if check_cooldown():
            log.info("⏳ Cooldown active — skipping analysis")
            return

        # ─── Step 1.5: Global trade cap ───────────────────────
        _cap_positions = yield get_open_positions(client)
        has_symbol_pos = any(p.get('symbol') == target_symbol for p in _cap_positions)
        
        # If we reached the global max trades AND this symbol isn't one of them, skip new entries
        max_trades = getattr(config, 'MAX_OPEN_TRADES', 3)
        if not has_symbol_pos and len(_cap_positions) >= max_trades:
            log.info(f"🚫 Max {max_trades} trades open globally — skipping new analysis for {target_symbol}")
            return

        # ─── Step 2: Get candle data ──────────────────────────
        log.debug("📊 Preparing market data for Claude (M15, H1, H4)...")

        # Fetch and load for multiple timeframes (M5 for early movement)
        timeframes_to_fetch = [5, 15, 60, 240]
        candles = {}
        indicators = {}
        
        for tf in timeframes_to_fetch:
            df = symbol_agg.get_candles(tf)
            if df.empty or len(df) < 20:
                log.debug(f"📊 Insufficient {tf}m candles for {target_symbol} — fetching from Demo cTrader API...")
                # Fetch from DEMO server (the data the AI is calibrated on)
                raw_candles = yield get_demo_trendbars(
                    target_symbol,
                    period_minutes=tf, count=50
                )
                if raw_candles:
                    symbol_agg.load_historical(raw_candles, timeframe=tf)
                    df = symbol_agg.get_candles(tf)
            candles[tf] = df
            
        candles_15m = candles[15]

        if candles_15m.empty:
            log.warning("No M15 candle data available — skipping analysis")
            return

        # Get 1-minute candles for short-term view
        candles_1m = symbol_agg.get_candles(1)

        # ─── Step 3: Calculate indicators ─────────────────────
        log.debug("📐 Calculating technical indicators for all timeframes...")
        
        # Calculate main 15m
        main_indicators = calculate_all(candles_15m)
        if not main_indicators:
            log.warning("M15 Indicator calculation failed — skipping analysis")
            return
            
        # Calculate MTFA
        mtfa_data = {}
        for tf in [5, 60, 240]:
            if not candles[tf].empty:
                mtfa_ind = calculate_all(candles[tf])
                if mtfa_ind:
                    # Also include the latest price from the candles for context
                    mtfa_ind["latest_close"] = candles[tf]["close"].iloc[-1]
                    mtfa_data[tf] = mtfa_ind

        if HAS_TELEGRAM_BOT:
            bot_state.last_indicators[target_symbol] = main_indicators

        # ─── Step 4: Get account info + open positions + ticks ──
        account = yield get_account_info(client)
        tick_info = symbol_agg.get_current_price()

        # Determine Market Regime (Dual-Timeframe M15 + H1)
        from trading.regime_manager import regime_manager
        market_regime = "UNKNOWN"
        if 60 in mtfa_data and mtfa_data[60]:
            h1_ind = mtfa_data[60]
            if h1_ind.get("adx") is not None and h1_ind.get("ema50") is not None and main_indicators.get("adx") is not None:
                market_regime = regime_manager.update_regime(
                    m15_adx=main_indicators["adx"],
                    h1_adx=h1_ind["adx"], 
                    current_price=tick_info["mid"],
                    ema50=h1_ind["ema50"]
                )
        log.debug(f"🧭 Current Market Regime: {market_regime}")

        # Fetch open positions for Claude to evaluate
        all_open_positions = yield get_open_positions(client)
        # Filter positions so Claude ONLY sees and evaluates trades for THIS specific symbol
        open_positions = [p for p in all_open_positions if p.get('symbol') == target_symbol]
        account["positions"] = open_positions
        
        if open_positions:
            log.info(f"📍 {len(open_positions)} open position(s) for {target_symbol} — Claude will evaluate")
        else:
            log.debug(f"📍 No open positions for {target_symbol} — looking for new entries")

        # ─── Step 5: Check drawdown ───────────────────────────
        if check_drawdown(account["balance"]):
            log.warning("🚨 Drawdown limit hit — skipping analysis")
            return

        # ─── Step 5.5: Python Pre-Filter (Cost Saving) ────────
        is_forced = wakeup_reason and ("FORCED" in wakeup_reason.upper() or "PRICE_TRIGGER" in wakeup_reason.upper())
        if not open_positions and not is_forced:
            py_score = 0  # Start from zero — earn your way to Claude
            
            # ─── Score the setup ───
            trend = main_indicators.get("trend", "FLAT")
            structure = main_indicators.get("structure", "Unknown")
            is_trend_aligned = False
            
            if trend != "FLAT": py_score += 15
            
            if (trend == "BULLISH" and "Bullish" in structure) or (trend == "BEARISH" and "Bearish" in structure):
                py_score += 20
                is_trend_aligned = True
                log.info(f"📈 Trend Alignment Bonus: Added +20 points (Trend and Structure both {trend})")
            
            # ─── Volume Gate: No volume = No Claude ───
            vol_ratio = main_indicators.get("volume_ratio", 0)
            prev_vol_ratio = main_indicators.get("prev_volume_ratio", 0)
            
            # Check if Claude is actively tracking this setup
            from ai.ai_memory import get_tracking_info, SETUP_EXPIRY_HOURS
            tracked = get_tracking_info(target_symbol)
            is_tracking = False
            if tracked and "started_at" in tracked:
                if (time.time() - tracked["started_at"]) / 3600 < SETUP_EXPIRY_HOURS:
                    is_tracking = True
            
            # Allow Claude if: volume is decent OR strongly trend-aligned
            if vol_ratio < 0.3 and prev_vol_ratio < 0.3 and not is_trend_aligned:
                log.info(f"🛑 Volume Gate: {target_symbol} volume {vol_ratio:.2f}x (prev {prev_vol_ratio:.2f}x) < 0.3x minimum. Skipping Claude.")
                if HAS_TELEGRAM_BOT:
                    bot_state.last_decision[target_symbol] = {
                        "action": "HOLD",
                        "confidence": 0,
                        "reason": f"Volume Gate — Volume at {vol_ratio:.2f}x average (< 0.3x minimum)",
                        "timestamp": time.time()
                    }
                return
            
            adx = main_indicators.get("adx", 0)
            if adx > 25: py_score += 15
            elif adx > 20: py_score += 10
            
            if vol_ratio > 1.5: py_score += 15
            elif vol_ratio > 1.0: py_score += 10
            elif vol_ratio > 0.5: py_score += 5
            
            if main_indicators.get("sweep_detected", False): py_score += 25
            
            rsi = main_indicators.get("rsi", 50)
            if rsi < 30 or rsi > 70: py_score += 15
            elif rsi < 35 or rsi > 65: py_score += 10
            elif rsi < 45 or rsi > 55: py_score += 5
            
            # MACD momentum
            macd_hist = main_indicators.get("macd_hist", 0)
            if abs(macd_hist) > 2: py_score += 10
            elif abs(macd_hist) > 1: py_score += 5
            
            # Bonus points if the 1-minute Tripwire explicitly woke Claude up due to a spike
            if wakeup_reason and "TRIPWIRE" in wakeup_reason:
                py_score += 25
                log.info(f"⚡ Tripwire Bonus: Added +25 points to {target_symbol} score for sudden momentum/volume spike!")
            
            # Bonus points if Claude is actively tracking this setup in memory
            if 'is_tracking' in locals() and is_tracking:
                py_score += 25
                log.info(f"👀 AI Tracking Bonus: Added +25 points to {target_symbol} because Claude is tracking a setup in memory!")
            
            if wakeup_reason and "GEMINI HUNTER" in wakeup_reason:
                log.info(f"🎯 Gemini Hunter Override: Bypassing Python Pre-Filter ({py_score}%) to wake Claude immediately!")
            else:
                # Dynamic Pre-Filter Threshold based on Regime and Volume
                dynamic_py_threshold = 45
                if market_regime in ["TRENDING", "VOLATILE"]:
                    dynamic_py_threshold = 40
                elif market_regime == "CHOPPY":
                    dynamic_py_threshold = 50
                
                if vol_ratio > 1.5:
                    dynamic_py_threshold -= 10
                elif vol_ratio < 0.5:
                    dynamic_py_threshold += 10
                
                dynamic_py_threshold = max(30, min(dynamic_py_threshold, 60))
                
                if py_score < dynamic_py_threshold:
                    log.info(f"🛑 Python Pre-Filter: {target_symbol} score {py_score}% < {dynamic_py_threshold}% (Dynamic). Skipping Claude.")
                    if HAS_TELEGRAM_BOT:
                        bot_state.last_decision[target_symbol] = {
                            "action": "HOLD",
                            "confidence": py_score,
                            "reason": f"Python Pre-Filter (Score: {py_score}%) — Insufficient setup to wake AI",
                            "timestamp": time.time()
                        }
                    return
                else:
                    log.info(f"✅ Python Pre-Filter: {target_symbol} passed with score {py_score}%! Waking Claude.")

        # ─── Step 5.75: API Response Caching Logic ────────────
        current_candle_time = candles_15m.iloc[-1]['timestamp'] if not candles_15m.empty else None
        current_price = tick_info["mid"]
        atr = main_indicators.get("atr", 10.0)
        
        has_position = len(open_positions) > 0
        
        # ─── Sync AI Memory with Reality ───────────────────────
        try:
            from ai.ai_memory import sync_open_positions
            sync_open_positions(target_symbol, has_position)
        except Exception as e:
            log.debug(f"Memory sync error: {e}")

        last_time = _last_analyzed_candle_time.get(target_symbol)
        last_price = _last_analyzed_price.get(target_symbol, current_price)
        
        # Check if price moved > 50% ATR (or 25% if managing an open position)
        atr_multiplier = 0.25 if has_position else 0.50
        price_moved = abs(current_price - last_price) > (atr * atr_multiplier) 
        new_candle = (last_time != current_candle_time)
        
        if not has_position and not wakeup_reason and not new_candle and not price_moved:
            log.info(f"💤 API Cache: {target_symbol} flat inside same candle (<25% ATR). Skipping Claude.")
            if HAS_TELEGRAM_BOT:
                bot_state.last_decision[target_symbol] = {
                    "action": "HOLD",
                    "confidence": 0,
                    "reason": "API Cache — Market flat inside current candle.",
                    "timestamp": time.time()
                }
            return
            
        # Update cache trackers for the API call we are about to make
        _last_analyzed_candle_time[target_symbol] = current_candle_time
        _last_analyzed_price[target_symbol] = current_price

        # ─── Step 6: Format prompt & call Claude ──────────────
        # Get Streak and Daily PNL for AI Context
        from trading.risk_manager import risk_state
        from data.trade_journal import get_stats
        
        try:
            stats = get_stats(target_symbol)
            today_pnl = stats.get("today_pnl", 0.0)
        except Exception:
            today_pnl = 0.0
            
        # Get Dollar Strength for correlation (especially for Gold)
        dollar_strength = ""
        if "XAU" in target_symbol or target_symbol == "BTCUSD":
            try:
                from data.correlation_engine import get_dollar_strength
                dollar_strength = get_dollar_strength(tick_aggregators)
            except Exception as e:
                log.debug(f"Correlation engine error: {e}")

        system_additions, user_data = format_for_claude(
            candles_15m, main_indicators, account,
            candles_1m=candles_1m if not candles_1m.empty else None,
            tick_info=tick_info if tick_info["bid"] > 0 else None,
            mtfa_data=mtfa_data,
            market_regime=market_regime,
            ml_report=ml_report_cache,
            wakeup_reason=wakeup_reason,
            streak_count=risk_state.streak_count,
            daily_pnl=today_pnl,
            symbol=target_symbol,
            dollar_correlation=dollar_strength
        )

        log.debug(f"🧠 Sending data to {config.CLAUDE_MODEL}...")
        decision = get_trading_decision(user_data, system_additions)

        log.info(f"🧠 Claude Decision: {decision['action']}")
        log.info(f"🎯 Confidence:    {decision.get('confidence', 'N/A')}%")
        log.info(f"📝 Reason:        {decision.get('reason', 'N/A')}")

        # ─── Record decision in AI memory ──────────────────
        try:
            from ai.ai_memory import record_decision, record_regime
            current_price_for_memory = tick_info.get("mid", 0) if tick_info else 0
            record_decision(target_symbol, decision, current_price_for_memory)
            # Record market regime so Claude can see regime transitions
            record_regime(target_symbol, market_regime)
        except Exception as e:
            log.debug(f"Memory record error: {e}")

        # Log usage stats
        stats = get_usage_stats()
        log.info(
            f"📊 Session stats: {stats['total_calls']} calls | "
            f"${stats['estimated_cost']:.4f} spent"
        )

        if HAS_TELEGRAM_BOT:
            bot_state.update_cycle(symbol=target_symbol, decision=decision, indicators=indicators)

        # ─── Step 7: Validate decision ────────────────────────
        has_position = bool(open_positions)
        if not validate_decision(decision, has_position=has_position, market_regime=market_regime):
            log.warning("❌ Decision validation failed — skipping")
            return
            
        # ─── Step 7.5: Smart Pullback Filter (H4 Trend Protection) ────────
        if decision["action"] in ["BUY", "SELL"] and 240 in mtfa_data:
            h4_ind = mtfa_data[240]
            if h4_ind and h4_ind.get("ema50") and h4_ind.get("ema200"):
                h4_ema50 = h4_ind["ema50"]
                h4_ema200 = h4_ind["ema200"]
                
                is_counter_trend = (decision["action"] == "BUY" and h4_ema50 < h4_ema200) or \
                                   (decision["action"] == "SELL" and h4_ema50 > h4_ema200)
                
                if is_counter_trend:
                    if decision.get("confidence", 0) < 70:
                        log.warning(f"🚫 VETO: Blocked {decision['action']} signal. Counter-trend requires >= 70% confidence (Got {decision.get('confidence', 0)}%)")
                        decision["action"] = "HOLD"
                        decision["reason"] = f"VETOED {decision['action']}: Low confidence ({decision.get('confidence', 0)}%) for a counter-trend pullback."
                    else:
                        log.warning(f"⚠️ SMART PULLBACK: Counter-trend {decision['action']} allowed at {decision.get('confidence')}% confidence. Keeping original TP (no halving — prevents inverted R:R).")
                        decision["is_smart_pullback"] = True

        if decision["action"] == "HOLD":
            log.info("⏸️  Final action: HOLD — no action this cycle")
            if HAS_TELEGRAM_BOT:
                safe_reason = str(decision.get('reason', 'N/A')).replace("_", "-")
                wake_status = decision.get("wake_status", "N/A")
                
                header = "⏸️ *HOLD*"
                if wake_status == "NEED_MORE_CONFIRMATION":
                    header = "⏳ *NEED MORE CONFIRMATION*"
                elif wake_status == "DECLINED":
                    header = "🛑 *SETUP DECLINED*"
                elif wake_status == "SETUP_MET":
                    header = "✅ *SETUP MET (Executing)*"
                    
                send_bot_message(
                    f"{header} — Confidence: {decision.get('confidence')}%\n"
                    f"_{safe_reason}_\n"
                    f"💰 Cost so far: ${stats['estimated_cost']:.4f}"
                )
            return

        # ─── Step 8: Handle position management actions ───────
        positions = yield get_open_positions(client)

        if decision["action"] == "CLOSE_TRADE":
            if positions:
                from data.ctrader_client import close_position
                for pos in positions:
                    current_price = symbol_agg.get_current_price().get("mid", pos["entryPrice"])
                    entry_price = float(pos.get("entryPrice", 0))
                    sl_price = float(pos.get("stopLoss", 0))
                    
                    # ─── Premature Close Protection ───
                    # If the trade is profitable but hasn't reached 1R, block the close
                    # unless Claude's reason mentions actual reversal signals
                    if entry_price > 0 and sl_price > 0:
                        risk_taken = abs(entry_price - sl_price)
                        if pos["side"] == "BUY":
                            current_profit = current_price - entry_price
                        else:
                            current_profit = entry_price - current_price
                        
                        close_reason = str(decision.get('position_action_reason', decision.get('reason', ''))).lower()
                        has_reversal_signal = any(kw in close_reason for kw in [
                            "reversal", "engulfing", "break of structure", "bos", 
                            "ema cross", "trend reversed", "lower low", "higher high",
                            "sweep against", "invalidat"
                        ])
                        
                        if current_profit > 0 and current_profit < risk_taken and not has_reversal_signal:
                            log.warning(
                                f"🛡️ PREMATURE CLOSE BLOCKED: Trade is +${current_profit:.2f} profit "
                                f"(< 1R of ${risk_taken:.2f}). No reversal signals detected. "
                                f"Forcing HOLD to let trade reach TP."
                            )
                            if HAS_TELEGRAM_BOT:
                                send_bot_message(
                                    f"🛡️ *PREMATURE CLOSE BLOCKED*\n"
                                    f"Claude wanted to close at +${current_profit:.2f} but trade hasn't reached 1R (${risk_taken:.2f}).\n"
                                    f"No reversal signals detected. Keeping trade open."
                                )
                            return
                    
                    log.info(f"🔒 Closing position {pos['positionId']}...")
                    yield close_position(client, pos["positionId"], int(pos["volume"] * 100))
                    log.info(f"✅ Position {pos['positionId']} closed!")

                    # Auto-journal the close
                    try:
                        pnl = log_trade_close(
                            str(pos["positionId"]), current_price,
                            f"Claude: {decision.get('position_action_reason', 'Reversal detected')}"
                        )
                    except Exception:
                        pnl = pos.get("unrealizedPnl", 0)

                    pnl_val = pnl if pnl is not None else pos.get("unrealizedPnl", 0)
                    pnl_emoji = "🟢" if pnl_val >= 0 else "🔴"
                    
                    if pnl_val is not None and "balance" in account:
                        from trading.risk_manager import risk_state, record_loss
                        risk_state.update(pnl_val, account["balance"])
                        if pnl_val < 0:
                            record_loss()

                    if HAS_TELEGRAM_BOT:
                        send_bot_message(
                            f"🔒 *POSITION CLOSED — Claude Decision*\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📍 {pos['side']} @ `${pos['entryPrice']:,.2f}`\n"
                            f"🏁 Exit: `${current_price:,.2f}`\n"
                            f"{pnl_emoji} P&L: `${pnl_val:,.2f}`\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📊 _{decision.get('position_action_reason', decision.get('reason', 'N/A'))}_"
                        )

                    send_alert(f"🔒 Position closed: {pos['side']} @ ${pos['entryPrice']:,.2f} → ${current_price:,.2f}")
                    
                    # Clear wake triggers for this symbol since the trade is closed
                    from ai.ai_memory import clear_wake_triggers
                    clear_wake_triggers(target_symbol)
            else:
                log.warning("CLOSE_TRADE but no open positions")
            return

        elif decision["action"] == "MOVE_SL_BE":
            if positions:
                from data.ctrader_client import amend_position_sltp
                for pos in positions:
                    entry = pos["entryPrice"]
                    existing_sl = float(pos.get("stopLoss", 0))
                    
                    pos_sym = pos.get("symbol", target_symbol)
                    if "XAU" in pos_sym:
                        buffer, dec = 0.50, 2
                    elif "JPY" in pos_sym:
                        buffer, dec = 0.05, 3
                    elif "BTC" in pos_sym:
                        buffer, dec = 20.0, 2
                    else:
                        buffer, dec = 0.0005, 5
                    
                    if pos['side'] == 'BUY':
                        new_sl = round(entry + buffer, dec)
                        # Prevent moving a deeply profitable SL backwards to breakeven
                        if existing_sl > 0 and existing_sl >= new_sl:
                            log.info(f"⏭️ Skipping MOVE_SL_BE: Current SL (${existing_sl:,.5f}) is already better than breakeven (${new_sl:,.5f})")
                            continue
                    else:
                        new_sl = round(entry - buffer, dec)
                        # Prevent moving a deeply profitable SL backwards to breakeven
                        if existing_sl > 0 and existing_sl <= new_sl:
                            log.info(f"⏭️ Skipping MOVE_SL_BE: Current SL (${existing_sl:,.5f}) is already better than breakeven (${new_sl:,.5f})")
                            continue
                    
                    existing_tp = pos.get("takeProfit", 0) or decision.get("take_profit", 0)
                    log.info(f"🛡️ Moving SL to breakeven (${new_sl:,.5f}) for position {pos['positionId']} — keeping TP ${existing_tp}")
                    yield amend_position_sltp(client, pos["positionId"], new_sl, existing_tp, symbol_name=pos.get("symbol"))
                    log.info(f"✅ SL moved to breakeven!")

                    if HAS_TELEGRAM_BOT:
                        safe_reason = str(decision.get('position_action_reason', 'Trade protected')).replace("_", "-")
                        send_bot_message(
                            f"🛡️ *SL → BREAKEVEN*\n"
                            f"Position: {pos['side']} @ ${entry:,.5f}\n"
                            f"New SL: ${new_sl:,.5f} (+ commission buffer)\n"
                            f"_{safe_reason}_"
                        )
            return

        elif decision["action"] == "MOVE_SL":
            if positions:
                from data.ctrader_client import amend_position_sltp
                for pos in positions:
                    new_sl = decision.get("stop_loss", 0)
                    if not new_sl or new_sl <= 0:
                        log.warning("MOVE_SL action but no valid stop_loss provided by Claude. Skipping.")
                        continue
                        
                    existing_sl = float(pos.get("stopLoss", 0))
                    existing_tp = pos.get("takeProfit", 0) or decision.get("take_profit", 0)
                    
                    if pos['side'] == 'BUY' and existing_sl > 0 and new_sl <= existing_sl:
                        log.info(f"⏭️ Skipping MOVE_SL: New SL (${new_sl:,.5f}) is worse than current SL (${existing_sl:,.5f})")
                        continue
                    if pos['side'] == 'SELL' and existing_sl > 0 and new_sl >= existing_sl:
                        log.info(f"⏭️ Skipping MOVE_SL: New SL (${new_sl:,.5f}) is worse than current SL (${existing_sl:,.5f})")
                        continue

                    log.info(f"🛡️ Moving SL to dynamic target (${new_sl:,.5f}) for position {pos['positionId']}")
                    yield amend_position_sltp(client, pos["positionId"], float(new_sl), existing_tp, symbol_name=pos.get("symbol"))
                    log.info(f"✅ Dynamic SL moved!")

                    if HAS_TELEGRAM_BOT:
                        safe_reason = str(decision.get('position_action_reason', 'Dynamic SL trailing')).replace("_", "-")
                        send_bot_message(
                            f"🛡️ *DYNAMIC SL TRAIL*\n"
                            f"Position: {pos['side']} @ ${pos['entryPrice']:,.5f}\n"
                            f"New SL: ${new_sl:,.5f}\n"
                            f"_{safe_reason}_"
                        )
            return

        elif decision["action"] == "PARTIAL_CLOSE":
            if positions:
                from data.ctrader_client import close_position, amend_position_sltp
                for pos in positions:
                    half_vol = int(pos["volume"] * 100 / 2)
                    if half_vol > 0:
                        log.info(f"✂️ Partial close: closing {half_vol/100} lots of position {pos['positionId']}...")
                        yield close_position(client, pos["positionId"], half_vol)
                        # Move SL to breakeven on remaining, preserving TP
                        pos_sym = pos.get("symbol", target_symbol)
                        if "XAU" in pos_sym:
                            buffer, dec = 0.50, 2
                        elif "JPY" in pos_sym:
                            buffer, dec = 0.05, 3
                        elif "BTC" in pos_sym:
                            buffer, dec = 20.0, 2
                        else:
                            buffer, dec = 0.0005, 5
                            
                        new_sl = round(pos['entryPrice'] + buffer, dec) if pos['side'] == 'BUY' else round(pos['entryPrice'] - buffer, dec)
                        existing_tp = pos.get("takeProfit", 0)
                        yield amend_position_sltp(client, pos["positionId"], new_sl, existing_tp, symbol_name=pos.get("symbol"))
                        log.info(f"✅ Partial close done, SL moved to BE!")

                        if HAS_TELEGRAM_BOT:
                            safe_reason = str(decision.get('position_action_reason', 'TP1 reached')).replace("_", "-")
                            send_bot_message(
                                f"✂️ *PARTIAL CLOSE*\n"
                                f"Closed: {half_vol/100} lots\n"
                                f"Remaining SL → ${new_sl:,.5f} (+ buffer)\n"
                                f"_{safe_reason}_"
                            )
            return

        # ─── Step 9: New trade — check existing positions for this symbol ──
        symbol_positions = [p for p in positions if p.get('symbol') == target_symbol]
        if symbol_positions:
            has_same_direction = any(p["side"] == decision["action"] for p in symbol_positions)
            
            if has_same_direction:
                log.info(f"📍 {target_symbol} already has a {decision['action']} position open — skipping new {decision['action']} entry (No doubling up!)")
                return
                
            # If we get here, all open positions must be in the opposite direction (Reversal)
            log.warning(f"🔄 REVERSAL DETECTED: Claude ordered {decision['action']} while holding opposite position(s)!")
            from data.ctrader_client import close_position
            for pos in symbol_positions:
                yield close_position(client, pos["positionId"], int(pos["volume"] * 100))
                log.info(f"✅ Closed old {pos['side']} position {pos['positionId']} for reversal.")
                
                # Auto-journal the close
                current_price_rev = symbol_agg.get_current_price().get("mid", pos["entryPrice"])
                try:
                    log_trade_close(str(pos["positionId"]), current_price_rev, f"Reversed to {decision['action']}")
                except Exception:
                    pass

            if HAS_TELEGRAM_BOT:
                send_bot_message(f"🔄 *REVERSAL INITIATED*\nAutomatically closed {len(symbol_positions)} opposite position(s) to execute {decision['action']}.")

        # ─── Step 9.4: Naked Trade Prevention ──────────────────────
        if not decision.get("stop_loss") or not decision.get("take_profit"):
            log.error(f"🚨 NAKED TRADE ABORTED: Trade {decision['action']} for {target_symbol} has missing SL or TP.")
            if HAS_TELEGRAM_BOT:
                send_bot_message(f"🚨 *NAKED TRADE ABORTED*\nMissing SL or TP for {target_symbol}.")
            return
            
        # ─── Step 9.5: ATR SL Padding & Offset Adjustments ─────────
        target_decimals = 5 if ("EUR" in target_symbol or "GBP" in target_symbol or "AUD" in target_symbol) else (3 if "JPY" in target_symbol else 2)
        
        # 1. ATR Padding
        atr = main_indicators.get("atr", 0)
        if atr and atr > 0:
            min_sl_dist = 1.5 * atr
            if decision["action"] == "BUY":
                sl_dist = decision["entry_price"] - decision["stop_loss"]
                if sl_dist < min_sl_dist:
                    added_dist = min_sl_dist - sl_dist
                    new_sl = decision["entry_price"] - min_sl_dist
                    log.warning(f"🛡️ ATR Padding: Widening BUY SL from {decision['stop_loss']} to {new_sl:.{target_decimals}f} (1.5x ATR = {min_sl_dist:.{target_decimals}f})")
                    decision["stop_loss"] = round(new_sl, target_decimals)
                    decision["take_profit"] = round(decision["take_profit"] + added_dist, target_decimals)
            elif decision["action"] == "SELL":
                sl_dist = decision["stop_loss"] - decision["entry_price"]
                if sl_dist < min_sl_dist:
                    added_dist = min_sl_dist - sl_dist
                    new_sl = decision["entry_price"] + min_sl_dist
                    log.warning(f"🛡️ ATR Padding: Widening SELL SL from {decision['stop_loss']} to {new_sl:.{target_decimals}f} (1.5x ATR = {min_sl_dist:.{target_decimals}f})")
                    decision["stop_loss"] = round(new_sl, target_decimals)
                    decision["take_profit"] = round(decision["take_profit"] - added_dist, target_decimals)

        # 2. Price Offset Adjustment
        offset_info = get_price_offset(target_symbol)
        avg_offset = offset_info.get("avg_offset", 0)
        
        # SAFETY: Block trades if offset is dangerously high
        SAFE_OFFSETS = {
            'XAUUSD': 3.00,  # Bumped from 1.5 to 3.0 for gold volatility
            'EURUSD': 0.0005,
            'USDJPY': 0.05,
            'BTCUSD': 60.0
        }
        max_safe_offset = SAFE_OFFSETS.get(target_symbol, 1.50)
        
        if abs(avg_offset) > max_safe_offset:
            log.warning(
                f"🚨 SAFETY BLOCK: Price offset too high! "
                f"Δ{avg_offset:+.3f} > ${max_safe_offset}. Trade BLOCKED."
            )
            if HAS_TELEGRAM_BOT:
                send_bot_message(
                    f"🚨 *TRADE BLOCKED — Offset Too High*\n"
                    f"Demo↔Live offset: `Δ{avg_offset:+.3f}`\n"
                    f"Max allowed: `${max_safe_offset}`\n"
                    f"Skipping {decision['action']} {target_symbol}"
                )
            return
            
        adjusted_sl = decision["stop_loss"]
        adjusted_tp = decision["take_profit"]
        
        if abs(avg_offset) > 0.01:  # Only adjust if offset is meaningful
            adjusted_sl = round(decision["stop_loss"] + avg_offset, target_decimals)
            adjusted_tp = round(decision["take_profit"] + avg_offset, target_decimals)
            
            # Add a safety buffer on SL (widen it to prevent premature hits)
            sl_buffer = 0.50 if "XAU" in target_symbol else (0.00010 if ("EUR" in target_symbol or "GBP" in target_symbol or "AUD" in target_symbol) else 0.05)
            if decision["action"] == "BUY":
                adjusted_sl -= sl_buffer  # Move SL further down for BUY
                adjusted_tp += sl_buffer  # Push TP up to maintain R:R
            else:
                adjusted_sl += sl_buffer  # Move SL further up for SELL
                adjusted_tp -= sl_buffer  # Push TP down to maintain R:R
                
            adjusted_sl = round(adjusted_sl, target_decimals)
            adjusted_tp = round(adjusted_tp, target_decimals)
            
            log.info(
                f"📐 Price offset adjustment: Δ{avg_offset:+.3f} | "
                f"SL: ${decision['stop_loss']:,.{target_decimals}f} → ${adjusted_sl:,.{target_decimals}f} | "
                f"TP: ${decision['take_profit']:,.{target_decimals}f} → ${adjusted_tp:,.{target_decimals}f}"
            )
            
            if HAS_TELEGRAM_BOT:
                send_bot_message(
                    f"📐 *Offset Adjustment Applied*\n"
                    f"Demo↔Live Δ: `{avg_offset:+.3f}`\n"
                    f"SL: `${decision['stop_loss']:,.{target_decimals}f}` → `${adjusted_sl:,.{target_decimals}f}`\n"
                    f"TP: `${decision['take_profit']:,.{target_decimals}f}` → `${adjusted_tp:,.{target_decimals}f}`\n"
                    f"Buffer: `${sl_buffer:.{target_decimals}f}`"
                )
            
            # Update decision with final adjusted SL/TP so position sizing is accurate
            decision["stop_loss"] = adjusted_sl
            decision["take_profit"] = adjusted_tp

        # 3. Final R:R Enforcement (Exact 1:1, 1:1.5, or 1:2 against LIVE PRICE)
        # We must use the current live market price to measure true SL distance, otherwise slippage destroys the 1:1 ratio.
        current_live_price = tick_info.get("mid", decision["entry_price"]) if tick_info else decision["entry_price"]
        
        true_sl_dist = abs(current_live_price - decision["stop_loss"])
        raw_tp_dist = abs(decision["take_profit"] - current_live_price)
        
        # Determine closest R:R tier
        if true_sl_dist > 0:
            raw_rr = raw_tp_dist / true_sl_dist
            if raw_rr < 1.25:
                target_multiplier = 1.0
            elif raw_rr < 1.75:
                target_multiplier = 1.5
            else:
                target_multiplier = 2.0
                
            enforced_tp_dist = true_sl_dist * target_multiplier
            
            if decision["action"] == "BUY":
                decision["take_profit"] = round(current_live_price + enforced_tp_dist, target_decimals)
            else:
                decision["take_profit"] = round(current_live_price - enforced_tp_dist, target_decimals)
                
            log.info(f"⚖️ R:R Enforcement: Snapped raw R:R ({raw_rr:.2f}) to {target_multiplier}:1 tier based on LIVE price ${current_live_price}. New TP: {decision['take_profit']:.{target_decimals}f}")
            adjusted_tp = decision["take_profit"]

        # ─── Step 10: Calculate position size ─────────────────
        qty = calculate_position_size(
            balance=account["balance"],
            risk_pct=decision["position_size_pct"],
            entry=decision["entry_price"],
            stop_loss=decision["stop_loss"],
            market_regime=market_regime,
            symbol=target_symbol,
            current_price=tick_info.get("mid", 1.0) if tick_info else 1.0
        )

        # (Step 10.5 Defensive sizing removed per strict $50 risk mandate)

        if qty <= 0:
            log.warning("Position size is 0 — skipping trade")
            return
        
        # ─── Step 11: Execute trade via cTrader ───────────────
        log.info("🚀 Executing trade via cTrader (Live Prop Firm)...")
        order = yield place_market_order(
            client,
            target_symbol,
            decision["action"],
            qty,
            sl_price=adjusted_sl,
            tp_price=adjusted_tp,
        )

        if order:
            log.info("✅ Trade executed successfully!")

            # Log to trade journal
            try:
                log_trade_open(
                    side=decision["action"],
                    volume=qty,
                    entry_price=decision["entry_price"],
                    stop_loss=decision["stop_loss"],
                    take_profit=decision["take_profit"],
                    confidence=decision.get("confidence", 0),
                    session_grade=decision.get("session_grade", ""),
                    sweep_detected=decision.get("sweep_detected", False),
                    reason=decision.get("reason", ""),
                    regime=market_regime,
                    symbol=target_symbol
                )
            except Exception as je:
                log.warning(f"Journal logging failed: {je}")

            if HAS_TELEGRAM_BOT:
                bot_state.trades_today += 1
                action_emoji = "🟢" if decision["action"] == "BUY" else "🔴"
                safe_reason = str(decision.get('reason', 'N/A')).replace("_", "-")
                send_bot_message(
                    f"{action_emoji} *{decision['action']} {target_symbol}*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📍 Entry: `${decision['entry_price']:,.2f}`\n"
                    f"🛑 SL: `${decision['stop_loss']:,.2f}`\n"
                    f"🎯 TP: `${decision['take_profit']:,.2f}`\n"
                    f"📦 Qty: `{qty}`\n"
                    f"📊 Confidence: `{decision['confidence']}%`\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"💬 _{safe_reason}_"
                )

            send_alert(
                f"🚀 Trade Executed: {decision['action']} {target_symbol}\n"
                f"Entry: ${decision['entry_price']:,.2f} | SL: ${decision['stop_loss']:,.2f} | TP: ${decision['take_profit']:,.2f}"
            )

    except Exception as e:
        err_str = str(e)
        if "Deferred" in err_str:
            log.debug(f"Analysis skipped (busy): {err_str}")
        else:
            log.error(f"❌ Analysis cycle failed: {e}")
            send_error_alert(err_str)
            if HAS_TELEGRAM_BOT:
                send_bot_message(f"🚨 *Analysis error:*\n`{err_str[:200]}`")

    finally:
        _analysis_in_progress.discard(target_symbol)
        log.info("═══════════════════════════════════════════════════════")
        log.info("        🧠 CLAUDE ANALYSIS CYCLE END")
        log.info("═══════════════════════════════════════════════════════\n")


# ═══════════════════════════════════════════════════════════════
# ML ANALYZER LOOP
# ═══════════════════════════════════════════════════════════════

ml_report_cache = ""

def update_ml_report():
    """Runs every hour to update Claude's statistical self-awareness report."""
    global ml_report_cache
    try:
        from data.ml_analyzer import analyze_edges
        ml_report_cache = analyze_edges() or "Not enough data for ML analysis yet."
        log.info("🧠 ML Edge Report updated successfully.")
    except Exception as e:
        log.warning(f"Failed to update ML Edge Report: {e}")


# ═══════════════════════════════════════════════════════════════
# TIER 1.5: Volatility Tripwire (every 60 sec)
# ═══════════════════════════════════════════════════════════════

last_tripwire_time = {symbol: None for symbol in config.TRADING_SYMBOLS}

def is_market_open():
    now = datetime.now(timezone.utc)
    if now.weekday() == 5: return False
    if now.weekday() == 6 and now.hour < 21: return False
    return True

@defer.inlineCallbacks
def tripwire_cycle():
    """
    Runs every 60 seconds. Scans ALL tracked symbols for:
    1. AI Wake Triggers (exact price levels Claude asked Python to watch)
    2. Volatility spikes, RSI capitulation, and wick sweeps.
    Wakes Claude only for the triggered symbol.
    """
    if not is_market_open():
        return

    # ─── AI WAKE TRIGGERS: Check Claude's requested price levels ───
    # Works for BOTH entry tracking AND open position management
    from ai.ai_memory import get_wake_triggers
    price_triggered_symbols = set()

    for symbol in config.TRADING_SYMBOLS:
        try:
            triggers = get_wake_triggers(symbol)
            if not triggers:
                continue

            wake_above = triggers.get("wake_above", 0)
            wake_below = triggers.get("wake_below", 0)
            lookout_instructions = triggers.get("lookout_instructions", "")

            if not wake_above and not wake_below and not lookout_instructions:
                continue

            sym_agg = tick_aggregators.get(symbol)
            if sym_agg is None:
                continue
            tick_info = sym_agg.get_current_price()
            current_price = tick_info.get("mid", 0)
            if current_price <= 0:
                continue

            triggered = False
            trigger_reason = ""

            if wake_above and current_price >= wake_above:
                triggered = True
                trigger_reason = f"Price ${current_price:,.5g} broke ABOVE Claude's target ${wake_above:,.5g}"
            elif wake_below and current_price <= wake_below:
                triggered = True
                trigger_reason = f"Price ${current_price:,.5g} broke BELOW Claude's target ${wake_below:,.5g}"
            elif lookout_instructions:
                # ─── GEMINI LOOKOUT EVALUATION ───
                sym_agg_15 = tick_aggregators.get(symbol)
                if sym_agg_15:
                    candles_15 = sym_agg_15.get_candles(15)
                    if not candles_15.empty and len(candles_15) >= 20:
                        from data.indicators import calculate_all
                        trip_indicators = calculate_all(candles_15)
                        
                        from ai.gemini_client import gemini_rotator
                        from twisted.internet import threads
                        from ai.prompt_builder import format_for_claude
                        from trading.regime_manager import regime_manager
                        
                        log.info(f"👀 Gemini Lookout evaluating {symbol}: '{lookout_instructions}' (with FULL Claude context)...")
                        acct = getattr(bot_state, '_cached_balance', {'balance': 0})
                        acct['positions'] = [p for p in getattr(bot_state, '_cached_positions', []) if p.get('symbol') == symbol]
                        
                        dollar_strength = ""
                        if "XAU" in symbol or symbol == "BTCUSD":
                            try:
                                from data.correlation_engine import get_dollar_strength
                                dollar_strength = get_dollar_strength(tick_aggregators)
                            except Exception:
                                pass

                        sys_add, usr_data = format_for_claude(
                            candles_15m=candles_15,
                            indicators=trip_indicators,
                            account=acct,
                            candles_1m=candles_1m,
                            tick_info=tick_info,
                            market_regime=regime_manager.current_regime,
                            ml_report=ml_report_cache,
                            symbol=symbol,
                            dollar_correlation=dollar_strength
                        )
                        full_context = f"{sys_add}\n\n{usr_data}"

                        gemini_decision = yield threads.deferToThread(
                            gemini_rotator.evaluate_lookout_instructions,
                            symbol, full_context, lookout_instructions
                        )
                        
                        if gemini_decision:
                            action = gemini_decision.get("action", "IGNORE")
                            gemini_reason = gemini_decision.get("reason", "Instructions met")
                            
                            if action == "WAKE_CLAUDE":
                                triggered = True
                                trigger_reason = f"Gemini Lookout Alert: {gemini_reason}"
                                log.info(f"🚨 GEMINI WOKE CLAUDE [{symbol}]: {trigger_reason}")
                            elif action == "INVALIDATE":
                                log.info(f"🗑️ GEMINI INVALIDATED SETUP [{symbol}]: {gemini_reason} — Wiping memory.")
                                from ai.ai_memory import _tracking_setups, _wake_triggers
                                if symbol in _tracking_setups: del _tracking_setups[symbol]
                                if symbol in _wake_triggers: del _wake_triggers[symbol]
                                continue
                            elif action == "PANIC_CLOSE":
                                log.warning(f"🚨 GEMINI PANIC CLOSE [{symbol}]: {gemini_reason} — Forcing emergency bailout!")
                                from data.ctrader_client import get_open_positions, close_position
                                positions = yield get_open_positions(client)
                                sym_positions = [p for p in positions if p.get('symbol') == symbol]
                                for pos in sym_positions:
                                    yield close_position(client, pos["positionId"], int(pos["volume"] * 100))
                                    log.info(f"✅ Panic closed {pos['side']} position {pos['positionId']}.")
                                    if HAS_TELEGRAM_BOT:
                                        send_bot_message(f"🚨 *GEMINI EMERGENCY BAILOUT*\nInstantly closed {pos['side']} position for {symbol} due to violent momentum shift!\n_{gemini_reason}_")
                                from ai.ai_memory import clear_wake_triggers, _tracking_setups
                                clear_wake_triggers(symbol)
                                if symbol in _tracking_setups: del _tracking_setups[symbol]
                                continue

            if triggered:
                now = datetime.now(timezone.utc)
                if last_tripwire_time.get(symbol) is not None:
                    mins_since = (now - last_tripwire_time[symbol]).total_seconds() / 60
                    if mins_since < 15:
                        continue

                last_tripwire_time[symbol] = now
                price_triggered_symbols.add(symbol)

                context = triggers.get("context", "unknown")
                context_emoji = "📊" if context == "position_management" else "🎯"

                log.warning(f"{context_emoji} AI WAKE TRIGGER [{symbol}] ({context}): {trigger_reason}")

                if HAS_TELEGRAM_BOT:
                    ctx_label = "POSITION MGMT" if context == "position_management" else "ENTRY SETUP"
                    send_bot_message(
                        f"{context_emoji} *AI WAKE TRIGGER [{symbol}]*\n"
                        f"📋 Type: {ctx_label}\n"
                        f"{trigger_reason}\n"
                        f"Claude is being woken up NOW!"
                    )

                # Build context-aware wakeup message
                setup_reason = triggers.get("reason", "")[:200]
                if context == "position_management":
                    wakeup_msg = (
                        f"PRICE_TRIGGER [{symbol}]: {trigger_reason}. "
                        f"You have an OPEN POSITION and YOU set this wake trigger for position management. "
                        f"Your reason was: '{setup_reason}'. "
                        f"Evaluate immediately: should you CLOSE_TRADE, MOVE_SL_BE, PARTIAL_CLOSE, or continue holding?"
                    )
                else:
                    wakeup_msg = (
                        f"PRICE_TRIGGER [{symbol}]: {trigger_reason}. "
                        f"YOU set this wake trigger because your thesis was: '{setup_reason}'. "
                        f"Your trigger condition is NOW MET. You MUST follow through with a trade (BUY or SELL) "
                        f"unless the market structure has fundamentally changed since you set the trigger."
                    )

                reactor.callLater(0, analysis_cycle, symbol=symbol, wakeup_reason=wakeup_msg)

        except Exception as e:
            log.debug(f"Wake trigger check error ({symbol}): {e}")

    # ─── TRIPWIRE: Volatility / RSI / Wick Sweep detection ───
    TRIPWIRE_THRESHOLDS = {
        'XAUUSD': {'range_min': 0.80, 'sweep_wick': 1.00, 'abs_move': 1.50},
        'EURUSD': {'range_min': 0.00050, 'sweep_wick': 0.00080, 'abs_move': 0.0015},
        'USDJPY': {'range_min': 0.080, 'sweep_wick': 0.120, 'abs_move': 0.200},
        'BTCUSD': {'range_min': 80.0, 'sweep_wick': 120.0, 'abs_move': 200.0},
    }

    for symbol in config.TRADING_SYMBOLS:
        # Skip symbols already triggered by AI Wake Trigger above
        if symbol in price_triggered_symbols:
            continue
        try:
            from ta.momentum import RSIIndicator

            sym_agg = tick_aggregators.get(symbol)
            if sym_agg is None:
                continue

            candles_1m = sym_agg.get_candles(1)
            if candles_1m.empty or len(candles_1m) < 15:
                continue

            recent = candles_1m.iloc[-16:-1]
            latest_closed = recent.iloc[-1]
            previous_14 = recent.iloc[:-1]

            avg_vol = previous_14['volume'].mean()
            avg_range = (previous_14['high'] - previous_14['low']).mean()

            if avg_vol == 0 or avg_range == 0:
                continue

            current_vol = latest_closed['volume']
            current_range = latest_closed['high'] - latest_closed['low']

            vol_ratio = current_vol / avg_vol
            range_ratio = current_range / avg_range

            thresholds = TRIPWIRE_THRESHOLDS.get(symbol, TRIPWIRE_THRESHOLDS['XAUUSD'])

            trigger_vol = (vol_ratio >= 3.0 and current_vol > 50)
            trigger_range = (range_ratio >= 3.0 and current_range >= thresholds['range_min'])
            trigger_abs = (current_range >= thresholds['abs_move'])

            rsi_series = RSIIndicator(close=candles_1m["close"], window=14).rsi()
            current_rsi = rsi_series.iloc[-2] if not rsi_series.empty else 50
            trigger_rsi_oversold = (current_rsi < 20)
            trigger_rsi_overbought = (current_rsi > 80)

            wick_up = latest_closed['high'] - max(latest_closed['open'], latest_closed['close'])
            wick_down = min(latest_closed['open'], latest_closed['close']) - latest_closed['low']
            total_length = latest_closed['high'] - latest_closed['low']
            trigger_sweep_bullish = (wick_down >= thresholds['sweep_wick'] and wick_down / total_length > 0.70) if total_length > 0 else False
            trigger_sweep_bearish = (wick_up >= thresholds['sweep_wick'] and wick_up / total_length > 0.70) if total_length > 0 else False

            if trigger_vol or trigger_range or trigger_abs or trigger_rsi_oversold or trigger_rsi_overbought or trigger_sweep_bullish or trigger_sweep_bearish:
                now = datetime.now(timezone.utc)

                if last_tripwire_time.get(symbol) is not None:
                    mins_since = (now - last_tripwire_time[symbol]).total_seconds() / 60
                    if mins_since < 30:  # Don't re-wake Claude within 30 min
                        continue

                last_tripwire_time[symbol] = now

                reasons = []
                if trigger_vol: reasons.append(f"Volume {vol_ratio:.1f}x")
                if trigger_range: reasons.append(f"Momentum {range_ratio:.1f}x")
                if trigger_abs: reasons.append(f"Large move ({current_range:.5g})")
                if trigger_rsi_oversold: reasons.append(f"RSI Capitulation ({current_rsi:.1f})")
                if trigger_rsi_overbought: reasons.append(f"RSI Exhaustion ({current_rsi:.1f})")
                if trigger_sweep_bullish: reasons.append("Bullish Wick Sweep")
                if trigger_sweep_bearish: reasons.append("Bearish Wick Sweep")
                reason_str = " | ".join(reasons)

                # ─── TRIPWIRE PRE-FILTER: Check M15 indicators before calling Claude ───
                sym_agg_15 = tick_aggregators.get(symbol)
                if sym_agg_15:
                    candles_15_check = sym_agg_15.get_candles(15)
                    if not candles_15_check.empty and len(candles_15_check) >= 20:
                        from data.indicators import calculate_all
                        trip_indicators = calculate_all(candles_15_check)
                        
                        trip_vol = trip_indicators.get("volume_ratio", 0)
                        trip_prev_vol = trip_indicators.get("prev_volume_ratio", 0)
                        trip_score = 0
                        
                        if trip_vol < 0.3 and trip_prev_vol < 0.3:
                            log.info(f"⚡ TRIPWIRE [{symbol}]: {reason_str} — BUT M15 volume {trip_vol:.2f}x (prev {trip_prev_vol:.2f}x) < 0.3x. Skipping Claude.")
                            continue
                        
                        trip_trend = trip_indicators.get("trend", "FLAT")
                        if trip_trend != "FLAT": trip_score += 15
                        if trip_indicators.get("adx", 0) > 20: trip_score += 10
                        if trip_vol > 0.5: trip_score += 10
                        if trip_indicators.get("sweep_detected", False): trip_score += 25
                        trip_rsi = trip_indicators.get("rsi", 50)
                        if trip_rsi < 35 or trip_rsi > 65: trip_score += 10
                        if abs(trip_indicators.get("macd_hist", 0)) > 1: trip_score += 10
                        
                        # Bonus points because Tripwire successfully detected a 1m momentum/volume spike
                        trip_score += 25
                        
                        # Dynamic tripwire threshold based on volume
                        dynamic_trip_threshold = 45
                        if trip_vol > 1.5:
                            dynamic_trip_threshold = 35  # Massive volume, lower the gate
                        elif trip_vol > 1.0:
                            dynamic_trip_threshold = 40  # Active market
                        elif trip_vol < 0.5:
                            dynamic_trip_threshold = 55  # Dead market, raise the gate
                            
                        if trip_score < dynamic_trip_threshold:
                            log.info(f"⚡ TRIPWIRE [{symbol}]: {reason_str} — BUT M15 score {trip_score}% < {dynamic_trip_threshold} (Dynamic). Skipping Claude.")
                            continue
                        
                        log.info(f"⚡ TRIPWIRE [{symbol}]: {reason_str} — M15 score {trip_score}% ≥ {dynamic_trip_threshold} (Dynamic). Calling Gemini Hunter...")
                        from ai.prompt_builder import format_for_claude
                        from trading.regime_manager import regime_manager
                        from twisted.internet import threads
                        from ai.gemini_client import gemini_rotator

                        # Build the exact same massive context that Claude uses
                        acct = getattr(bot_state, '_cached_balance', {'balance': 0})
                        acct['positions'] = [p for p in getattr(bot_state, '_cached_positions', []) if p.get('symbol') == symbol]
                        
                        tick_info = sym_agg.get_current_price()
                        
                        mtfa_data = {}
                        for tf in [5, 60, 240]:
                            c_tf = sym_agg.get_candles(tf)
                            if not c_tf.empty:
                                mtfa_ind = calculate_all(c_tf)
                                if mtfa_ind:
                                    mtfa_ind["latest_close"] = c_tf["close"].iloc[-1]
                                    mtfa_data[tf] = mtfa_ind
                        
                        dollar_strength = ""
                        if "XAU" in symbol or symbol == "BTCUSD":
                            try:
                                from data.correlation_engine import get_dollar_strength
                                dollar_strength = get_dollar_strength(tick_aggregators)
                            except Exception:
                                pass

                        sys_add, usr_data = format_for_claude(
                            candles_15m=candles_15_check,
                            indicators=trip_indicators,
                            account=acct,
                            candles_1m=candles_1m,
                            tick_info=tick_info,
                            mtfa_data=mtfa_data,
                            market_regime=regime_manager.current_regime,
                            ml_report=ml_report_cache,
                            symbol=symbol,
                            dollar_correlation=dollar_strength
                        )
                        full_context = f"{sys_add}\n\n{usr_data}"

                        hunter_decision = yield threads.deferToThread(
                            gemini_rotator.evaluate_hunter_setup,
                            symbol, full_context
                        )
                        if hunter_decision and hunter_decision.get("action") == "WAKE_CLAUDE":
                            gemini_reason = hunter_decision.get("reason", "Good setup")
                            log.warning(f"🎯 GEMINI HUNTER [{symbol}]: {gemini_reason}. Waking Claude!")
                            if HAS_TELEGRAM_BOT:
                                send_bot_message(f"🎯 *GEMINI HUNTER [" + symbol + "]*\n" + gemini_reason + "\nWaking Claude up!")
                            reactor.callLater(0, analysis_cycle, symbol=symbol, wakeup_reason=f"GEMINI HUNTER [{symbol}]: {gemini_reason}. {reason_str} detected. Evaluate immediately.")
                        else:
                            log.info(f"🛑 GEMINI HUNTER [{symbol}]: Passed on the setup. ({hunter_decision.get('reason', 'No reason')})")

        except Exception as e:
            log.error(f"Tripwire error ({symbol}): {e}")



# ═══════════════════════════════════════════════════════════════
# TIER 3: Local Position Monitor (every 15 min)
# ═══════════════════════════════════════════════════════════════

@defer.inlineCallbacks
def monitor_cycle():
    """Local monitoring cycle — checks positions, detects closes, no Claude call."""
    global _previous_positions, _last_wakeup_time, _profit_tiers_triggered
    global _loss_tiers_triggered, _auto_lock_tiers_triggered, _scale_out_triggered
    
    try:
        log_parts = []
        for sym in config.TRADING_SYMBOLS:
            t_agg = tick_aggregators.get(sym)
            if t_agg:
                info = t_agg.get_current_price()
                if info["bid"] > 0:
                    log_parts.append(f"{sym}: ${info['mid']:,.5g}")
        
        if not log_parts:
            log.debug("⏳ No ticks received for any symbol yet — waiting...")
            return

        monitor_str = " | ".join(log_parts)
        log.info(f"📡 Monitor | {monitor_str}")

        # Check open positions and balance
        positions = yield get_open_positions(client)
        current_position_ids = {pos["positionId"] for pos in positions}
        account = yield get_account_info(client)

        # Cache for Telegram commands
        if HAS_TELEGRAM_BOT:
            bot_state._cached_positions = positions
            bot_state._cached_balance = account
            
        # ─── Detect new positions (for Slippage Tracking) ─────
        new_ids = current_position_ids - set(_previous_positions.keys())
        if new_ids and bool(_previous_positions):  # Ignore on first loop
            from ai.ai_memory import _entry_theses, record_slippage
            for new_id in new_ids:
                pos = next((p for p in positions if p["positionId"] == new_id), None)
                if pos:
                    symbol = pos.get("symbol", config.TRADING_SYMBOL)
                    thesis = _entry_theses.get(symbol)
                    if thesis and thesis.get("entry_price"):
                        expected_entry = thesis["entry_price"]
                        actual_entry = pos.get("entryPrice", 0)
                        if actual_entry > 0:
                            slippage = abs(actual_entry - expected_entry)
                            log.info(f"🏦 Execution Slippage for {symbol}: Expected ${expected_entry:,.2f}, Filled ${actual_entry:,.2f} -> Slippage: ${slippage:.2f}")
                            record_slippage(symbol, slippage)

        # ─── Detect auto-closed positions (SL/TP hit) ─────────
        closed_ids = set(_previous_positions.keys()) - current_position_ids
        if closed_ids and _previous_positions:  # Only if we had previous data
            from trading.risk_manager import risk_state
            
            for closed_id in closed_ids:
                log.info(f"🏁 Position {closed_id} was closed (SL/TP hit)")
                pos = _previous_positions.get(closed_id)
                closed_sym = pos.get('symbol', config.TRADING_SYMBOL) if pos else config.TRADING_SYMBOL
                closed_tick = tick_aggregators.get(closed_sym, tick_agg).get_current_price()
                current_price = closed_tick["mid"]

                # ─── Close the Demo mirror position too ───
                try:
                    from data.ctrader_client import _demo_position_map, _demo_client, _demo_is_authenticated, _demo_account_id
                    demo_entry = _demo_position_map.get(closed_id)
                    demo_pos_id = None
                    demo_vol = 100  # fallback: 0.01 lot
                    
                    if demo_entry:
                        if isinstance(demo_entry, tuple):
                            demo_pos_id = demo_entry[0]
                            demo_vol = demo_entry[1] if demo_entry[1] > 0 else 100
                        else:
                            demo_pos_id = demo_entry
                    
                    if demo_pos_id and _demo_client and _demo_is_authenticated:
                        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAClosePositionReq
                        
                        demo_close_req = ProtoOAClosePositionReq()
                        demo_close_req.ctidTraderAccountId = _demo_account_id
                        demo_close_req.positionId = demo_pos_id
                        demo_close_req.volume = demo_vol
                        
                        _demo_client.send(demo_close_req)
                        log.info(f"🔗 Demo mirror position {demo_pos_id} close sent (vol: {demo_vol}, SL/TP auto-sync)")
                        
                        # Remove from map
                        _demo_position_map.pop(closed_id, None)
                    elif demo_pos_id:
                        log.warning(f"⚠️ Demo mirror {demo_pos_id} exists but demo client not ready")
                    else:
                        log.debug(f"No demo mirror for live position {closed_id}")
                except Exception as e:
                    log.error(f"⚠️ Failed to close demo mirror for {closed_id}: {e}")

                # Journal the close
                try:
                    pnl = log_trade_close(
                        str(closed_id), current_price, "SL/TP auto-hit"
                    )
                    pnl_str = f"${pnl:,.2f}" if pnl is not None else "unknown"
                    pnl_emoji = "🟢" if (pnl or 0) >= 0 else "🔴"
                    
                    if pnl is not None:
                        risk_state.update(pnl, account["balance"])
                        if pnl < 0:
                            from trading.risk_manager import record_loss
                            record_loss()
                        
                except Exception as e:
                    log.warning(f"Journal close failed: {e}")
                    pnl_str = "unknown"
                    pnl_emoji = "❓"

                # Notify via Telegram
                if HAS_TELEGRAM_BOT:
                    send_bot_message(
                        f"🏁 *TRADE CLOSED — SL/TP Hit*\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Position ID: `{closed_id}`\n"
                        f"{pnl_emoji} P&L: `{pnl_str}`\n"
                        f"📈 Market: `${current_price:,.2f}`\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"_Automatically detected by monitor_"
                    )

                send_alert(f"🏁 Position {closed_id} closed (SL/TP hit) | P&L: {pnl_str}")
                
                # Clear wake triggers for this symbol since the trade is closed
                from ai.ai_memory import clear_wake_triggers
                clear_wake_triggers(closed_sym)

        # Update tracking
        # Update tracking
        _previous_positions = {p["positionId"]: p for p in positions}
        for closed_id in list(_profit_tiers_triggered.keys()):
            if closed_id not in current_position_ids:
                del _profit_tiers_triggered[closed_id]
        for closed_id in list(_loss_tiers_triggered.keys()):
            if closed_id not in current_position_ids:
                del _loss_tiers_triggered[closed_id]
        for closed_id in list(_auto_lock_tiers_triggered.keys()):
            if closed_id not in current_position_ids:
                del _auto_lock_tiers_triggered[closed_id]
        for closed_id in list(_scale_out_triggered):
            if closed_id not in current_position_ids:
                _scale_out_triggered.remove(closed_id)

        # ─── WEEKEND GAP PROTECTION ───────────────
        if getattr(config, 'WEEKEND_CLOSE_ENABLED', False) and positions:
            now_utc = datetime.now(timezone.utc)
            # Friday is weekday() == 4. If hour >= 20 (8:00 PM UTC)
            if now_utc.weekday() == 4 and now_utc.hour >= 20:
                log.warning("🚨 WEEKEND GAP PROTECTION TRIGGERED! Closing all open trades.")
                from data.ctrader_client import close_position
                for pos in positions:
                    yield close_position(client, pos["positionId"], pos["volume"])
                
                if HAS_TELEGRAM_BOT:
                    send_bot_message("🛡️ *WEEKEND GAP PROTECTION*\nAll open trades have been closed automatically to prevent Monday gap risk. System will hold until market reopens.")
                
                # Positions are closed, return to skip the rest of the loop
                return

        # Log current positions
        if positions:
            for pos in positions:
                upnl = pos.get("unrealizedPnl", 0)
                pnl_emoji = "📈" if upnl >= 0 else "📉"
                log.info(
                    f"📍 Position: {pos['side']} {pos['symbol']} | "
                    f"Vol: {pos['volume']} | Entry: ${pos['entryPrice']:,.2f} | "
                    f"{pnl_emoji} uPnL: ${upnl:,.2f}"
                )

                # ─── AUTO-BREAKEVEN LOGIC (1:1 R:R) ─────────────
                entry = pos.get("entryPrice", 0)
                sl = pos.get("stopLoss", 0)
                tp = pos.get("takeProfit", 0)
                side = pos.get("side")
                # Get live price for THIS position's symbol
                pos_symbol = pos.get('symbol', config.TRADING_SYMBOL)
                pos_tick = tick_aggregators.get(pos_symbol, tick_agg).get_current_price()
                current_price = pos_tick["mid"]
                
                if current_price > 0 and entry > 0 and sl > 0:
                    risk = abs(entry - sl)
                    should_scale_out = False
                    
                    if side == "BUY" and sl < entry:
                        if current_price >= entry + risk:
                            should_scale_out = True
                    elif side == "SELL" and sl > entry:
                        if current_price <= entry - risk:
                            should_scale_out = True
                            
                    pos_id_key = pos.get('positionId', 0)
                    if should_scale_out and pos_id_key not in _scale_out_triggered:
                        log.info(f"🛡️ 1:1 R:R REACHED for position {pos_id_key}! Scaling out 50% & Auto-Breakeven.")
                        from data.ctrader_client import amend_position_sltp, close_position
                        
                        # Calculate half volume (cTrader typically allows steps, divide by 2)
                        current_vol = pos.get('volume', 0)
                        half_vol = current_vol // 2
                        
                        if half_vol > 0:
                            yield close_position(client, pos_id_key, half_vol)
                        
                        buffer = getattr(config, 'BREAKEVEN_BUFFER', 0.50)
                        new_sl = entry + buffer if side == 'BUY' else entry - buffer
                        # Use defer.ensureDeferred to run the async amend within the loop
                        yield amend_position_sltp(client, pos_id_key, new_sl, tp, symbol_name=pos.get("symbol"))
                        _scale_out_triggered.add(pos_id_key)
                        
                        if HAS_TELEGRAM_BOT:
                            send_bot_message(
                                f"🛡️ *RISK-FREE SECURED*\n"
                                f"Position: {side} @ ${entry:,.2f}\n"
                                f"Price reached 1:1 R:R (${current_price:,.2f})\n"
                                f"• Closed 50% of position in profit.\n"
                                f"• Stop Loss moved to Entry (${new_sl:,.2f})."
                            )
                            
                # ─── WAKEUP LOGIC (Danger Zone) ─────────────
                if getattr(config, 'DANGER_ZONE_PCT', 0) > 0 and entry > 0:
                    danger_pct = config.DANGER_ZONE_PCT
                    now = time.time()
                    cooldown = getattr(config, 'WAKEUP_COOLDOWN_MINUTES', 15) * 60
                    
                    if (now - _last_wakeup_time) > cooldown:
                        trigger_wakeup = False
                        
                        # Calculate original risk for percentage
                        orig_risk = abs(entry - sl) if sl > 0 else 0
                        
                        # Check SL Danger
                        if sl > 0 and orig_risk > 0:
                            dist_to_sl = abs(current_price - sl)
                            if dist_to_sl / orig_risk <= danger_pct:
                                trigger_wakeup = True
                                log.warning(f"🚨 WAKEUP: Price is within {danger_pct*100}% of Stop Loss!")
                        
                        # Check TP Danger (Evaluate early exit)
                        if not trigger_wakeup and tp > 0:
                            total_reward = abs(tp - entry)
                            dist_to_tp = abs(tp - current_price)
                            if total_reward > 0 and dist_to_tp / total_reward <= danger_pct:
                                trigger_wakeup = True
                                log.warning(f"🚨 WAKEUP: Price is within {danger_pct*100}% of Take Profit!")
                                
                        if trigger_wakeup:
                            _last_wakeup_time = now
                            if HAS_TELEGRAM_BOT:
                                send_bot_message("🚨 *GUARD DOG WAKEUP*\nPrice entered Danger Zone (near SL/TP). Forcing Claude to evaluate immediately!")
                            
                            # Fire off analysis cycle asynchronously with reason
                            reactor.callLater(0, analysis_cycle, symbol=pos_symbol, wakeup_reason="DANGER ZONE: Price is within 15% of SL or TP. Check if we should exit early to protect capital.")

                # ─── TIERED PROFIT PROTECTION ─────────────
                # Wake Claude at 30%, 40%, 50%, 60%, 70%, 80% of TP distance.
                # Each tier triggers ONCE per position. Under 20% = wait for BE.
                # Above 80% = let TP hit naturally.
                pos_id_key = pos.get('positionId', 0)
                
                if entry > 0 and tp > 0:
                    total_reward = abs(tp - entry)
                    
                    # Calculate how much profit we've captured
                    if side == "BUY":
                        profit_captured = current_price - entry
                    else:
                        profit_captured = entry - current_price
                    
                    # Only trigger when in profit
                    if profit_captured > 0 and total_reward > 0:
                        profit_ratio = profit_captured / total_reward
                        
                        # Get ATR for buffer (default 2.0 if not available)
                        atr = 2.0
                        if HAS_TELEGRAM_BOT and hasattr(bot_state, 'last_indicators') and bot_state.last_indicators:
                            ind = bot_state.last_indicators.get(pos_symbol, {})
                            if 'atr' in ind:
                                atr = ind['atr']
                        atr_buffer = atr * 0.5  # Give half an ATR of breathing room
                        
                        # 1. ─── AUTO-LOCK LOGIC ─────────────
                        if pos_id_key not in _auto_lock_tiers_triggered:
                            _auto_lock_tiers_triggered[pos_id_key] = set()
                        triggered_lock = _auto_lock_tiers_triggered[pos_id_key]
                        
                        highest_lock_tier = 0
                        for tier in _AUTO_LOCK_TIERS:
                            if profit_ratio >= tier and tier not in triggered_lock:
                                highest_lock_tier = tier
                                
                        if highest_lock_tier > 0:
                            for t in _AUTO_LOCK_TIERS:
                                if t <= highest_lock_tier:
                                    triggered_lock.add(t)
                            
                            # Lock in profit with ATR buffer
                            new_sl = sl
                            should_partial_close = False
                            locked_pct_msg = ""
                            
                            if highest_lock_tier == 0.50:
                                # 50% Tier: Breakeven + Partial Close
                                if side == "BUY":
                                    potential_sl = entry + atr_buffer
                                    if sl == 0 or potential_sl > sl:
                                        new_sl = potential_sl
                                else:
                                    potential_sl = entry - atr_buffer
                                    if sl == 0 or potential_sl < sl:
                                        new_sl = potential_sl
                                should_partial_close = True
                                locked_pct_msg = "Breakeven"
                                
                            elif highest_lock_tier == 0.75:
                                # 75% Tier: Lock 50% profit
                                locked_dist = 0.50 * total_reward
                                if side == "BUY":
                                    potential_sl = entry + locked_dist - atr_buffer
                                    if sl == 0 or potential_sl > sl:
                                        new_sl = potential_sl
                                else:
                                    potential_sl = entry - locked_dist + atr_buffer
                                    if sl == 0 or potential_sl < sl:
                                        new_sl = potential_sl
                                locked_pct_msg = "50%"
                                    
                            if should_partial_close and pos.get('volume', 0) > 0:
                                half_vol = pos['volume'] // 2
                                if half_vol > 0:
                                    log.info(f"✂️ Taking 50% profit out (Volume: {half_vol}) at 50% TP distance!")
                                    from data.ctrader_client import close_position
                                    yield close_position(client, pos["positionId"], half_vol)
                                    if HAS_TELEGRAM_BOT:
                                        send_bot_message(f"✂️ *Partial Close:* Closed 50% of position {pos['positionId']} to secure profits!")

                            if new_sl != sl:
                                log.info(f"🔒 TIER LOCK: Moving SL to ${new_sl:,.2f} (Buffered) to guarantee {locked_pct_msg} on position {pos['positionId']}")
                                from data.ctrader_client import amend_position_sltp
                                yield amend_position_sltp(client, pos["positionId"], new_sl, tp, symbol_name=pos.get("symbol"))
                                sl = new_sl  # Update local var
                                if HAS_TELEGRAM_BOT:
                                    send_bot_message(f"🔒 *Auto-Lock:* SL moved to `${new_sl:,.2f}` (Guarantees {locked_pct_msg} profit with ATR buffer)")

                        # 2. ─── CLAUDE WAKEUP LOGIC ─────────────
                        if pos_id_key not in _profit_tiers_triggered:
                            _profit_tiers_triggered[pos_id_key] = set()
                        triggered = _profit_tiers_triggered[pos_id_key]
                        
                        highest_tier_triggered = 0
                        for tier in _PROFIT_TIERS:
                            if profit_ratio >= tier and tier not in triggered:
                                highest_tier_triggered = tier
                                
                        if highest_tier_triggered > 0:
                            for t in _PROFIT_TIERS:
                                if t <= highest_tier_triggered:
                                    triggered.add(t)
                                    
                            pct_display = int(highest_tier_triggered * 100)
                            profit_dollars = round(profit_captured * pos.get('volume', 0), 2)
                            
                            urgency = "💰" if pct_display < 70 else "🔥"
                            msg_tone = "Significant profit — evaluate exit" if pct_display < 70 else "STRONG PROFIT — protect gains!"
                            
                            log.info(f"{urgency} PROFIT TIER {pct_display}%: ~${profit_dollars:,.2f} profit — Waking Claude!")
                            
                            if HAS_TELEGRAM_BOT:
                                send_bot_message(
                                    f"{urgency} *PROFIT PROTECTION — {pct_display}% TIER*\n"
                                    f"━━━━━━━━━━━━━━━━━━\n"
                                    f"{msg_tone}\n"
                                    f"📈 Profit: ~`${profit_dollars:,.2f}`\n"
                                    f"📍 Price: `${current_price:,.2f}`\n"
                                    f"🎯 TP: `${tp:,.2f}` ({int(profit_ratio*100)}% reached)\n"
                                    f"━━━━━━━━━━━━━━━━━━\n"
                                    f"_Claude deciding: HOLD / PARTIAL CLOSE / TAKE PROFIT_"
                                )
                            
                            reason_msg = f"PROFIT PROTECTION TIER HIT: Trade is {pct_display}% of the way to TP. We are in profit. Please evaluate if we should TAKE PROFIT now, PARTIAL CLOSE, or if momentum is strong enough to HOLD for the remaining {100-pct_display}%."
                            reactor.callLater(0, analysis_cycle, symbol=pos_symbol, wakeup_reason=reason_msg)
                            
                    # ─── TIERED LOSS PROTECTION ─────────────
                    # Wake Claude at 40%, 60%, 80% of SL distance (drawdown)
                    elif profit_captured < 0 and sl > 0:
                        orig_risk = abs(entry - sl)
                        if orig_risk > 0:
                            drawdown_ratio = abs(profit_captured) / orig_risk
                            
                            # Initialize tier tracking for this position
                            if pos_id_key not in _loss_tiers_triggered:
                                _loss_tiers_triggered[pos_id_key] = set()
                            
                            triggered_loss = _loss_tiers_triggered[pos_id_key]
                            
                            # Find the highest tier we've reached
                            highest_loss_tier = 0
                            for tier in _LOSS_TIERS:
                                if drawdown_ratio >= tier and tier not in triggered_loss:
                                    highest_loss_tier = tier
                                    
                            if highest_loss_tier > 0:
                                # Add this tier and lower tiers
                                for t in _LOSS_TIERS:
                                    if t <= highest_loss_tier:
                                        triggered_loss.add(t)
                                        
                                pct_display = int(highest_loss_tier * 100)
                                loss_dollars = round(abs(profit_captured) * pos.get('volume', 0), 2)
                                
                                urgency = "⚠️" if pct_display < 80 else "🚨"
                                msg_tone = "Trade is in drawdown. Evaluate structural integrity." if pct_display < 80 else "CRITICAL DRAWDOWN. Evaluate if setup is completely invalidated."
                                
                                log.warning(f"{urgency} LOSS TIER {pct_display}%: ~${loss_dollars:,.2f} drawdown — Waking Claude for deep analysis!")
                                
                                if HAS_TELEGRAM_BOT:
                                    send_bot_message(
                                        f"{urgency} *LOSS PROTECTION — {pct_display}% TIER*\n"
                                        f"━━━━━━━━━━━━━━━━━━\n"
                                        f"{msg_tone}\n"
                                        f"📉 Drawdown: ~`-${loss_dollars:,.2f}`\n"
                                        f"📍 Price: `${current_price:,.2f}`\n"
                                        f"🛡️ SL: `${sl:,.2f}` ({int(drawdown_ratio*100)}% to stop out)\n"
                                        f"━━━━━━━━━━━━━━━━━━\n"
                                        f"_Claude deciding: HOLD (if valid) / CLOSE_TRADE (if invalid)_"
                                    )
                                
                                # Force Claude to evaluate immediately with reason
                                reason_msg = f"LOSS PROTECTION TIER HIT: Trade is {pct_display}% of the way to Stop Loss. We are in drawdown. Perform deep structural analysis. If the setup is completely invalidated (e.g. key levels broken, momentum shifted), recommend CLOSE_TRADE now to save capital. If the setup is still structurally valid and this is just a normal pullback, recommend HOLD."
                                reactor.callLater(0, analysis_cycle, symbol=pos_symbol, wakeup_reason=reason_msg)
                                
                                # ─── Record Drawdown Event ────────────────────────
                                if highest_loss_tier >= 0.60:
                                    try:
                                        from ai.ai_memory import record_drawdown
                                        from trading.regime_manager import _current_regimes
                                        current_regime = _current_regimes.get(pos_symbol, "UNKNOWN")
                                        record_drawdown(pos_symbol, pct_display, current_regime)
                                    except Exception as e:
                                        log.warning(f"Failed to record drawdown memory: {e}")
                            
                # ─── TRAILING STOP LOGIC ─────────────
                trailing_activation = getattr(config, 'TRAILING_ACTIVATION', 10.0)
                trailing_distance = getattr(config, 'TRAILING_STOP_DISTANCE', 5.0)
                
                if entry > 0:
                    should_trail = False
                    new_trail_sl = sl
                    
                    if side == "BUY" and (current_price - entry) >= trailing_activation:
                        potential_sl = current_price - trailing_distance
                        if sl == 0 or potential_sl > sl:  # Move SL strictly up
                            should_trail = True
                            new_trail_sl = potential_sl
                            
                    elif side == "SELL" and (entry - current_price) >= trailing_activation:
                        potential_sl = current_price + trailing_distance
                        if sl == 0 or potential_sl < sl:  # Move SL strictly down
                            should_trail = True
                            new_trail_sl = potential_sl
                            
                    if should_trail:
                        log.info(f"📈 TRAILING STOP TRIGGERED: Trailing SL to ${new_trail_sl:,.2f} for position {pos['positionId']}!")
                        from data.ctrader_client import amend_position_sltp
                        yield amend_position_sltp(client, pos["positionId"], new_trail_sl, tp, symbol_name=pos.get("symbol"))
                        # We do NOT send a Telegram message here to avoid spamming the user every 5 minutes as it trails.

        else:
            log.debug("No open positions")

        # Update Telegram status
        if HAS_TELEGRAM_BOT:
            for sym, agg in tick_aggregators.items():
                bot_state.last_indicators[sym] = agg.get_current_price()

    except Exception as e:
        # (5, 'Deferred') is a harmless Twisted timing conflict when monitor
        # and analysis cycles overlap — silently skip, it resolves next cycle
        err_str = str(e)
        if "Deferred" in err_str:
            log.debug(f"Monitor skipped (busy): {err_str}")
        else:
            log.error(f"Monitor cycle error: {e}")


# ═══════════════════════════════════════════════════════════════
# STARTUP & CONNECTION
# ═══════════════════════════════════════════════════════════════

@defer.inlineCallbacks
def on_connected(c):
    """Called when TCP connection to cTrader is established."""
    global client, _analysis_loop, _monitor_loop
    client = c

    log.info("🔗 Connected to cTrader!")

    try:
        # Step 1: Authenticate
        yield authenticate_app(c)
        yield authenticate_account(c)

        # Step 2: Get account info
        account = yield get_account_info(c)
        log.info(f"💰 Live balance: ${account['balance']:,.2f}")

        # Step 3: Load symbols
        yield get_symbol_list(c)

        # Step 4: Get symbol details and subscribe for ALL trading symbols
        for sym in config.TRADING_SYMBOLS:
            symbol_id = _symbol_cache.get(sym)
            if symbol_id:
                yield get_symbol_details(c, symbol_id)
                log.info(f"✅ {sym} ready (ID: {symbol_id})")
            else:
                log.warning(f"⚠️ {sym} not found in broker symbol list — skipping")
                continue

            # Load historical candles from DEMO server (the data the AI is calibrated on)
            log.info(f"📊 Loading historical candles for {sym} from Demo...")
            raw_candles = yield get_demo_trendbars(sym, period_minutes=15, count=200)
            if raw_candles:
                tick_aggregators[sym].load_historical(raw_candles, timeframe=15)
                last_close = raw_candles[-1]["close"]
                log.info(f"📊 {sym} Demo historical data loaded | Last close: {last_close:,.5g}")
            else:
                # Fallback to live candles if demo isn't ready yet
                log.info(f"📊 Demo not ready, loading {sym} candles from Live...")
                raw_candles = yield get_trendbars(c, sym, period_minutes=15, count=200)
                if raw_candles:
                    tick_aggregators[sym].load_historical(raw_candles, timeframe=15)
                    last_close = raw_candles[-1]["close"]
                    log.info(f"📊 {sym} Live historical data loaded (fallback) | Last close: {last_close:,.5g}")

            # Subscribe to LIVE ticks (for offset tracking and position P&L)
            yield subscribe_to_prices(c, sym)
            log.info(f"📡 Subscribed to {sym} live ticks (for offset tracking)")

        # Step 7: Start Telegram bot with callbacks
        if HAS_TELEGRAM_BOT and config.TELEGRAM_BOT_TOKEN:
            log.info("🤖 Starting Telegram bot...")

            # Wire callbacks so Telegram commands trigger real actions
            bot_state.on_analyze = lambda: analysis_cycle(wakeup_reason="FORCED_BY_USER")
            bot_state.on_force_trade = lambda: analysis_cycle(wakeup_reason="FORCED_BY_USER")

            @defer.inlineCallbacks
            def _close_all():
                from data.ctrader_client import close_position
                positions = yield get_open_positions(c)
                for pos in positions:
                    yield close_position(c, pos["positionId"], int(pos["volume"] * 100))
                    log.info(f"🔒 Closed position {pos['positionId']}")
                    try:
                        log_trade_close(pos["positionId"], pos.get("currentPrice", 0), "Manual close via Telegram")
                    except Exception:
                        pass
                send_bot_message(f"✅ Closed {len(positions)} position(s)")

            bot_state.on_close_all = lambda: _close_all()

            def _get_positions_sync():
                # This is called from Telegram thread — return cached data
                return getattr(bot_state, '_cached_positions', [])

            def _get_balance_sync():
                return getattr(bot_state, '_cached_balance', {'balance': 0})

            bot_state.on_get_positions = _get_positions_sync
            bot_state.on_get_balance = _get_balance_sync

            # Cache initial data
            bot_state._cached_balance = account

            start_telegram_bot()

            import time
            time.sleep(1)  # Give bot time to initialize

            send_bot_message(
                f"🤖 *Emy AI Trading System Started*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📊 Symbols: `{', '.join(config.TRADING_SYMBOLS)}`\n"
                f"🧠 Model: `{config.CLAUDE_MODEL}`\n"
                f"⏱️ Analysis: `Every {config.ANALYSIS_INTERVAL_MINUTES}min`\n"
                f"📡 Monitor: `Every {config.MONITOR_INTERVAL_MINUTES}min`\n"
                f"💰 Balance: `${account['balance']:,.2f}`\n\n"
                f"Use /help to see commands"
            )

        send_alert(
            f"🤖 Emy AI Trading System Started\n"
            f"Mode: Analyze Demo → Execute Live\n"
            f"Broker: cTrader {config.CTRADER_HOST.upper()}\n"
            f"Symbols: {', '.join(config.TRADING_SYMBOLS)}\n"
            f"AI: {config.CLAUDE_MODEL} (every {config.ANALYSIS_INTERVAL_MINUTES}min)\n"
            f"Live Balance: ${account['balance']:,.2f}"
        )

        config.print_config_summary()
        log.info("🧠 AI Brain: Demo data feed (profitable data)")
        log.info("💰 Execution: Live Prop Firm account")
        log.info("📐 SL/TP auto-adjusted by price offset between feeds")
        log.info("✅ All systems go!\n")

        # Step 8: Run initial ML analysis and Claude analysis
        log.info("🧠 Running initial ML edge analysis...")
        update_ml_report()
        
        @defer.inlineCallbacks
        def master_analysis_cycle():
            for sym in config.TRADING_SYMBOLS:
                try:
                    yield analysis_cycle(symbol=sym)
                except Exception as e:
                    log.error(f"Analysis cycle failed for {sym}: {e}")

        log.info("🧠 Running initial Claude analysis across all symbols...")
        yield master_analysis_cycle()

        # Step 9: Start scheduled loops
        _ml_loop = task.LoopingCall(update_ml_report)
        _ml_loop.start(43200, now=False)  # Every 12 hours (12 * 60 * 60)
        log.info("🧠 ML Analyzer loop: every 12 hours")

        # Claude no longer runs on a timer. He sleeps until Gemini wakes him.
        log.info("🧠 Claude Analyzer: SLEEPING (Sniper Mode - awaiting Gemini)")

        def _update_progress_bar():
            import sys
            import time
            if getattr(bot_state, 'is_paused', False):
                sys.stdout.write("\r\033[K" + "🛌 Dead Zone Active: Bot is hibernating...")
                sys.stdout.flush()
                return
            syms = len(config.TRADING_SYMBOLS)
            elapsed = int(time.time() % 30)
            next_scan = 30 - elapsed
            bar = '█' * elapsed + '░' * (30 - elapsed)
            sys.stdout.write("\r\033[K" + f"⚡ Gemini Scout LIVE: Scanning {syms} pairs [{bar}] {next_scan}s")
            sys.stdout.flush()

        _pb_loop = task.LoopingCall(_update_progress_bar)
        _pb_loop.start(1.0, now=False)

        _dz_loop = task.LoopingCall(dead_zone_cycle)
        _dz_loop.start(60.0, now=True)
        log.info(f"🛌 Dead Zone watcher: {config.DEAD_ZONE_START} to {config.DEAD_ZONE_END}")

        _monitor_loop = task.LoopingCall(monitor_cycle)
        _monitor_loop.start(config.MONITOR_INTERVAL_MINUTES * 60, now=False)
        log.info(f"📡 Monitor loop: every {config.MONITOR_INTERVAL_MINUTES} min")

        _tripwire_loop = task.LoopingCall(tripwire_cycle)
        _tripwire_loop.start(30, now=False)
        log.info("⚡ Tripwire loop: every 30 seconds")

        log.info("🚀 System is LIVE! Collecting ticks and analyzing...\n")

    except Exception as e:
        log.error(f"Startup failed: {e}")
        import traceback
        traceback.print_exc()
        reactor.stop()


def on_disconnected(c, reason):
    """Handle disconnection."""
    log.warning(f"🔌 Disconnected from cTrader: {reason}")
    
    # Rate limit Telegram alerts to once every 15 minutes to avoid spam
    now_utc = datetime.now(timezone.utc)
    if not hasattr(on_disconnected, "last_msg_time") or (now_utc - on_disconnected.last_msg_time).total_seconds() > 900:
        send_alert("⚠️ cTrader disconnected — restarting to auto-recover...")
        if HAS_TELEGRAM_BOT:
            send_bot_message("⚠️ *cTrader disconnected* — restarting process to auto-recover...")
        on_disconnected.last_msg_time = now_utc

    # Stop the reactor so start.sh can fully restart the app
    try:
        from twisted.internet import reactor
        if reactor.running:
            reactor.stop()
    except BaseException:
        pass


def shutdown(signum=None, frame=None):
    """Graceful shutdown handler."""
    log.info("\n🛑 Shutting down Emy AI Trading System...")

    stats = get_usage_stats()
    log.info(
        f"📊 Session summary: {stats['total_calls']} Claude calls | "
        f"${stats['estimated_cost']:.4f} total cost"
    )

    send_alert("🛑 Emy AI Trading System stopped.")
    if HAS_TELEGRAM_BOT:
        send_bot_message(
            f"🛑 *System stopped*\n"
            f"Claude calls: {stats['total_calls']} | Cost: ${stats['estimated_cost']:.4f}"
        )

    if reactor.running:
        reactor.stop()


def main():
    """Main entry point."""
    # Register shutdown handlers
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ─── Load AI Memory from Disk ───
    try:
        from ai.ai_memory import load_from_disk
        load_from_disk()
    except Exception as e:
        log.error(f"Failed to load AI memory: {e}")


    log.info("🚀 Starting Emy AI Trading System...")
    log.info(f"   Broker: cTrader ({config.CTRADER_HOST})")
    log.info(f"   Model: {config.CLAUDE_MODEL}")
    log.info(f"   Analysis: every {config.ANALYSIS_INTERVAL_MINUTES} min")
    log.info(f"   Monitor: every {config.MONITOR_INTERVAL_MINUTES} min")

    # Validate config
    config.validate_config()

    # Create Live cTrader client
    host = EndPoints.PROTOBUF_LIVE_HOST if config.CTRADER_HOST == "live" else EndPoints.PROTOBUF_DEMO_HOST
    ctrader = Client(host, EndPoints.PROTOBUF_PORT, FixedTcpProtocol)

    # Set callbacks for Live client
    ctrader.setConnectedCallback(on_connected)
    ctrader.setDisconnectedCallback(on_disconnected)
    ctrader.setMessageReceivedCallback(handle_message)
    ctrader.startService()

    # Create Demo cTrader client if Dual Execution is enabled
    if config.DUAL_EXECUTION_ENABLED:
        log.info("⚙️  Dual Execution Enabled — Analyze Demo, Execute Live")
        log.info("   🧠 AI Brain: Demo data feed (calibrated data)")
        log.info("   💰 Execution: Live Prop Firm account")
        from data.ctrader_client import set_demo_client, on_demo_connected
        ctrader_demo = Client(EndPoints.PROTOBUF_DEMO_HOST, EndPoints.PROTOBUF_PORT, FixedTcpProtocol)
        ctrader_demo.setConnectedCallback(lambda c: on_demo_connected(c))
        ctrader_demo.setMessageReceivedCallback(handle_demo_message)  # Demo ticks feed the AI brain
        ctrader_demo.startService()
        set_demo_client(ctrader_demo)

    log.info("🔗 Connecting to cTrader...\n")
    reactor.run()


if __name__ == "__main__":
    main()
