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
                            entry: float, stop_loss: float, market_regime: str = "UNKNOWN",
                            symbol: str = "XAUUSD", current_price: float = 1.0) -> float:
    """
    Calculate position size based on strict $50 fixed risk and exact lot boundaries.
    """
    # 1. Strict flat $50 risk (Non-negotiable)
    risk_amount_usd = 50.0
    
    pip_risk = abs(entry - stop_loss)
    if pip_risk == 0:
        log.warning("Stop loss equals entry — cannot calculate position size")
        return 0.0

    # Adjust risk amount if quote currency is not USD (e.g. JPY)
    if "JPY" in symbol.upper() and "USD" in symbol.upper():
        risk_amount_quote = risk_amount_usd * current_price
    else:
        risk_amount_quote = risk_amount_usd

    # Raw position size calculation (units)
    raw_position_size = risk_amount_quote / pip_risk

    # 2. Determine units per lot based on symbol
    symbol_upper = symbol.upper()
    if "XAU" in symbol_upper:
        units_per_lot = 100
        min_lots = 0.02
        max_lots = 0.08
    elif "BTC" in symbol_upper or "ETH" in symbol_upper:
        units_per_lot = 1
        min_lots = 0.01
        max_lots = 1.0
    else:
        # Forex (EURUSD, USDJPY)
        units_per_lot = 100000
        min_lots = 0.20
        max_lots = 1.0

    # Convert to lots for bounding and round to 2 decimal places (standard broker step)
    calculated_lots = round(raw_position_size / units_per_lot, 2)

    # 3. Apply Strict Non-Negotiable Bounds
    if calculated_lots < min_lots:
        log.warning(f"⚖️ Calculated {calculated_lots:.2f} lots is below {min_lots} minimum. Forcing {min_lots} lots.")
        calculated_lots = min_lots
    elif calculated_lots > max_lots:
        log.warning(f"⚖️ Calculated {calculated_lots:.2f} lots exceeds {max_lots} maximum. Forcing {max_lots} lots.")
        calculated_lots = max_lots

    # Final units
    position_size = calculated_lots * units_per_lot
    result = round(position_size, 2)

    # Audit Trail
    log.info(
        f"Risk Audit [{symbol}]: Fixed Risk $50.00 | "
        f"Bounds: [{min_lots} - {max_lots}] lots | "
        f"Size: {calculated_lots:.2f} lots ({result} units)"
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

        # Calculate the absolute initial balance for the day
        initial_balance = balance - daily_pnl
        if initial_balance <= 0:
            initial_balance = balance  # Fallback

        loss_pct = abs(daily_pnl / initial_balance * 100) if daily_pnl < 0 else 0

        if loss_pct >= config.MAX_DAILY_LOSS:
            log.warning(
                f"🛑 DAILY LOSS LIMIT HIT: {loss_pct:.1f}% loss "
                f"(${daily_pnl:,.2f}) exceeds {config.MAX_DAILY_LOSS}% max of initial balance (${initial_balance:,.2f})"
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
