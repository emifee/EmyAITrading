"""
prompt_builder.py — Formats market data for Trend + Liquidity Sweep strategy.

Builds multi-timeframe market snapshot with session context,
key levels, sweep detection, and structural analysis.
"""

from datetime import datetime, timezone, timedelta
import pandas as pd
from utils.logger import log
import config

# Trade journal for performance history
try:
    from data.trade_journal import get_stats, get_recent_trades, get_loss_patterns
    HAS_JOURNAL = True
except ImportError:
    HAS_JOURNAL = False


def _safe(val, default=0):
    """Return val if not None, else default. Fixes .get() returning None for existing keys."""
    return val if val is not None else default


def _get_market_session() -> tuple:
    """Determine the current forex market session and grade."""
    now = datetime.now(timezone.utc)
    hour = now.hour

    # Session times (UTC)
    if 7 <= hour < 9:
        return "🇪🇺 London Open", "A", "LONDON_OPEN"
    elif 9 <= hour < 12:
        return "🇪🇺 London Session", "A", "LONDON"
    elif 12 <= hour < 14:
        return "🇪🇺🇺🇸 London-NY Overlap", "A", "OVERLAP"
    elif 14 <= hour < 17:
        return "🇺🇸 New York Session", "A", "NY"
    elif 17 <= hour < 21:
        return "🇺🇸 NY Close", "B", "NY_CLOSE"
    elif 21 <= hour or hour < 0:
        return "🌙 Transition", "B", "TRANSITION"
    elif 0 <= hour < 7:
        return "🌏 Asian Session", "A", "ASIAN"
    else:
        return "🌙 Off-hours", "B", "OFF"


def format_for_claude(candles_15m: pd.DataFrame, indicators: dict,
                       account: dict, candles_1m: pd.DataFrame = None,
                       tick_info: dict = None, mtfa_data: dict = None,
                       market_regime: str = "UNKNOWN", ml_report: str = "",
                       wakeup_reason: str = None, streak_count: int = 0, daily_pnl: float = 0.0,
                       symbol: str = "XAUUSD") -> str:
    """
    Build comprehensive market data prompt for Trend + Liquidity Sweep strategy.

    Args:
        candles_15m: 15-minute OHLCV DataFrame.
        indicators: Pre-calculated indicator dictionary.
        account: Account info (balance, positions).
        candles_1m: Optional 1-minute OHLCV DataFrame.
        tick_info: Optional real-time tick data.
        mtfa_data: Dict mapping timeframe (e.g. 60, 240) to indicator dicts.

    Returns:
        str: Formatted market snapshot string.
    """
    try:
        current_price = indicators.get("current_price", 0)

        # Use live tick price if available
        if tick_info and tick_info.get("mid", 0) > 0:
            current_price = tick_info["mid"]

        # Market session
        session_name, session_grade, session_code = _get_market_session()

        # Format last 15 candles (15m) — enough context for sweep detection
        n_candles = min(15, len(candles_15m))
        last_candles = candles_15m.tail(n_candles)[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        if hasattr(last_candles["timestamp"].iloc[0], 'strftime'):
            last_candles["timestamp"] = last_candles["timestamp"].dt.strftime("%Y-%m-%d %H:%M")
        candle_table = last_candles.to_string(index=False)

        # Format 1-minute view (last 5 candles for entry trigger detection)
        short_term_section = ""
        if candles_1m is not None and not candles_1m.empty and len(candles_1m) >= 3:
            last_5_1m = candles_1m.tail(5)[["timestamp", "open", "high", "low", "close"]].copy()
            if hasattr(last_5_1m["timestamp"].iloc[0], 'strftime'):
                last_5_1m["timestamp"] = last_5_1m["timestamp"].dt.strftime("%H:%M")

            closes_1m = candles_1m["close"].tail(5)
            momentum = "RISING" if closes_1m.iloc[-1] > closes_1m.iloc[0] else "FALLING"
            delta = closes_1m.iloc[-1] - closes_1m.iloc[0]

            short_term_section = f"""
─── SHORT-TERM VIEW (1-min) — for entry trigger ────
Momentum (last 5 min): {momentum} (${delta:+,.2f})
{last_5_1m.to_string(index=False)}
"""

        # Live tick section
        tick_section = ""
        if tick_info and tick_info.get("bid", 0) > 0:
            tick_section = f"""
─── LIVE TICK DATA ──────────────────────────────────
Bid:            ${tick_info['bid']:,.2f}
Ask:            ${tick_info['ask']:,.2f}
Spread:         ${tick_info.get('spread', 0):,.2f}
"""

        # Sweep detection section
        sweep_section = "Not detected in recent candles"
        if indicators.get("sweep_detected"):
            sweep_section = (
                f"🚨 {indicators['sweep_type']} detected!\n"
                f"   Swept level: ${_safe(indicators.get('sweep_level')):,.2f}\n"
                f"   Engulfing confirmation: {'YES ✅' if indicators.get('engulfing_detected') else 'NO — waiting'}"
            )

        # Key levels section
        round_levels = indicators.get("round_levels", [])
        round_levels_str = ", ".join([f"${r:,.0f}" for r in round_levels]) if round_levels else "N/A"

        # Open positions — detailed for position management
        positions_str = "None — no open positions"
        # Check if there are open positions to determine AI Mode (Hunter vs Manager)
        has_position = False
        if account.get("positions"):
            has_position = True
            pos_list = []
            for p in account["positions"]:
                side = p.get("side", "?")
                vol = p.get("volume", 0)
                entry = float(p.get("entryPrice", 0))
                sl = float(p.get("stopLoss", 0))
                tp = float(p.get("takeProfit", 0))
                current_pos_price = float(p.get("currentPrice", current_price))
                upnl = _safe(p.get("unrealizedPnl"))
                pos_id = p.get("positionId", "?")

                # Calculate distance to SL/TP
                risk_taken = abs(entry - sl) if sl > 0 else 0
                reward_target = abs(tp - entry) if tp > 0 else 0
                if side == "BUY":
                    current_move = current_pos_price - entry
                else:
                    current_move = entry - current_pos_price

                rr_progress = round(current_move / risk_taken, 2) if risk_taken > 0 else 0

                pos_list.append(
                    f"  ⚡ {side} {vol} lots @ ${entry:,.2f} (ID: {pos_id})\n"
                    f"     Current: ${current_pos_price:,.2f} | Move: ${current_move:+,.2f}\n"
                    f"     SL: ${sl:,.2f} | TP: ${tp:,.2f}\n"
                    f"     Risk: ${risk_taken:,.2f} | Reward target: ${reward_target:,.2f}\n"
                    f"     R:R Progress: {rr_progress:+.2f}R | uPnL: ${upnl:,.2f}"
                )
            positions_str = "\n".join(pos_list)

        # ─── Trade Journal — Past Performance ─────────────────
        journal_section = ""
        if HAS_JOURNAL:
            try:
                stats = get_stats()
                recent = get_recent_trades(5)

                if stats["total_trades"] > 0:
                    journal_section = (
                        f"─── 📓 TRADE JOURNAL — PAST PERFORMANCE ────────────\n"
                        f"Total trades: {stats['total_trades']} | "
                        f"Win rate: {stats['win_rate']}% | "
                        f"Avg R:R: {stats['avg_rr']}:1\n"
                        f"Total P&L: ${stats['total_pnl']:,.2f} | "
                        f"Avg winner: ${stats['avg_winner']:,.2f} | "
                        f"Avg loser: ${stats['avg_loser']:,.2f}\n"
                        f"Today P&L: ${stats['today_pnl']:,.2f} ({stats['today_trades']} trades)\n"
                    )

                    if recent:
                        journal_section += "\nLast 5 trades:\n"
                        for t in recent:
                            pnl = t.get('pnl_dollars') or 0
                            exit_p = t.get('exit_price') or 0
                            rr = t.get('risk_reward') or 0
                            emoji = "✅" if pnl > 0 else "❌"
                            journal_section += (
                                f"  {emoji} {t['side']} @ ${t['entry_price']:,.2f} → "
                                f"${exit_p:,.2f} | "
                                f"P&L: ${pnl:,.2f} | "
                                f"R:R: {rr} | "
                                f"{t.get('exit_reason') or '?'}\n"
                            )

                    # ─── Pattern Analysis — LEARN FROM MISTAKES ───
                    try:
                        patterns = get_loss_patterns()
                        if patterns:
                            sa = patterns["side_analysis"]
                            journal_section += (
                                f"\n─── 🔍 PATTERN ANALYSIS ─────────────────────────\n"
                                f"BUY trades: {_safe(sa.get('buy_total'))} total, {_safe(sa.get('buy_wr'))}% WR, P&L: ${_safe(sa.get('buy_pnl')):,.2f}\n"
                                f"SELL trades: {_safe(sa.get('sell_total'))} total, {_safe(sa.get('sell_wr'))}% WR, P&L: ${_safe(sa.get('sell_pnl')):,.2f}\n"
                                f"Current losing streak: {_safe(patterns.get('streak'))}\n"
                            )

                            if patterns.get('streak', 0) >= 2:
                                journal_section += (
                                    f"\n🚨🚨 URGENT WARNING: YOU HAVE LOST {patterns['streak']} TRADES IN A ROW! 🚨🚨\n"
                                    f"Your previous analysis was incorrect. Do NOT take sub-optimal trades.\n"
                                    f"You MUST perform significantly deeper analysis for the next trade. If in doubt, HOLD.\n"
                                )

                            if patterns.get("lessons"):
                                journal_section += "\n⚠️ CRITICAL LESSONS (MUST FOLLOW):\n"
                                for lesson in patterns["lessons"]:
                                    journal_section += f"  🚨 {lesson}\n"

                            if patterns.get("losing_reasons"):
                                journal_section += "\nWhy recent trades LOST:\n"
                                for lr in patterns["losing_reasons"][-3:]:
                                    journal_section += (
                                        f"  ❌ {lr['side']} (${lr['pnl']}) — {lr['reason'][:120]}\n"
                                    )
                    except Exception as pe:
                        log.debug(f"Pattern analysis not available: {pe}")

                    journal_section += (
                        "\n⚡ USE THIS DATA: Do NOT repeat losing patterns. "
                        "If a direction is losing, switch bias. "
                        "If win rate is below 40%, increase confidence threshold. "
                        "Only take A-grade setups after a losing streak.\n"
                    )
                else:
                    journal_section = (
                        "─── 📓 TRADE JOURNAL ────────────────────────────\n"
                        "No closed trades yet — this is a new system.\n"
                        "Be extra conservative until a track record is established.\n"
                    )
            except Exception as e:
                log.debug(f"Journal data not available: {e}")

        # ─── Macro Performance Context ─────────────────────────
        macro_section = ""
        try:
            from data.trade_journal import get_time_reports
            reports = get_time_reports()
            today = reports["today"]
            week = reports["week"]
            month = reports["month"]
            
            macro_section = (
                "─── 📅 MACRO PERFORMANCE CONTEXT ────────────────\n"
                f"Today: {today['trades']} trades, {today['win_rate']}% WR, P&L: ${today['pnl']:,.2f}\n"
                f"This Week: {week['trades']} trades, {week['win_rate']}% WR, P&L: ${week['pnl']:,.2f}\n"
                f"This Month: {month['trades']} trades, {month['win_rate']}% WR, P&L: ${month['pnl']:,.2f}\n"
            )
            
            if today['pnl'] < 0:
                macro_section += "⚠️ WARNING: You are losing money today. Switch to MAXIMUM DEFENSIVE mode. Do not take any B-grade setups. Protect capital.\n"
            elif week['pnl'] > 0:
                macro_section += "✅ SUCCESS: You are highly profitable this week. Protect these weekly gains. Only take A+ sniper setups.\n"
            
            macro_section += "\n"
        except Exception as e:
            log.debug(f"Macro reports not available: {e}")

        # ─── Live Macroeconomic News ──────────────────────────
        news_section = ""
        try:
            from data.macro_feed import get_live_macro_news
            news_data = get_live_macro_news(max_events=3)
            news_section = (
                "─── 📰 LIVE MACROECONOMIC NEWS ──────────────────\n"
                f"{news_data}\n"
                "⚡ NEWS RULE: If High-Impact news is scheduled within the next 2 hours, be extremely cautious or HOLD.\n"
                "If news just dropped, expect massive liquidity sweeps. Use fundamental context to justify technical moves.\n\n"
            )
        except Exception as e:
            log.debug(f"Macro news feed not available: {e}")

        # ─── Multi-Timeframe Analysis (MTFA) ──────────────────
        mtfa_section = ""
        if mtfa_data:
            mtfa_section = "─── MULTI-TIMEFRAME ANALYSIS (MTFA) ────────────────\n"
            # Sort timeframes so they appear in order (5, 60, 240)
            for tf in sorted(mtfa_data.keys()):
                ind = mtfa_data[tf]
                if tf == 5:
                    label = "M5 (Early Momentum)"
                elif tf == 60:
                    label = "H1 (Trend)"
                else:
                    label = "H4 (Macro)"
                    
                mtfa_section += (
                    f"{label}:\n"
                    f"  Price: ${_safe(ind.get('latest_close')):,.2f} | "
                    f"Trend: {_safe(ind.get('trend'), 'UNKNOWN')} | "
                    f"Structure: {_safe(ind.get('structure'), 'UNKNOWN')}\n"
                    f"  EMA50: ${_safe(ind.get('ema50')):,.2f} | "
                    f"EMA200: ${_safe(ind.get('ema200')):,.2f}\n"
                    f"  MACD: {_safe(ind.get('macd')):.2f} | "
                    f"RSI: {_safe(ind.get('rsi')):.1f}\n"
                )
            mtfa_section += "\n⚡ MTFA RULE: Do NOT trade against the H1/H4 trend unless it's a confirmed massive sweep reversal.\n"

        strategy_text = "Trend + Liquidity Sweep"
        
        # ─── Split AI Logic: Hunter vs Manager ────────────────
        if has_position:
            playbook_rules = (
                f"🎯 REGIME CONTEXT: {market_regime}\n"
                "• MANAGER MODE ACTIVE: You have an open position.\n"
                "• IGNORE new entry signals. Your ONLY job is to evaluate the existing trade.\n"
                "• Check for invalidation signals (e.g. price closed beyond key EMA, momentum shifted).\n"
                "• If structure is broken, output CLOSE_TRADE.\n"
                "• If structure is valid, output HOLD or PARTIAL_CLOSE.\n"
            )
        else:
            playbook_rules = (
                f"🎯 REGIME CONTEXT: {market_regime}\n"
                "• HUNTER MODE ACTIVE: No open positions.\n"
                "• Look for liquidity sweeps, trend continuations, and high-probability entries.\n"
                "• If you find a setup, output BUY or SELL with precise Stop Loss and Take Profit.\n"
                "• If no clear setup exists, output HOLD.\n"
            )

        # ─── Volatility & Risk Warnings ────────────────────────
        now_utc = datetime.now(timezone.utc)
        volatility_warning = ""
        
        # Calculate time to NY Open (13:00 UTC)
        ny_open = now_utc.replace(hour=13, minute=0, second=0, microsecond=0)
        if now_utc.hour >= 13:
            ny_open += timedelta(days=1)
            
        # Calculate time to London Open (8:00 UTC)
        london_open = now_utc.replace(hour=8, minute=0, second=0, microsecond=0)
        if now_utc.hour >= 8:
            london_open += timedelta(days=1)
            
        mins_to_ny = (ny_open - now_utc).total_seconds() / 60
        mins_to_london = (london_open - now_utc).total_seconds() / 60
        
        if 0 < mins_to_ny <= 60:
            volatility_warning = f"⚠️ VOLATILITY WARNING: New York session opens in {int(mins_to_ny)} minutes. Expect aggressive volume spikes and wicks. Tighten stops if in profit.\n"
        elif 0 < mins_to_london <= 60:
            volatility_warning = f"⚠️ VOLATILITY WARNING: London session opens in {int(mins_to_london)} minutes. Expect aggressive volume spikes and wicks. Tighten stops if in profit.\n"

        risk_warning = ""
        if streak_count >= 2 or daily_pnl < 0:
            risk_warning = (
                f"🚨 RISK ALERT: You are on a {streak_count}-trade losing streak today. "
                f"Daily P&L: ${daily_pnl:,.2f}.\n"
                f"Instead of blindly holding or artificially lowering confidence, DEEPEN YOUR ANALYSIS. You must actively hunt for an A+ high-probability setup with strict volume and structural confirmation to recover. If a pristine setup exists, trade it. If the market is choppy, wait patiently.\n"
            )

        # ─── Display All Indicators ─────────────────────────
        # We previously stripped indicators based on regime, but ADX lag 
        # caused Claude to miss obvious trends. We now show ALL indicators.
        
        trend_section = (
            f"─── STEP 1: TREND DIRECTION (M15) ───────────────────\n"
            f"EMA 50:         ${_safe(indicators.get('ema50')):,.2f}\n"
            f"EMA 200:        ${_safe(indicators.get('ema200')):,.2f}\n"
            f"Trend Bias:     {_safe(indicators.get('trend'), 'UNKNOWN')}\n"
            f"Structure:      {_safe(indicators.get('structure'), 'Unknown')}\n"
            f"EMA 20 (ref):   ${_safe(indicators.get('ema20')):,.2f}\n\n"
        )
            
        key_levels_section = (
            "─── STEP 2: KEY LEVELS ─────────────────────────────\n"
            f"Session High:   ${_safe(indicators.get('session_high')):,.2f}\n"
            f"Session Low:    ${_safe(indicators.get('session_low')):,.2f}\n"
            f"BB Upper:       ${_safe(indicators.get('bb_upper')):,.2f}\n"
            f"BB Lower:       ${_safe(indicators.get('bb_lower')):,.2f}\n"
            f"VWAP:           ${_safe(indicators.get('vwap')):,.2f}\n"
            f"Fib 0.382:      ${_safe(indicators.get('fib_0382')):,.2f}\n"
            f"Fib 0.500:      ${_safe(indicators.get('fib_0500')):,.2f}\n"
            f"Fib 0.618:      ${_safe(indicators.get('fib_0618')):,.2f}\n"
            f"Swing High:     ${_safe(indicators.get('swing_high')):,.2f}\n"
            f"Swing Low:      ${_safe(indicators.get('swing_low')):,.2f}\n"
            f"Nearest Round:  ${_safe(indicators.get('nearest_round')):,.2f} ({_safe(indicators.get('distance_to_round')):,.2f} away)\n"
        )
        
        sweep_data = f"─── STEP 3: SWEEP DETECTION ─────────────────────────\n{sweep_section}\n\n"
        
        supp_section = (
            "─── STEP 4: SUPPORTING DATA ─────────────────────────\n"
            f"ATR(14):        ${_safe(indicators.get('atr')):,.2f}\n"
            f"RSI(14):        {_safe(indicators.get('rsi'), 'N/A')}\n"
            f"MACD:           {_safe(indicators.get('macd'), 'N/A')}\n"
            f"MACD Histogram: {_safe(indicators.get('macd_hist'), 'N/A')}\n"
            f"Volume Ratio:   {_safe(indicators.get('volume_ratio'))}x avg {'📈 SPIKE' if _safe(indicators.get('volume_ratio')) > 1.5 else ''}\n"
            f"Volume Delta:   {_safe(indicators.get('volume_delta'), 'N/A')}\n"
        )

        wakeup_str = ""
        if wakeup_reason:
            wakeup_str = (
                f"🚨 **WAKEUP TRIGGERED:** {wakeup_reason}\n"
                f"Evaluate the position immediately. Check if momentum is stalling at this profit level.\n"
                f"═══════════════════════════════════════════════════════\n"
            )

        prompt = f"""
{symbol} MARKET SNAPSHOT — {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}
Strategy: {strategy_text}
═══════════════════════════════════════════════════════

{wakeup_str}{volatility_warning}{risk_warning}
{playbook_rules}
CURRENT PRICE: ${current_price:,.2f}
{tick_section}
─── SESSION ─────────────────────────────────────────
Session: {session_name}
Grade: {session_grade} {'(Prime entry window)' if session_grade == 'A' else '(Normal trading)'}

─── 🚨 YOUR STATISTICAL WEAKNESSES ───
{ml_report if ml_report else 'No statistical edge report available yet.'}
⚡ ML RULE: Review the report above. If the current regime or hour is marked as a DEAD ZONE or you are bleeding money, you MUST be extremely cautious. Do not take B-grade setups in weak environments.

{macro_section}
{news_section}
{mtfa_section}
{trend_section}{key_levels_section}
{sweep_data}{supp_section}
─── LAST {n_candles} CANDLES (15m) ─────────────────────────
{candle_table}
{short_term_section}
─── ACCOUNT ─────────────────────────────────────────
Available Balance: ${account.get('balance', 0):,.2f} USD
Open Positions:
{positions_str}
Max Risk Per Trade: {config.MAX_RISK_PER_TRADE}%

{journal_section}
"""

        log.debug(f"Prompt built: {len(prompt)} characters")
        return prompt.strip()

    except Exception as e:
        log.error(f"Failed to build Claude prompt: {e}")
        raise
