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

import config
from utils.logger import log
from utils.notifier import send_alert, send_error_alert
from data.ctrader_client import (
    authenticate_app, authenticate_account,
    get_account_info, get_symbol_list, get_symbol_details,
    get_trendbars, get_open_positions, subscribe_to_prices,
    place_market_order, _symbol_cache, _current_price,
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
_previous_position_ids = set()  # Track positions to detect closes
_last_wakeup_time = 0.0  # Track when we last forced Claude to wake up
_profit_tiers_triggered = {}  # Track which profit tiers have been triggered per position
_loss_tiers_triggered = {}  # Track which loss tiers have been triggered per position
_auto_lock_tiers_triggered = {}  # Track auto-lock executions

# Profit protection tiers: Claude wakes at each milestone (once per tier)
_PROFIT_TIERS = [0.50, 0.75]
# Loss protection tiers: Claude wakes to evaluate deep structural invalidation
_LOSS_TIERS = [0.50, 0.75]
# Auto-Lock tiers: Mechanically trail Stop Loss silently
_AUTO_LOCK_TIERS = [0.40, 0.50, 0.60, 0.70]


# ═══════════════════════════════════════════════════════════════
# TIER 1: Real-Time Tick Handler
# ═══════════════════════════════════════════════════════════════

def handle_message(c, msg):
    """Handle all incoming cTrader messages including spot events."""
    extracted = Protobuf.extract(msg)
    msg_type = type(extracted).__name__

    # Handle spot (tick) events
    if msg_type == "ProtoOASpotEvent":
        symbol_id = extracted.symbolId
        digits = 2

        # Find symbol name
        symbol_name = None
        for name, sid in _symbol_cache.items():
            if sid == symbol_id:
                symbol_name = name
                break

        if symbol_name and symbol_name in tick_aggregators:
            bid = extracted.bid / 100000.0 if hasattr(extracted, "bid") and extracted.bid else 0
            ask = extracted.ask / 100000.0 if hasattr(extracted, "ask") and extracted.ask else 0

            if bid > 0 and ask > 0:
                tick_aggregators[symbol_name].on_tick(bid, ask)

                # Log every 100th tick to avoid spam
                if tick_aggregators[symbol_name].tick_count % 100 == 0:
                    log.debug(f"📡 {symbol_name} Tick #{tick_aggregators[symbol_name].tick_count}: ${(bid+ask)/2:,.2f}")


# ═══════════════════════════════════════════════════════════════
# TIER 2: Claude AI Analysis (every 2 hours)
# ═══════════════════════════════════════════════════════════════

@defer.inlineCallbacks
def analysis_cycle(wakeup_reason=None, symbol=None):
    """Full Claude AI analysis cycle — runs every ANALYSIS_INTERVAL_MINUTES."""
    # Resolve target symbol
    target_symbol = symbol or config.TRADING_SYMBOL
    symbol_agg = tick_aggregators.get(target_symbol, tick_agg)

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

        # ─── Step 1: Check cooldown timer ─────────────────────
        if check_cooldown():
            log.info("⏳ Cooldown active — skipping analysis")
            return

        # ─── Step 1.5: Global trade cap ───────────────────────
        _cap_positions = yield get_open_positions(client)
        if len(_cap_positions) >= config.MAX_OPEN_TRADES:
            log.info(f"🚫 Max {config.MAX_OPEN_TRADES} trades open — skipping new analysis for {target_symbol}")
            if HAS_TELEGRAM_BOT:
                send_bot_message(f"🚫 *Trade cap reached* ({config.MAX_OPEN_TRADES}/{config.MAX_OPEN_TRADES})\nSkipping {target_symbol} analysis.")
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
                log.debug(f"📊 Insufficient {tf}m candles for {target_symbol} — fetching from cTrader API...")
                # Map minutes to ProtoOATrendbarPeriod if needed, get_trendbars handles this
                raw_candles = yield get_trendbars(
                    client, target_symbol,
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
            bot_state.last_indicators = main_indicators

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
        open_positions = yield get_open_positions(client)
        account["positions"] = open_positions
        if open_positions:
            log.info(f"📍 {len(open_positions)} open position(s) — Claude will evaluate")
        else:
            log.debug("📍 No open positions — looking for new entries")

        # ─── Step 5: Check drawdown ───────────────────────────
        if check_drawdown(account["balance"]):
            log.warning("🚨 Drawdown limit hit — skipping analysis")
            return

        # ─── Step 6: Format prompt & call Claude ──────────────
        # Get Streak and Daily PNL for AI Context
        from trading.risk_manager import risk_state
        from data.trade_journal import get_stats
        
        try:
            stats = get_stats()
            today_pnl = stats.get("today_pnl", 0.0)
        except Exception:
            today_pnl = 0.0

        prompt = format_for_claude(
            candles_15m, main_indicators, account,
            candles_1m=candles_1m if not candles_1m.empty else None,
            tick_info=tick_info if tick_info["bid"] > 0 else None,
            mtfa_data=mtfa_data,
            market_regime=market_regime,
            ml_report=ml_report_cache,
            wakeup_reason=wakeup_reason,
            streak_count=risk_state.streak_count,
            daily_pnl=today_pnl,
            symbol=target_symbol
        )

        log.debug(f"🧠 Sending data to {config.CLAUDE_MODEL}...")
        decision = get_trading_decision(prompt)

        log.info(f"🧠 Claude Decision: {decision['action']}")
        log.info(f"🎯 Confidence:    {decision.get('confidence', 'N/A')}%")
        log.info(f"📝 Reason:        {decision.get('reason', 'N/A')}")

        # Log usage stats
        stats = get_usage_stats()
        log.info(
            f"📊 Session stats: {stats['total_calls']} calls | "
            f"${stats['estimated_cost']:.4f} spent"
        )

        if HAS_TELEGRAM_BOT:
            bot_state.update_cycle(decision=decision, indicators=indicators)

        # ─── Step 7: Validate decision ────────────────────────
        has_position = bool(open_positions)
        if not validate_decision(decision, has_position=has_position, market_regime=market_regime):
            log.warning("❌ Decision validation failed — skipping")
            return

        if decision["action"] == "HOLD":
            log.info("⏸️  Final action: HOLD — no action this cycle")
            if HAS_TELEGRAM_BOT:
                safe_reason = str(decision.get('reason', 'N/A')).replace("_", "-")
                send_bot_message(
                    f"⏸️ *HOLD* — Confidence: {decision.get('confidence')}%\n"
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
                    current_price = tick_agg.get_current_price().get("mid", pos["entryPrice"])
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
            else:
                log.warning("CLOSE_TRADE but no open positions")
            return

        elif decision["action"] == "MOVE_SL_BE":
            if positions:
                from data.ctrader_client import amend_position_sltp
                for pos in positions:
                    entry = pos["entryPrice"]
                    existing_sl = float(pos.get("stopLoss", 0))
                    buffer = getattr(config, 'BREAKEVEN_BUFFER', 0.50)
                    
                    if pos['side'] == 'BUY':
                        new_sl = entry + buffer
                        # Prevent moving a deeply profitable SL backwards to breakeven
                        if existing_sl > 0 and existing_sl >= new_sl:
                            log.info(f"⏭️ Skipping MOVE_SL_BE: Current SL (${existing_sl:,.2f}) is already better than breakeven (${new_sl:,.2f})")
                            continue
                    else:
                        new_sl = entry - buffer
                        # Prevent moving a deeply profitable SL backwards to breakeven
                        if existing_sl > 0 and existing_sl <= new_sl:
                            log.info(f"⏭️ Skipping MOVE_SL_BE: Current SL (${existing_sl:,.2f}) is already better than breakeven (${new_sl:,.2f})")
                            continue
                    
                    existing_tp = pos.get("takeProfit", 0) or decision.get("take_profit", 0)
                    log.info(f"🛡️ Moving SL to breakeven (${new_sl:,.2f}) for position {pos['positionId']} — keeping TP ${existing_tp}")
                    yield amend_position_sltp(client, pos["positionId"], new_sl, existing_tp)
                    log.info(f"✅ SL moved to breakeven!")

                    if HAS_TELEGRAM_BOT:
                        safe_reason = str(decision.get('position_action_reason', 'Trade protected')).replace("_", "-")
                        send_bot_message(
                            f"🛡️ *SL → BREAKEVEN*\n"
                            f"Position: {pos['side']} @ ${entry:,.2f}\n"
                            f"New SL: ${new_sl:,.2f} (+ commission buffer)\n"
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
                        buffer = getattr(config, 'BREAKEVEN_BUFFER', 0.50)
                        new_sl = pos['entryPrice'] + buffer if pos['side'] == 'BUY' else pos['entryPrice'] - buffer
                        existing_tp = pos.get("takeProfit", 0)
                        yield amend_position_sltp(client, pos["positionId"], new_sl, existing_tp)
                        log.info(f"✅ Partial close done, SL moved to BE!")

                        if HAS_TELEGRAM_BOT:
                            safe_reason = str(decision.get('position_action_reason', 'TP1 reached')).replace("_", "-")
                            send_bot_message(
                                f"✂️ *PARTIAL CLOSE*\n"
                                f"Closed: {half_vol/100} lots\n"
                                f"Remaining SL → ${new_sl:,.2f} (+ buffer)\n"
                                f"_{safe_reason}_"
                            )
            return

        # ─── Step 9: New trade — check no existing positions for this symbol ──
        symbol_positions = [p for p in positions if p.get('symbol') == target_symbol]
        if symbol_positions:
            log.info(f"📍 {target_symbol} already has {len(symbol_positions)} position(s) — skipping new entry")
            return

        # (Dynamic ATR SL Override Removed to trust Claude's structural SL)

        # ─── Step 10: Calculate position size ─────────────────
        qty = calculate_position_size(
            balance=account["balance"],
            risk_pct=decision["position_size_pct"],
            entry=decision["entry_price"],
            stop_loss=decision["stop_loss"],
            market_regime=market_regime
        )

        if qty <= 0:
            log.warning("Position size is 0 — skipping trade")
            return

        # ─── Step 11: Execute trade via cTrader ───────────────
        log.info("🚀 Executing trade via cTrader...")
        order = yield place_market_order(
            client,
            target_symbol,
            decision["action"],
            qty,
            sl_price=decision["stop_loss"],
            tp_price=decision["take_profit"],
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
        log.error(f"❌ Analysis cycle failed: {e}")
        send_error_alert(str(e))
        if HAS_TELEGRAM_BOT:
            send_bot_message(f"🚨 *Analysis error:*\n`{str(e)[:200]}`")

    finally:
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

def tripwire_cycle():
    """
    Runs every 60 seconds. Scans ALL tracked symbols for volatility spikes,
    RSI capitulation, and wick sweeps. Wakes Claude only for the triggered symbol.
    """
    if not is_market_open():
        return

    TRIPWIRE_THRESHOLDS = {
        'XAUUSD': {'range_min': 0.80, 'sweep_wick': 1.00, 'abs_move': 1.50},
        'EURUSD': {'range_min': 0.00050, 'sweep_wick': 0.00080, 'abs_move': 0.0015},
        'USDJPY': {'range_min': 0.080, 'sweep_wick': 0.120, 'abs_move': 0.200},
        'BTCUSD': {'range_min': 80.0, 'sweep_wick': 120.0, 'abs_move': 200.0},
    }

    for symbol in config.TRADING_SYMBOLS:
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
                    if mins_since < 10:
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

                log.warning(f"⚡ TRIPWIRE [{symbol}]: {reason_str}")

                if HAS_TELEGRAM_BOT:
                    send_bot_message(f"⚡ *TRIPWIRE [" + symbol + "]*\n" + reason_str + " detected on the 1m chart. Waking Claude up!")

                reactor.callLater(0, analysis_cycle, symbol=symbol, wakeup_reason=f"TRIPWIRE [{symbol}]: {reason_str}. Evaluate immediately.")

        except Exception as e:
            log.error(f"Tripwire error ({symbol}): {e}")



# ═══════════════════════════════════════════════════════════════
# TIER 3: Local Position Monitor (every 15 min)
# ═══════════════════════════════════════════════════════════════

@defer.inlineCallbacks
def monitor_cycle():
    """Local monitoring cycle — checks positions, detects closes, no Claude call."""
    global _previous_position_ids, _last_wakeup_time, _profit_tiers_triggered

    try:
        tick_info = tick_agg.get_current_price()  # Primary symbol (XAUUSD)

        if tick_info["bid"] <= 0:
            log.debug("⏳ No ticks received yet — waiting...")
            return

        log.info(
            f"📡 Monitor | XAUUSD: ${tick_info['mid']:,.2f} | "
            f"Spread: ${tick_info['spread']:,.2f} | "
            f"Ticks: {tick_info['tick_count']:,}"
        )

        # Check open positions and balance
        positions = yield get_open_positions(client)
        current_ids = {pos["positionId"] for pos in positions}
        account = yield get_account_info(client)

        # Cache for Telegram commands
        if HAS_TELEGRAM_BOT:
            bot_state._cached_positions = positions
            bot_state._cached_balance = account

        # ─── Detect auto-closed positions (SL/TP hit) ─────────
        closed_ids = _previous_position_ids - current_ids
        if closed_ids and _previous_position_ids:  # Only if we had previous data
            current_price = tick_info["mid"]
            from trading.risk_manager import risk_state
            
            for closed_id in closed_ids:
                log.info(f"🏁 Position {closed_id} was closed (SL/TP hit)")
                # Use XAUUSD price as fallback for close logging
                current_price = tick_info["mid"]

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

        # Update tracking
        _previous_position_ids = current_ids
        for closed_id in list(_profit_tiers_triggered.keys()):
            if closed_id not in current_ids:
                del _profit_tiers_triggered[closed_id]
        for closed_id in list(_loss_tiers_triggered.keys()):
            if closed_id not in current_ids:
                del _loss_tiers_triggered[closed_id]
        for closed_id in list(_auto_lock_tiers_triggered.keys()):
            if closed_id not in current_ids:
                del _auto_lock_tiers_triggered[closed_id]

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
                current_price = pos_tick["mid"] if pos_tick["bid"] > 0 else tick_info["mid"]
                
                if entry > 0 and sl > 0:
                    risk = abs(entry - sl)
                    should_breakeven = False
                    
                    if side == "BUY" and sl < entry:
                        if current_price >= entry + risk:
                            should_breakeven = True
                    elif side == "SELL" and sl > entry:
                        if current_price <= entry - risk:
                            should_breakeven = True
                            
                    if should_breakeven:
                        log.info(f"🛡️ AUTO-BREAKEVEN TRIGGERED: 1:1 R:R reached for position {pos['positionId']}!")
                        from data.ctrader_client import amend_position_sltp
                        buffer = getattr(config, 'BREAKEVEN_BUFFER', 0.50)
                        new_sl = entry + buffer if side == 'BUY' else entry - buffer
                        # Use defer.ensureDeferred to run the async amend within the loop
                        yield amend_position_sltp(client, pos["positionId"], new_sl, tp)
                        if HAS_TELEGRAM_BOT:
                            send_bot_message(
                                f"🛡️ *AUTO-BREAKEVEN*\n"
                                f"Position: {side} @ ${entry:,.2f}\n"
                                f"Price reached 1:1 R:R (${current_price:,.2f})\n"
                                f"Stop Loss moved to Entry + Commission Buffer (${new_sl:,.2f})!"
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
                        if HAS_TELEGRAM_BOT and hasattr(bot_state, 'last_indicators') and bot_state.last_indicators and 'atr' in bot_state.last_indicators:
                            atr = bot_state.last_indicators['atr']
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
                            lock_ratio = highest_lock_tier - 0.20
                            locked_dist = lock_ratio * total_reward
                            new_sl = sl
                            
                            if side == "BUY":
                                potential_sl = entry + locked_dist - atr_buffer
                                if sl == 0 or potential_sl > sl:
                                    new_sl = potential_sl
                            elif side == "SELL":
                                potential_sl = entry - locked_dist + atr_buffer
                                if sl == 0 or potential_sl < sl:
                                    new_sl = potential_sl
                                    
                            if new_sl != sl:
                                locked_pct = int(lock_ratio * 100)
                                log.info(f"🔒 TIER LOCK: Moving SL to ${new_sl:,.2f} (Buffered) to guarantee {locked_pct}% profit on position {pos['positionId']}")
                                from data.ctrader_client import amend_position_sltp
                                yield amend_position_sltp(client, pos["positionId"], new_sl, tp)
                                sl = new_sl  # Update local var
                                if HAS_TELEGRAM_BOT:
                                    send_bot_message(f"🔒 *Auto-Lock:* SL moved to `${new_sl:,.2f}` (Guarantees {locked_pct}% profit with ATR buffer)")

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
                        yield amend_position_sltp(client, pos["positionId"], new_trail_sl, tp)
                        # We do NOT send a Telegram message here to avoid spamming the user every 5 minutes as it trails.

        else:
            log.debug("No open positions")

        # Update Telegram status
        if HAS_TELEGRAM_BOT:
            bot_state.last_indicators = tick_info

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
        log.info(f"💰 Demo balance: ${account['balance']:,.2f}")

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

            # Load historical candles for this symbol
            log.info(f"📊 Loading historical candles for {sym}...")
            raw_candles = yield get_trendbars(c, sym, period_minutes=15, count=200)
            if raw_candles:
                tick_aggregators[sym].load_historical(raw_candles, timeframe=15)
                last_close = raw_candles[-1]["close"]
                log.info(f"📊 {sym} historical data loaded | Last close: {last_close:,.5g}")

            # Subscribe to live ticks
            yield subscribe_to_prices(c, sym)
            log.info(f"📡 Subscribed to {sym} live ticks")

        # Step 7: Start Telegram bot with callbacks
        if HAS_TELEGRAM_BOT and config.TELEGRAM_BOT_TOKEN:
            log.info("🤖 Starting Telegram bot...")

            # Wire callbacks so Telegram commands trigger real actions
            bot_state.on_analyze = lambda: analysis_cycle()
            bot_state.on_force_trade = lambda: analysis_cycle()

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
            f"Broker: cTrader {config.CTRADER_HOST.upper()}\n"
            f"Symbols: {', '.join(config.TRADING_SYMBOLS)}\n"
            f"AI: {config.CLAUDE_MODEL} (every {config.ANALYSIS_INTERVAL_MINUTES}min)\n"
            f"Balance: ${account['balance']:,.2f}"
        )

        config.print_config_summary()
        log.info("✅ All systems go!\n")

        # Step 8: Run initial ML analysis and Claude analysis
        log.info("🧠 Running initial ML edge analysis...")
        update_ml_report()
        
        log.info("🧠 Running initial Claude analysis...")
        yield analysis_cycle()

        # Step 9: Start scheduled loops
        _ml_loop = task.LoopingCall(update_ml_report)
        _ml_loop.start(43200, now=False)  # Every 12 hours (12 * 60 * 60)
        log.info("🧠 ML Analyzer loop: every 12 hours")

        _analysis_loop = task.LoopingCall(analysis_cycle)
        _analysis_loop.start(config.ANALYSIS_INTERVAL_MINUTES * 60, now=False)
        log.info(f"⏰ Analysis loop: every {config.ANALYSIS_INTERVAL_MINUTES} min")

        def _update_progress_bar():
            try:
                import sys
                from twisted.internet import reactor
                if not getattr(_analysis_loop, "running", False) or not hasattr(_analysis_loop, "call") or not _analysis_loop.call:
                    return
                
                time_left = max(0, int(_analysis_loop.call.getTime() - reactor.seconds()))
                interval = config.ANALYSIS_INTERVAL_MINUTES * 60
                
                if time_left > interval:
                    time_left = interval
                    
                pct = 1.0 - (time_left / interval)
                bar_len = 20
                filled = int(bar_len * pct)
                bar = "█" * filled + "░" * (bar_len - filled)
                mins, secs = divmod(time_left, 60)
                
                if time_left > 0:
                    sys.stdout.write(f"\r⏳ Next Analysis: [{bar}] {mins:02d}:{secs:02d} remaining\033[K")
                    sys.stdout.flush()
            except Exception:
                pass

        _pb_loop = task.LoopingCall(_update_progress_bar)
        _pb_loop.start(1.0, now=False)

        _monitor_loop = task.LoopingCall(monitor_cycle)
        _monitor_loop.start(config.MONITOR_INTERVAL_MINUTES * 60, now=False)
        log.info(f"📡 Monitor loop: every {config.MONITOR_INTERVAL_MINUTES} min")

        _tripwire_loop = task.LoopingCall(tripwire_cycle)
        _tripwire_loop.start(60, now=False)
        log.info("⚡ Tripwire loop: every 60 seconds")

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

    log.info("🚀 Starting Emy AI Trading System...")
    log.info(f"   Broker: cTrader ({config.CTRADER_HOST})")
    log.info(f"   Model: {config.CLAUDE_MODEL}")
    log.info(f"   Analysis: every {config.ANALYSIS_INTERVAL_MINUTES} min")
    log.info(f"   Monitor: every {config.MONITOR_INTERVAL_MINUTES} min")

    # Validate config
    config.validate_config()

    # Create cTrader client
    host = EndPoints.PROTOBUF_LIVE_HOST if config.CTRADER_HOST == "live" else EndPoints.PROTOBUF_DEMO_HOST
    ctrader = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)

    # Set callbacks
    ctrader.setConnectedCallback(on_connected)
    ctrader.setDisconnectedCallback(on_disconnected)
    ctrader.setMessageReceivedCallback(handle_message)

    # Start connection
    ctrader.startService()

    log.info("🔗 Connecting to cTrader...\n")
    reactor.run()


if __name__ == "__main__":
    main()
