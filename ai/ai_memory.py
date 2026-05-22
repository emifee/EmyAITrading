"""
ai_memory.py — Full memory system for Claude AI trading decisions.

Features:
1. Track last N analysis decisions per symbol (survives in-memory)
2. Track active setups with 4-hour expiry
3. Save entry thesis when trade executes (WHY Claude entered)
4. Track last 2 losing trades with thesis vs outcome
5. Persist ALL memory to disk (survives bot restarts)
6. Flip-flop detection
7. 4-Hour Session Journal (memo_to_self)
8. Level-Specific Trade History (win/loss at specific price zones)
9. Market Condition Memory (regime timeline)
10. Key Level Memory (tested & rejected levels)
"""

import os
import json
import time
from collections import deque
from utils.logger import log


# ─── File persistence path ────────────────────────────────────
_MEMORY_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_MEMORY_FILE = os.path.join(_MEMORY_DIR, "ai_memory.json")

# ─── Per-symbol memory buffers ────────────────────────────────
_memory = {}           # symbol -> list of decision records (last 5)
_tracking_setups = {}  # symbol -> {"setup": str, "started_at": float, ...}
_entry_theses = {}     # symbol -> {"thesis": str, "action": str, "entry_price": float, ...}
_recent_losses = {}    # symbol -> list of last 2 losses with thesis
_journal = {}          # symbol -> {"memo": str, "timestamp": float, "h4_trend": str, "last_trade": str}
_rule_violations = {}  # symbol -> list of {"reason": str, "timestamp": float}
_level_history = {}    # symbol -> list of {"level": float, "action": str, "won": bool, "pnl": float, "timestamp": float}
_regime_timeline = {}  # symbol -> list of {"regime": str, "timestamp": float} (last 12 readings)
_tested_levels = {}    # symbol -> dict of level_key -> {"level": float, "times_tested": int, "times_rejected": int, "last_tested": float}
_broker_memory = {}    # symbol -> {"total_slippage": float, "trades": int, "avg_slippage": float}
_historical_drawdowns = {} # symbol -> list of {"drawdown_pct": float, "regime": str, "timestamp": float}
_wake_triggers = {}    # symbol -> {"wake_above": float, "wake_below": float, "reason": str, "context": str, "set_at": float}

MAX_MEMORY = 5
SETUP_EXPIRY_HOURS = 8.0  # Increased to 8 hours to allow M15/H1 setups time to play out
MAX_LOSSES = 2
MAX_LEVEL_HISTORY = 10
MAX_REGIME_TIMELINE = 12
LEVEL_PROXIMITY = 5.0  # Consider levels within $5 as the "same" zone


# ═══════════════════════════════════════════════════════════════
# PERSISTENCE — Save/Load to disk
# ═══════════════════════════════════════════════════════════════

def _save_to_disk():
    """Persist all memory state to JSON file."""
    try:
        state = {
            "memory": {k: list(v) for k, v in _memory.items()},
            "tracking_setups": _tracking_setups,
            "entry_theses": _entry_theses,
            "recent_losses": _recent_losses,
            "journal": _journal,
            "rule_violations": _rule_violations,
            "level_history": _level_history,
            "regime_timeline": _regime_timeline,
            "tested_levels": _tested_levels,
            "broker_memory": _broker_memory,
            "historical_drawdowns": _historical_drawdowns,
            "wake_triggers": _wake_triggers,
            "saved_at": time.time(),
        }
        os.makedirs(_MEMORY_DIR, exist_ok=True)
        with open(_MEMORY_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.debug(f"Memory save error: {e}")


def load_from_disk():
    """Load memory state from disk on startup."""
    global _memory, _tracking_setups, _entry_theses, _recent_losses
    global _level_history, _regime_timeline, _tested_levels
    
    if not os.path.exists(_MEMORY_FILE):
        log.info("🧠 Memory: No saved state found — starting fresh")
        return
    
    try:
        with open(_MEMORY_FILE, "r") as f:
            state = json.load(f)
        
        _memory = {k: list(v) for k, v in state.get("memory", {}).items()}
        _tracking_setups = state.get("tracking_setups", {})
        _entry_theses = state.get("entry_theses", {})
        _recent_losses = state.get("recent_losses", {})
        _journal = state.get("journal", {})
        _rule_violations = state.get("rule_violations", {})
        _level_history = state.get("level_history", {})
        _regime_timeline = state.get("regime_timeline", {})
        _tested_levels = state.get("tested_levels", {})
        _broker_memory = state.get("broker_memory", {})
        _historical_drawdowns = state.get("historical_drawdowns", {})
        _wake_triggers = state.get("wake_triggers", {})
        
        saved_at = state.get("saved_at", 0)
        age_min = int((time.time() - saved_at) / 60) if saved_at else 0
        
        total_records = sum(len(v) for v in _memory.values())
        total_losses = sum(len(v) for v in _recent_losses.values())
        total_levels = sum(len(v) for v in _level_history.values())
        
        log.info(
            f"🧠 Memory: Loaded from disk | "
            f"{total_records} decisions | "
            f"{len(_entry_theses)} active theses | "
            f"{total_losses} loss records | "
            f"{total_levels} level records | "
            f"Saved {age_min}min ago"
        )
        
        # Expire old tracking setups that are past 4 hours
        now = time.time()
        expired = []
        for sym, setup in _tracking_setups.items():
            if (now - setup.get("started_at", 0)) / 3600 >= SETUP_EXPIRY_HOURS:
                expired.append(sym)
        for sym in expired:
            log.info(f"🧠 Memory: Expired stale setup for {sym} (loaded from disk)")
            del _tracking_setups[sym]
        
        # Expire tested levels older than 24 hours
        for sym in list(_tested_levels.keys()):
            expired_lvls = []
            for lvl_key, lvl_data in _tested_levels[sym].items():
                if (now - lvl_data.get("last_tested", 0)) / 3600 >= 24:
                    expired_lvls.append(lvl_key)
            for lvl_key in expired_lvls:
                del _tested_levels[sym][lvl_key]
        
        # Expire regime timeline entries older than 12 hours
        for sym in list(_regime_timeline.keys()):
            _regime_timeline[sym] = [
                r for r in _regime_timeline[sym]
                if (now - r.get("timestamp", 0)) / 3600 < 12
            ]
            
        # Expire rule violations older than 24 hours
        for sym in list(_rule_violations.keys()):
            _rule_violations[sym] = [
                v for v in _rule_violations[sym]
                if (now - v.get("timestamp", 0)) / 3600 < 24
            ]
        
    except Exception as e:
        log.warning(f"🧠 Memory: Failed to load from disk: {e} — starting fresh")


# ═══════════════════════════════════════════════════════════════
# RECORD DECISIONS
# ═══════════════════════════════════════════════════════════════

def record_decision(symbol: str, decision: dict, current_price: float = 0):
    """
    Store a Claude decision in short-term memory.
    
    Args:
        symbol: e.g. "XAUUSD"
        decision: The parsed Claude response dict
        current_price: Current market price at time of decision
    """
    if symbol not in _memory:
        _memory[symbol] = []
    
    record = {
        "timestamp": time.time(),
        "action": decision.get("action", "HOLD"),
        "confidence": decision.get("confidence", 0),
        "reason": decision.get("reason", ""),
        "entry_price": decision.get("entry_price", 0),
        "stop_loss": decision.get("stop_loss", 0),
        "take_profit": decision.get("take_profit", 0),
        "trend_bias": decision.get("trend_bias", "NEUTRAL"),
        "sweep_detected": decision.get("sweep_detected", False),
        "key_level": decision.get("key_level", 0),
        "current_price": current_price,
    }
    
    # Save the memo to the journal if one is provided
    memo = decision.get("memo_to_self", "")
    if memo:
        if symbol not in _journal:
            _journal[symbol] = {}
        _journal[symbol]["memo"] = memo
        _journal[symbol]["timestamp"] = time.time()
        # h4_trend is not in decision directly, but we have trend_bias
        _journal[symbol]["h4_trend"] = decision.get("trend_bias", "UNKNOWN")
        log.info(f"🧠 Memory: Journal memo saved for {symbol} — {memo[:50]}...")
    
    _memory[symbol].append(record)
    # Keep only last N
    if len(_memory[symbol]) > MAX_MEMORY:
        _memory[symbol] = _memory[symbol][-MAX_MEMORY:]
    
    # Auto-detect if Claude is tracking a setup from its reason text
    reason = decision.get("reason", "").lower()
    action = decision.get("action", "HOLD")
    
    tracking_keywords = [
        "waiting for", "watching for", "looking for", "need to see",
        "if price breaks", "confirmation needed", "monitor for",
        "expecting", "anticipating", "tracking"
    ]
    
    # ─── Key Level Memory: track levels Claude mentions ───
    key_level = decision.get("key_level", 0)
    if key_level > 0:
        _record_tested_level(symbol, key_level, action)
    
    if action == "HOLD":
        is_invalid = decision.get("wake_status") == "DECLINED" or any(kw in reason for kw in ["invalid", "abandon", "cancel", "failed", "no longer valid"])
        if is_invalid:
            if symbol in _tracking_setups:
                log.info(f"🧠 Memory: Setup INVALIDATED by Claude. Wiping tracked setup for {symbol}.")
                del _tracking_setups[symbol]
        elif any(kw in reason for kw in tracking_keywords):
            # Claude is watching something specific — track it
            wake_above = decision.get("wake_above_price", 0)
            wake_below = decision.get("wake_below_price", 0)
            _tracking_setups[symbol] = {
                "setup": decision.get("reason", ""),
                "started_at": time.time(),
                "price_at_start": current_price,
                "bias": decision.get("trend_bias", "NEUTRAL"),
                "target_entry": decision.get("entry_price", 0),
                "target_sl": decision.get("stop_loss", 0),
                "target_tp": decision.get("take_profit", 0),
                "wake_above_price": wake_above,
                "wake_below_price": wake_below,
            }
            wake_info = ""
            if wake_above: wake_info += f" | Wake above: ${wake_above:,.5g}"
            if wake_below: wake_info += f" | Wake below: ${wake_below:,.5g}"
            log.info(f"🧠 Memory: Tracking setup for {symbol} — {decision.get('reason', '')[:80]}{wake_info}")
        else:
            # Claude is holding but not tracking anything new. Clear old tracking.
            if symbol in _tracking_setups:
                log.info(f"🧠 Memory: Cleared old tracking setup for {symbol} (Claude stopped tracking)")
                del _tracking_setups[symbol]
    elif action in ("BUY", "SELL"):
        # Trade executed — save entry thesis
        _entry_theses[symbol] = {
            "thesis": decision.get("reason", ""),
            "action": action,
            "confidence": decision.get("confidence", 0),
            "entry_price": decision.get("entry_price", 0),
            "stop_loss": decision.get("stop_loss", 0),
            "take_profit": decision.get("take_profit", 0),
            "trend_bias": decision.get("trend_bias", "NEUTRAL"),
            "sweep_detected": decision.get("sweep_detected", False),
            "entered_at": time.time(),
            "price_at_entry": current_price,
        }
        log.info(f"🧠 Memory: Entry thesis saved for {symbol} — {action} @ ${current_price:,.2f}")
        
        # Clear tracking (setup resolved into a trade)
        if symbol in _tracking_setups:
            del _tracking_setups[symbol]
    
    # ─── Universal Wake Triggers & Lookout: store from ANY decision ───
    wake_above = decision.get("wake_above_price", 0)
    wake_below = decision.get("wake_below_price", 0)
    lookout_instructions = decision.get("lookout_instructions", "").strip()
    
    if wake_above or wake_below or lookout_instructions:
        context = "entry_tracking" if action == "HOLD" and not current_price else "position_management"
        # Determine context based on whether there's likely an open position
        if action in ("HOLD", "MOVE_SL_BE", "PARTIAL_CLOSE", "CLOSE_TRADE"):
            # Could be either — use presence of entry thesis as signal
            context = "position_management" if symbol in _entry_theses else "entry_tracking"
        _wake_triggers[symbol] = {
            "wake_above": wake_above,
            "wake_below": wake_below,
            "lookout_instructions": lookout_instructions,
            "reason": decision.get("reason", "")[:200],
            "action": action,
            "context": context,
            "set_at": time.time(),
        }
        wake_info = ""
        if wake_above: wake_info += f"above ${wake_above:,.5g} "
        if wake_below: wake_info += f"below ${wake_below:,.5g} "
        if lookout_instructions: wake_info += f" (+Lookout) "
        log.info(f"🎯 Wake Trigger SET for {symbol}: {wake_info}({context})")
    else:
        # Claude didn't set any wake triggers or instructions — clear old ones for this symbol
        if symbol in _wake_triggers:
            log.info(f"🎯 Wake Trigger CLEARED for {symbol} (Claude did not set new triggers or instructions)")
            del _wake_triggers[symbol]
    
    # Save to disk after every decision
    _save_to_disk()


def sync_open_positions(symbol: str, has_position: bool):
    """
    Called before analysis to ensure AI memory strictly matches the broker's reality.
    If the broker has no open positions but memory thinks there is one (due to a failed trade
    execution or missed close event), this forcefully clears the ghost memory.
    """
    if not has_position and symbol in _entry_theses:
        log.warning(f"🧹 Memory Sync: Broker shows no open trades for {symbol}, but memory had a ghost trade. Clearing _entry_theses.")
        del _entry_theses[symbol]
        _save_to_disk()


# ═══════════════════════════════════════════════════════════════
# RECORD TRADE OUTCOME (called when trade closes)
# ═══════════════════════════════════════════════════════════════

def record_trade_outcome(symbol: str, pnl: float, exit_reason: str, exit_price: float = 0):
    """
    Record a trade outcome — especially losses — so Claude can learn.
    
    Args:
        symbol: e.g. "XAUUSD"
        pnl: Profit/loss in dollars
        exit_reason: Why the trade closed (SL hit, TP hit, manual, etc.)
        exit_price: Price at exit
    """
    thesis_info = _entry_theses.pop(symbol, {})
    
    # ─── Level-Specific Trade History ─────────────────────
    entry_price = thesis_info.get("entry_price", 0)
    action_taken = thesis_info.get("action", "?")
    is_win = pnl >= 0
    if entry_price > 0:
        if symbol not in _level_history:
            _level_history[symbol] = []
        _level_history[symbol].append({
            "level": round(entry_price, 2),
            "action": action_taken,
            "won": is_win,
            "pnl": round(pnl, 2),
            "timestamp": time.time(),
        })
        # Keep last N
        if len(_level_history[symbol]) > MAX_LEVEL_HISTORY:
            _level_history[symbol] = _level_history[symbol][-MAX_LEVEL_HISTORY:]
        
        # Mark the key level as rejected if we lost at it
        if not is_win:
            _mark_level_rejected(symbol, entry_price)
    
    if pnl < 0:
        # This was a losing trade — store it for Claude to learn from
        if symbol not in _recent_losses:
            _recent_losses[symbol] = []
        
        loss_record = {
            "timestamp": time.time(),
            "action": action_taken,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "stop_loss": thesis_info.get("stop_loss", 0),
            "take_profit": thesis_info.get("take_profit", 0),
            "pnl": pnl,
            "exit_reason": exit_reason,
            "entry_thesis": thesis_info.get("thesis", "No thesis recorded"),
            "confidence_at_entry": thesis_info.get("confidence", 0),
            "trend_bias": thesis_info.get("trend_bias", "?"),
            "sweep_at_entry": thesis_info.get("sweep_detected", False),
        }
        
        _recent_losses[symbol].append(loss_record)
        # Keep only last N losses
        if len(_recent_losses[symbol]) > MAX_LOSSES:
            _recent_losses[symbol] = _recent_losses[symbol][-MAX_LOSSES:]
        
        log.info(
            f"🧠 Memory: Loss recorded for {symbol} | "
            f"PnL: ${pnl:,.2f} | Reason: {exit_reason} | "
            f"Entry thesis: {thesis_info.get('thesis', 'N/A')[:60]}"
        )
    else:
        log.info(f"🧠 Memory: Win recorded for {symbol} | PnL: ${pnl:,.2f}")
        
    # Update journal last trade
    if symbol not in _journal:
        _journal[symbol] = {}
    _journal[symbol]["last_trade"] = f"{'Won' if pnl >= 0 else 'Lost'} ${abs(pnl):,.2f} ({exit_reason})"
    
    _save_to_disk()


# ═══════════════════════════════════════════════════════════════
# RECORD MARKET REGIME (called each analysis cycle)
# ═══════════════════════════════════════════════════════════════

def record_regime(symbol: str, regime: str):
    """
    Record the current market regime so Claude can see regime transitions.
    
    Args:
        symbol: e.g. "XAUUSD"
        regime: e.g. "TRENDING_BULLISH", "RANGING_CHOPPY", "TRANSITIONING"
    """
    if symbol not in _regime_timeline:
        _regime_timeline[symbol] = []
    
    timeline = _regime_timeline[symbol]
    
    # Only record if regime changed or it's been > 30 min since last entry
    if timeline:
        last = timeline[-1]
        elapsed = time.time() - last.get("timestamp", 0)
        if last.get("regime") == regime and elapsed < 1800:  # 30 min
            return  # Same regime, too recent — skip
    
    timeline.append({
        "regime": regime,
        "timestamp": time.time(),
    })
    
    # Keep only last N
    if len(timeline) > MAX_REGIME_TIMELINE:
        _regime_timeline[symbol] = timeline[-MAX_REGIME_TIMELINE:]
    
    _save_to_disk()


# ═══════════════════════════════════════════════════════════════
# KEY LEVEL TRACKING (internal helpers)
# ═══════════════════════════════════════════════════════════════

def _level_key(price: float) -> str:
    """Round a price to the nearest LEVEL_PROXIMITY zone for grouping."""
    rounded = round(price / LEVEL_PROXIMITY) * LEVEL_PROXIMITY
    return f"{rounded:.2f}"


def _record_tested_level(symbol: str, level: float, action: str):
    """Record that a level was tested (Claude mentioned it in a decision)."""
    if level <= 0:
        return
    
    if symbol not in _tested_levels:
        _tested_levels[symbol] = {}
    
    key = _level_key(level)
    if key not in _tested_levels[symbol]:
        _tested_levels[symbol][key] = {
            "level": round(level, 2),
            "times_tested": 0,
            "times_rejected": 0,
            "last_tested": 0,
            "last_action": "",
        }
    
    entry = _tested_levels[symbol][key]
    entry["times_tested"] += 1
    entry["last_tested"] = time.time()
    entry["last_action"] = action
    # Update with the most recent exact level
    entry["level"] = round(level, 2)


def _mark_level_rejected(symbol: str, price: float):
    """Mark a level as rejected (a trade at this level lost)."""
    if price <= 0 or symbol not in _tested_levels:
        return
    
    key = _level_key(price)
    if key in _tested_levels[symbol]:
        _tested_levels[symbol][key]["times_rejected"] += 1
        log.info(f"🧠 Memory: Level ${price:,.2f} marked as REJECTED for {symbol}")


# ═══════════════════════════════════════════════════════════════
# RULE VIOLATION TRACKING
# ═══════════════════════════════════════════════════════════════

def record_violation(symbol: str, reason: str):
    """
    Record a rule violation (e.g. R:R too low, wrong SL direction).
    
    Args:
        symbol: e.g. "XAUUSD"
        reason: The validation failure reason
    """
    if symbol not in _rule_violations:
        _rule_violations[symbol] = []
        
    _rule_violations[symbol].append({
        "reason": reason,
        "timestamp": time.time()
    })
    
    # Keep last 5 violations
    if len(_rule_violations[symbol]) > 5:
        _rule_violations[symbol] = _rule_violations[symbol][-5:]
        
    _save_to_disk()
    log.info(f"🧠 Memory: Rule violation recorded for {symbol} — {reason}")


# ═══════════════════════════════════════════════════════════════
# BROKER BEHAVIOR (SLIPPAGE)
# ═══════════════════════════════════════════════════════════════

def record_slippage(symbol: str, slippage_amount: float):
    """
    Record execution slippage (difference between Claude's requested entry
    and the actual filled entry from the broker).
    """
    if symbol not in _broker_memory:
        _broker_memory[symbol] = {"total_slippage": 0.0, "trades": 0, "avg_slippage": 0.0}
        
    bm = _broker_memory[symbol]
    bm["total_slippage"] += slippage_amount
    bm["trades"] += 1
    bm["avg_slippage"] = bm["total_slippage"] / bm["trades"]
    
    _save_to_disk()
    log.info(f"🧠 Memory: Recorded ${slippage_amount:.2f} slippage for {symbol}. New avg: ${bm['avg_slippage']:.2f}")


# ═══════════════════════════════════════════════════════════════
# HISTORICAL DRAWDOWN TRACKING
# ═══════════════════════════════════════════════════════════════

def record_drawdown(symbol: str, drawdown_pct: float, regime: str):
    """
    Record a severe drawdown event to warn Claude in similar future conditions.
    """
    if symbol not in _historical_drawdowns:
        _historical_drawdowns[symbol] = []
        
    _historical_drawdowns[symbol].append({
        "drawdown_pct": drawdown_pct,
        "regime": regime,
        "timestamp": time.time()
    })
    
    # Keep last 5 events
    if len(_historical_drawdowns[symbol]) > 5:
        _historical_drawdowns[symbol] = _historical_drawdowns[symbol][-5:]
        
    _save_to_disk()
    log.info(f"🧠 Memory: Recorded severe drawdown ({drawdown_pct}%) during {regime} for {symbol}.")


# ═══════════════════════════════════════════════════════════════
# BUILD MEMORY PROMPT (included in Claude's data)
# ═══════════════════════════════════════════════════════════════

def get_memory_prompt(symbol: str) -> str:
    """
    Build a memory context string to include in Claude's prompt.
    
    Returns formatted string with:
    1. 4-Hour Session Journal
    2. Market Condition Timeline (regime changes)
    3. Last 5 decisions with timestamps
    4. Active tracked setup with timer
    5. Entry thesis if position is open
    6. Level-Specific Trade History
    7. Key Level Tested/Rejected Memory
    8. Last 2 losing trades with what went wrong
    9. Flip-flop warnings
    """
    now = time.time()
    lines = []
    has_content = False
    
    # ─── 4-Hour Session Journal ───────────────────────────
    journal = _journal.get(symbol)
    if journal and "memo" in journal:
        journal_age_min = int((now - journal.get("timestamp", now)) / 60)
        # Only inject if less than 4 hours old
        if journal_age_min < (4 * 60):
            has_content = True
            lines.append("─── 📓 YOUR 4-HOUR SESSION JOURNAL ──────────────")
            lines.append(f"Written {journal_age_min} mins ago by YOU.")
            lines.append(f"H4 Trend: {journal.get('h4_trend', 'UNKNOWN')}")
            if "last_trade" in journal:
                lines.append(f"Last Trade: {journal['last_trade']}")
            lines.append(f"Your Note: \"{journal['memo']}\"")
            lines.append("")
    
    # ─── Market Condition Memory (Regime Timeline) ────────
    timeline = _regime_timeline.get(symbol, [])
    if timeline:
        # Only show entries from the last 6 hours
        recent = [r for r in timeline if (now - r["timestamp"]) / 3600 < 6]
        if recent:
            has_content = True
            lines.append("─── 🌊 MARKET CONDITION MEMORY (Regime Timeline) ───")
            lines.append("How the market has behaved recently:")
            for r in recent:
                age_min = int((now - r["timestamp"]) / 60)
                if age_min < 60:
                    t_str = f"{age_min}min ago"
                else:
                    t_str = f"{age_min // 60}h {age_min % 60}min ago"
                lines.append(f"  [{t_str}] → {r['regime']}")
            
            # Detect if we just transitioned
            if len(recent) >= 2:
                prev_regime = recent[-2]["regime"]
                curr_regime = recent[-1]["regime"]
                if prev_regime != curr_regime:
                    lines.append(f"  ⚠️ REGIME SHIFT: {prev_regime} → {curr_regime}")
                    lines.append(f"     Be cautious — strategy that worked in {prev_regime} may not work now.")
            lines.append("")
    
    # ─── Mistake Memory (Rule Violations) ─────────────────
    violations = _rule_violations.get(symbol, [])
    if violations:
        recent_violations = [v for v in violations if (now - v["timestamp"]) / 3600 < 24]
        if recent_violations:
            has_content = True
            lines.append("─── 🚫 RULE VIOLATIONS (Mistake Memory) ────────────")
            lines.append("You recently made these errors and your trades were BLOCKED:")
            for v in recent_violations[-3:]:
                v_age = int((now - v["timestamp"]) / 60)
                v_time = f"{v_age}min ago" if v_age < 60 else f"{v_age // 60}h ago"
                lines.append(f"  ❌ [{v_time}] {v['reason']}")
            lines.append("  → DO NOT repeat these mistakes in your next analysis.")
            lines.append("")
            
    # ─── Broker Behavior Memory (Slippage) ────────────────
    bm = _broker_memory.get(symbol)
    if bm and bm.get("trades", 0) >= 3:
        has_content = True
        lines.append("─── 🏦 BROKER BEHAVIOR (Execution Memory) ──────────")
        avg_slip = bm.get("avg_slippage", 0)
        lines.append(f"Broker average entry slippage: ${avg_slip:.2f} (over {bm['trades']} trades)")
        if avg_slip > 0.30:
            lines.append("  ⚠️ HIGH SLIPPAGE DETECTED: Factor this into your Stop Loss distance.")
        lines.append("")
        
    # ─── Historical Drawdown Memory ───────────────────────
    drawdowns = _historical_drawdowns.get(symbol, [])
    if drawdowns:
        # Check if the current market regime has caused massive drawdowns before
        current_regime = _regime_timeline.get(symbol, [{"regime": "UNKNOWN"}])[-1]["regime"]
        matching_drawdowns = [d for d in drawdowns if d["regime"] == current_regime]
        
        if matching_drawdowns:
            has_content = True
            lines.append("─── 📉 HISTORICAL DRAWDOWN WARNING ─────────────────")
            lines.append(f"⚠️ DANGER: In the past, the current regime ({current_regime}) caused severe drawdowns.")
            for d in matching_drawdowns[-2:]:
                d_age = int((now - d["timestamp"]) / 86400)
                lines.append(f"  - {d_age} days ago: {d['drawdown_pct']}% drawdown to Stop Loss.")
            lines.append("  → Play extreme defense. Reduce your position size or demand perfect setups.")
            lines.append("")
            
    # ─── Previous Decisions ───────────────────────────────
    entries = _memory.get(symbol, [])
    if entries:
        has_content = True
        lines.append("─── 🧠 YOUR PREVIOUS DECISIONS (Memory) ────────────")
        lines.append("Review your past reasoning. Build on it — don't start fresh each time.")
        lines.append("")
        
        for rec in entries[-5:]:
            age_min = int((now - rec["timestamp"]) / 60)
            
            if age_min < 60:
                time_str = f"{age_min}min ago"
            else:
                time_str = f"{age_min // 60}h {age_min % 60}min ago"
            
            price_str = f"${rec['current_price']:,.2f}" if rec['current_price'] > 0 else "N/A"
            reason_short = rec["reason"][:120] + "..." if len(rec["reason"]) > 120 else rec["reason"]
            
            lines.append(
                f"  [{time_str}] {rec['action']} {rec['confidence']}% @ {price_str} | "
                f"Bias: {rec['trend_bias']} | "
                f"Sweep: {'Yes' if rec['sweep_detected'] else 'No'}"
            )
            lines.append(f"    → {reason_short}")
            lines.append("")
    
    # ─── Active Setup Tracking ────────────────────────────
    tracking = _tracking_setups.get(symbol)
    if tracking:
        has_content = True
        elapsed_min = int((now - tracking["started_at"]) / 60)
        elapsed_hours = elapsed_min / 60
        
        if elapsed_hours >= SETUP_EXPIRY_HOURS:
            lines.append(f"⏰ TRACKED SETUP EXPIRED ({elapsed_min} min / {SETUP_EXPIRY_HOURS}h limit)")
            lines.append(f"   You were watching: {tracking['setup'][:150]}")
            lines.append(f"   Price when started: ${tracking['price_at_start']:,.2f}")
            lines.append(f"   This setup did NOT materialize in {SETUP_EXPIRY_HOURS} hours.")
            lines.append(f"   → ABANDON this thesis. Look for a completely NEW setup with fresh eyes.")
            lines.append("")
            del _tracking_setups[symbol]
            _save_to_disk()
        else:
            remaining_min = int((SETUP_EXPIRY_HOURS * 60) - elapsed_min)
            lines.append(f"📌 ACTIVE TRACKED SETUP ({elapsed_min} min elapsed, {remaining_min} min remaining)")
            lines.append(f"   Bias: {tracking['bias']} | Started @ ${tracking['price_at_start']:,.2f}")
            if tracking['target_entry'] > 0:
                lines.append(
                    f"   Target: Entry ${tracking['target_entry']:,.2f} | "
                    f"SL ${tracking['target_sl']:,.2f} | TP ${tracking['target_tp']:,.2f}"
                )
            lines.append(f"   What you were watching: {tracking['setup'][:200]}")
            lines.append(f"   → CHECK: Has this condition been met? If YES → execute. If NO and still valid → keep waiting.")
            lines.append(f"   → If market structure CHANGED and invalidated this thesis → abandon and look fresh.")
            lines.append("")
    
    # ─── Entry Thesis (if position is open) ───────────────
    thesis = _entry_theses.get(symbol)
    if thesis:
        has_content = True
        entered_ago = int((now - thesis.get("entered_at", now)) / 60)
        lines.append(f"📝 YOUR ENTRY THESIS (you entered this trade {entered_ago} min ago)")
        lines.append(f"   {thesis['action']} @ ${thesis['entry_price']:,.2f} | Confidence: {thesis['confidence']}%")
        lines.append(f"   SL: ${thesis['stop_loss']:,.2f} | TP: ${thesis['take_profit']:,.2f}")
        lines.append(f"   WHY you entered: {thesis['thesis'][:250]}")
        lines.append(f"   → Is this thesis STILL VALID? If structure broke → CLOSE. If valid → HOLD or MOVE_SL_BE.")
        lines.append("")
    
    # ─── Level-Specific Trade History ─────────────────────
    level_hist = _level_history.get(symbol, [])
    if level_hist:
        # Group trades by price zone
        zone_stats = {}  # zone_key -> {"wins": int, "losses": int, "net_pnl": float, "actions": set}
        for trade in level_hist:
            zone = _level_key(trade["level"])
            if zone not in zone_stats:
                zone_stats[zone] = {"wins": 0, "losses": 0, "net_pnl": 0.0, "actions": set(), "level": trade["level"]}
            if trade["won"]:
                zone_stats[zone]["wins"] += 1
            else:
                zone_stats[zone]["losses"] += 1
            zone_stats[zone]["net_pnl"] += trade["pnl"]
            zone_stats[zone]["actions"].add(trade["action"])
        
        # Only show zones with 2+ trades (meaningful pattern)
        meaningful = {k: v for k, v in zone_stats.items() if (v["wins"] + v["losses"]) >= 2}
        if meaningful:
            has_content = True
            lines.append("─── 📊 LEVEL-SPECIFIC TRADE HISTORY ────────────────")
            lines.append("Your past performance at specific price zones:")
            for zone_key, stats in sorted(meaningful.items(), key=lambda x: x[1]["net_pnl"]):
                total = stats["wins"] + stats["losses"]
                wr = (stats["wins"] / total * 100) if total > 0 else 0
                actions_str = "/".join(stats["actions"])
                emoji = "🟢" if wr >= 60 else "🔴" if wr < 40 else "🟡"
                lines.append(
                    f"  {emoji} Near ${stats['level']:,.2f}: {stats['wins']}W / {stats['losses']}L "
                    f"({wr:.0f}% WR) | Net: ${stats['net_pnl']:,.2f} | Trades: {actions_str}"
                )
                if wr < 40 and total >= 2:
                    lines.append(f"     🛑 DANGER ZONE: You keep losing at this level. AVOID or require extra confirmation.")
            lines.append("")
    
    # ─── Key Level Tested/Rejected Memory ─────────────────
    tested = _tested_levels.get(symbol, {})
    if tested:
        # Only show levels tested 2+ times or rejected 1+ times
        relevant = {
            k: v for k, v in tested.items()
            if v["times_rejected"] >= 1 or v["times_tested"] >= 3
        }
        # Only show levels from last 12 hours
        relevant = {
            k: v for k, v in relevant.items()
            if (now - v["last_tested"]) / 3600 < 12
        }
        if relevant:
            has_content = True
            lines.append("─── 🔑 KEY LEVEL MEMORY (Tested & Rejected) ────────")
            lines.append("Levels that have been tested multiple times today:")
            for lvl_key, lvl_data in sorted(relevant.items(), key=lambda x: x[1]["times_rejected"], reverse=True):
                age_min = int((now - lvl_data["last_tested"]) / 60)
                if age_min < 60:
                    t_str = f"{age_min}min ago"
                else:
                    t_str = f"{age_min // 60}h ago"
                
                reject_warning = ""
                if lvl_data["times_rejected"] >= 2:
                    reject_warning = " ← 🛑 MULTIPLE REJECTIONS — DO NOT retry this level"
                elif lvl_data["times_rejected"] >= 1:
                    reject_warning = " ← ⚠️ LOST here before — require extra confirmation"
                
                lines.append(
                    f"  ${lvl_data['level']:,.2f}: Tested {lvl_data['times_tested']}x, "
                    f"Rejected {lvl_data['times_rejected']}x (last: {t_str}){reject_warning}"
                )
            lines.append("")
    
    # ─── Recent Losses (learn from mistakes) ──────────────
    losses = _recent_losses.get(symbol, [])
    if losses:
        has_content = True
        lines.append(f"─── ❌ YOUR LAST {len(losses)} LOSING TRADE(S) — LEARN FROM THESE ───")
        lines.append("Understand WHY you lost. Do NOT repeat the same mistake.")
        lines.append("")
        
        for loss in losses[-MAX_LOSSES:]:
            loss_ago = int((now - loss["timestamp"]) / 60)
            if loss_ago < 60:
                loss_time = f"{loss_ago}min ago"
            elif loss_ago < 1440:
                loss_time = f"{loss_ago // 60}h {loss_ago % 60}min ago"
            else:
                loss_time = f"{loss_ago // 1440}d {(loss_ago % 1440) // 60}h ago"
            
            lines.append(f"  ❌ {loss['action']} @ ${loss['entry_price']:,.2f} → ${loss['exit_price']:,.2f} ({loss_time})")
            lines.append(f"     PnL: ${loss['pnl']:,.2f} | Exit: {loss['exit_reason']}")
            lines.append(f"     Confidence was: {loss['confidence_at_entry']}% | Sweep: {'Yes' if loss['sweep_at_entry'] else 'No'}")
            lines.append(f"     YOUR THESIS WAS: {loss['entry_thesis'][:200]}")
            lines.append(f"     → What went wrong? Analyze and AVOID repeating this pattern.")
            lines.append("")
    
    # ─── Flip-Flop Detection ─────────────────────────────
    if len(entries) >= 2:
        biases = [e["trend_bias"] for e in entries[-3:]]
        if "BULLISH" in biases and "BEARISH" in biases:
            has_content = True
            lines.append("⚠️ CONSISTENCY WARNING: You flipped between BULLISH and BEARISH in recent calls.")
            lines.append("   Pick a direction based on structure and COMMIT. Don't flip every 15 minutes.")
            lines.append("")
    
    if not has_content:
        return ""
    
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def get_entry_thesis(symbol: str) -> dict:
    """Get the entry thesis for an open position."""
    return _entry_theses.get(symbol, {})


def clear_entry_thesis(symbol: str):
    """Clear entry thesis (position closed without recording outcome)."""
    _entry_theses.pop(symbol, None)
    _save_to_disk()


def clear_memory(symbol: str = None):
    """Clear memory for a symbol or all symbols."""
    if symbol:
        _memory.pop(symbol, None)
        _tracking_setups.pop(symbol, None)
        _entry_theses.pop(symbol, None)
        _recent_losses.pop(symbol, None)
        _rule_violations.pop(symbol, None)
        _level_history.pop(symbol, None)
        _regime_timeline.pop(symbol, None)
        _tested_levels.pop(symbol, None)
        _broker_memory.pop(symbol, None)
        _historical_drawdowns.pop(symbol, None)
    else:
        _memory.clear()
        _tracking_setups.clear()
        _entry_theses.clear()
        _recent_losses.clear()
        _rule_violations.clear()
        _level_history.clear()
        _regime_timeline.clear()
        _tested_levels.clear()
        _broker_memory.clear()
        _historical_drawdowns.clear()
    _save_to_disk()


def get_tracking_info(symbol: str) -> dict:
    """Get the current tracking setup info for a symbol."""
    return _tracking_setups.get(symbol, {})


def get_wake_triggers(symbol: str) -> dict:
    """Get the current wake triggers for a symbol (works for both entry tracking AND position management)."""
    triggers = _wake_triggers.get(symbol, {})
    if not triggers:
        return {}
    # Check if triggers are expired (older than 2 hours)
    if (time.time() - triggers.get("set_at", 0)) / 3600 >= 2.0:
        del _wake_triggers[symbol]
        return {}
    return triggers


def clear_wake_triggers(symbol: str):
    """Clear wake triggers for a symbol (called when a trade closes)."""
    if symbol in _wake_triggers:
        log.info(f"🎯 Wake Trigger CLEARED for {symbol} (trade closed)")
        del _wake_triggers[symbol]
        _save_to_disk()
