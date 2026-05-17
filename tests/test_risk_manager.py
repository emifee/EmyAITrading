"""
test_risk_manager.py — Tests for risk management calculations.
"""

import sys
import os
import pytest
from unittest.mock import patch
from datetime import datetime, timedelta

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trading.risk_manager import (
    calculate_position_size,
    validate_risk_reward,
    check_cooldown,
    record_loss,
)


class TestCalculatePositionSize:
    """Tests for the position sizing formula."""

    def test_basic_calculation(self):
        """Standard position size calculation."""
        # $1000 balance, 2% risk = $20, SL distance = $15
        result = calculate_position_size(
            balance=1000.0,
            risk_pct=2.0,
            entry=3200.0,
            stop_loss=3185.0,
        )
        # $20 / $15 = 1.33
        assert result == 1.33

    def test_larger_balance(self):
        """Larger balance should give larger position."""
        result = calculate_position_size(
            balance=10000.0,
            risk_pct=2.0,
            entry=3200.0,
            stop_loss=3185.0,
        )
        # $200 / $15 = 13.33
        assert result == 13.33

    def test_wider_stop_loss(self):
        """Wider SL should give smaller position."""
        result = calculate_position_size(
            balance=1000.0,
            risk_pct=2.0,
            entry=3200.0,
            stop_loss=3150.0,  # $50 distance
        )
        # $20 / $50 = 0.4, with 0.5x multiplier = 0.2
        assert result == 0.2

    def test_risk_capped_at_max(self):
        """Risk percentage above max should be capped."""
        result = calculate_position_size(
            balance=1000.0,
            risk_pct=10.0,  # Would be capped to 2.0%
            entry=3200.0,
            stop_loss=3180.0,
        )
        # Capped: $20 / $20 = 1.0, with 0.5x multiplier = 0.5
        assert result == 0.5

    def test_zero_stop_distance(self):
        """SL equal to entry should return 0."""
        result = calculate_position_size(
            balance=1000.0,
            risk_pct=2.0,
            entry=3200.0,
            stop_loss=3200.0,
        )
        assert result == 0.0

    def test_sell_position(self):
        """Should work for SELL (SL above entry)."""
        result = calculate_position_size(
            balance=1000.0,
            risk_pct=2.0,
            entry=3200.0,
            stop_loss=3220.0,  # SL above for shorts
        )
        # $20 / $20 = 1.0, with 0.5x multiplier = 0.5
        assert result == 0.5


class TestValidateRiskReward:
    """Tests for R:R ratio validation."""

    def test_good_rr_ratio(self):
        """3:1 R:R should pass."""
        assert validate_risk_reward(
            entry=3200.0,
            stop_loss=3190.0,
            take_profit=3230.0,
        ) is True

    def test_exact_minimum_rr(self):
        """Exactly 2:1 R:R should pass."""
        assert validate_risk_reward(
            entry=3200.0,
            stop_loss=3190.0,
            take_profit=3220.0,
        ) is True

    def test_bad_rr_ratio(self):
        """0.5:1 R:R should fail."""
        assert validate_risk_reward(
            entry=3200.0,
            stop_loss=3190.0,
            take_profit=3205.0, # reward = 5, risk = 10
        ) is False

    def test_zero_risk(self):
        """Zero risk should fail."""
        assert validate_risk_reward(
            entry=3200.0,
            stop_loss=3200.0,
            take_profit=3210.0,
        ) is False


class TestCooldown:
    """Tests for the cooldown timer."""

    def test_no_cooldown_initially(self):
        """Should not be in cooldown at start."""
        import trading.risk_manager as rm
        rm._last_loss_time = None
        assert check_cooldown() is False

    def test_cooldown_active_after_loss(self):
        """Should be in cooldown right after recording a loss."""
        import trading.risk_manager as rm
        rm._last_loss_time = datetime.utcnow()
        assert check_cooldown() is True

    def test_cooldown_expired(self):
        """Should not be in cooldown after enough time passes."""
        import trading.risk_manager as rm
        rm._last_loss_time = datetime.utcnow() - timedelta(minutes=31)
        assert check_cooldown() is False
