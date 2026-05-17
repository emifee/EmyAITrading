"""
risk_manager.py — Hardcoded risk management rules.

⚠️  CRITICAL: This module OVERRIDES Claude if needed.
Claude is the analyst. Python enforces the rules.
Never let AI have unchecked control over trade sizing or loss limits.
"""

from datetime import datetime, timedelta
from utils.logger import log
import config


# Track daily losses and last loss time in memory
_daily_losses = []
_last_loss_time = None
_peak_balance = None


# --- Risk State Architecture V2 ---
class RiskState:
    """In-memory state tracker for dynamic risk management."""
    def __init__(self):
        self.streak_count = 0
        self.is_paused = False

    def update(self, pnl: float, balance: float):
        """Update streak based on closed trade P&L."""
        if self.is_paused:
            return  # Needs manual reset

        # Define a "win" as > 0.5% of account balance (to ignore breakevens)
        win_threshold = balance * 0.005 

        if pnl > win_threshold:
            log.info(f"📈 RiskState: Winning trade (+${pnl:,.2f})! Resetting streak to 0.")
            self.streak_count = 0
        elif pnl < -10.0: # Ignore tiny sub-$10 spread losses from BE
            self.streak_count += 1
            log.warning(f"📉 RiskState: Losing trade (-${abs(pnl):,.2f}). Streak is now {self.streak_count}.")
            
            if self.streak_count >= 5:
                self.is_paused = True
                log.error("🛑 CIRCUIT BREAKER (L5): Trading paused. Manual intervention required (/reset_risk).")

    def reset(self):
        self.streak_count = 0
        self.is_paused = False
        log.info("🔄 RiskState: Streak and circuit breaker manually reset.")

risk_state = RiskState()

STREAK_MULTIPLIERS = {
    0: 1.0,
    1: 1.0,
    2: 1.0,
    3: 1.0,
    4: 1.0
}
MIN_RISK_PCT = 0.25

def calculate_position_size(balance: float, risk_pct: float,
                            entry: float, stop_loss: float, market_regime: str = "UNKNOWN") -> float:
    """
    Calculate position size based on ATR-derived risk, with dynamic streak and regime multipliers.
    """
    if risk_state.is_paused:
        log.warning("🛑 Position size = 0 (Circuit breaker active — L5 streak).")
        return 0.0

    # Cap base risk
    base_risk_pct = min(risk_pct, config.MAX_RISK_PER_TRADE)

    # Apply Streak Multiplier
    streak_multiplier = STREAK_MULTIPLIERS.get(risk_state.streak_count, 0.0)
    
    # Apply Regime Multiplier
    regime_multiplier = 1.0
    if market_regime == "TRANSITIONING":
        regime_multiplier = 0.50  # Half risk in chaos
    elif market_regime == "RANGING_CHOPPY":
        regime_multiplier = 0.75  # 75% risk in ranges
        
    # Track Peak Balance for Drawdown
    global _peak_balance
    if _peak_balance is None or balance > _peak_balance:
        _peak_balance = balance

    # Apply Drawdown Multiplier
    drawdown_multiplier = 1.0
    if _peak_balance and balance < _peak_balance:
        drawdown_pct = ((_peak_balance - balance) / _peak_balance) * 100
        if drawdown_pct >= 5.0:
            drawdown_multiplier = 0.50
            log.debug(f"📉 Drawdown ({drawdown_pct:.1f}%) > 5%. Applying 0.5x global multiplier.")

    combined_multiplier = streak_multiplier * regime_multiplier * drawdown_multiplier
    final_risk_pct = max(MIN_RISK_PCT, base_risk_pct * combined_multiplier)

    # Circuit breaker fail-safe
    if combined_multiplier == 0.0:
        log.warning("🛑 Position size = 0 (Multiplier is 0).")
        return 0.0

    risk_amount = balance * (final_risk_pct / 100)
    pip_risk = abs(entry - stop_loss)

    if pip_risk == 0:
        log.warning("Stop loss equals entry — cannot calculate position size")
        return 0.0

    position_size = risk_amount / pip_risk

    # Cap at max lot size (convert lots to units: 0.30 lots = 30 units)
    max_lots = getattr(config, 'MAX_LOT_SIZE', 0.30)
    max_units = max_lots * 100  # 1 lot = 100 units in our system
    if position_size > max_units:
        log.warning(f"Position size {position_size:.2f} units ({position_size/100:.2f} lots) exceeds max {max_lots} lots — capping to {max_units} units")
        position_size = max_units

    result = round(position_size, 2)

    # Audit Trail
    log.info(
        f"Risk Audit: Base {base_risk_pct}% | Streak: {risk_state.streak_count} | "
        f"Multiplier: {combined_multiplier}x | Final Risk: {final_risk_pct}% | "
        f"Size: {result/100:.2f} lots"
    )

    return result


def daily_loss_exceeded(balance: float, daily_pnl: float) -> bool:
    """
    Check if the day's cumulative losses exceed the daily loss limit.

    Args:
        balance: Current account balance.
        daily_pnl: Today's cumulative P&L (negative for losses).

    Returns:
        bool: True if daily loss limit has been hit.
    """
    try:
        if balance == 0:
            log.warning("Cannot check daily loss — balance is 0")
            return True  # Safety: stop trading if we can't determine balance

        loss_pct = abs(daily_pnl / balance * 100) if daily_pnl < 0 else 0

        if loss_pct >= config.MAX_DAILY_LOSS:
            log.warning(
                f"🛑 DAILY LOSS LIMIT HIT: {loss_pct:.1f}% loss "
                f"(${daily_pnl:,.2f}) exceeds {config.MAX_DAILY_LOSS}% max"
            )
            return True

        log.debug(f"Daily P&L: ${daily_pnl:,.2f} ({loss_pct:.1f}% of balance)")
        return False

    except Exception as e:
        log.error(f"Failed to check daily loss: {e}")
        return True  # Safety: stop trading if check fails


def check_drawdown(current_equity: float) -> bool:
    """
    Circuit breaker — check if account has dropped too far from peak.

    Args:
        current_equity: Current account equity.

    Returns:
        bool: True if drawdown limit has been triggered.
    """
    global _peak_balance

    try:
        if _peak_balance is None or current_equity > _peak_balance:
            _peak_balance = current_equity
            log.debug(f"Peak balance updated: ${_peak_balance:,.2f}")

        if _peak_balance == 0:
            return True

        drawdown_pct = (((_peak_balance - current_equity) / _peak_balance) * 100)

        if drawdown_pct >= config.MAX_DRAWDOWN:
            log.warning(
                f"🚨 DRAWDOWN CIRCUIT BREAKER: {drawdown_pct:.1f}% drawdown "
                f"from peak ${_peak_balance:,.2f} → ${current_equity:,.2f}"
            )
            return True

        log.debug(f"Drawdown: {drawdown_pct:.1f}% (limit: {config.MAX_DRAWDOWN}%)")
        return False

    except Exception as e:
        log.error(f"Failed to check drawdown: {e}")
        return True  # Safety: stop trading if check fails


def validate_risk_reward(entry: float, stop_loss: float,
                         take_profit: float) -> bool:
    """
    Verify the trade meets minimum Risk:Reward ratio.

    Args:
        entry: Entry price.
        stop_loss: Stop loss price.
        take_profit: Take profit price.

    Returns:
        bool: True if R:R meets minimum requirement.
    """
    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)

    if risk == 0:
        log.warning("Risk is 0 — invalid R:R")
        return False

    rr_ratio = reward / risk

    if rr_ratio < config.MIN_RISK_REWARD:
        log.warning(f"R:R {rr_ratio:.1f}:1 below minimum {config.MIN_RISK_REWARD}:1")
        return False

    log.debug(f"R:R ratio: {rr_ratio:.1f}:1 ✓")
    return True


def check_cooldown() -> bool:
    """
    Check if enough time has passed since the last losing trade.

    Returns:
        bool: True if still in cooldown (should NOT trade).
    """
    global _last_loss_time

    if _last_loss_time is None:
        return False

    elapsed = datetime.utcnow() - _last_loss_time
    cooldown = timedelta(minutes=config.COOLDOWN_MINUTES)

    if elapsed < cooldown:
        remaining = cooldown - elapsed
        log.info(
            f"Cooldown active: {remaining.seconds // 60}m {remaining.seconds % 60}s "
            f"remaining after last loss"
        )
        return True

    return False


def record_loss():
    """Record that a losing trade just occurred (triggers cooldown)."""
    global _last_loss_time
    _last_loss_time = datetime.utcnow()
    log.info(f"Loss recorded — cooldown started ({config.COOLDOWN_MINUTES} min)")


def reset_peak_balance():
    """Reset peak balance tracker (e.g. on system restart)."""
    global _peak_balance
    _peak_balance = None
    log.info("Peak balance reset")
