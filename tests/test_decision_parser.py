"""
test_decision_parser.py — Tests for Claude decision validation and parsing.
"""

import sys
import os
import pytest
import json

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.decision_parser import parse_raw_response, validate_decision


class TestParseRawResponse:
    """Tests for JSON parsing from Claude's raw output."""

    def test_clean_json(self):
        """Should parse clean JSON."""
        raw = '{"action": "BUY", "confidence": 75, "entry_price": 3200.00}'
        result = parse_raw_response(raw)
        assert result["action"] == "BUY"
        assert result["confidence"] == 75

    def test_json_with_markdown_fences(self):
        """Should strip ```json fences."""
        raw = '```json\n{"action": "SELL", "confidence": 80}\n```'
        result = parse_raw_response(raw)
        assert result["action"] == "SELL"

    def test_json_with_surrounding_text(self):
        """Should extract JSON from surrounding text."""
        raw = 'Here is my analysis:\n{"action": "HOLD", "confidence": 40}\nThank you.'
        result = parse_raw_response(raw)
        assert result["action"] == "HOLD"

    def test_invalid_json_returns_fallback(self):
        """Should return fallback HOLD decision for non-JSON."""
        result = parse_raw_response("This is not JSON at all")
        assert result["action"] == "HOLD"


class TestValidateDecision:
    """Tests for decision validation and hard limit enforcement."""

    def _make_decision(self, **overrides):
        """Helper to create a valid decision dict."""
        base = {
            "action": "BUY",
            "confidence": 75,
            "confidence_level": "high",
            "entry_price": 3200.00,
            "stop_loss": 3185.00,
            "take_profit": 3230.00,
            "position_size_pct": 1.5,
            "reason": "Strong uptrend with RSI confirmation",
        }
        base.update(overrides)
        return base

    def test_valid_buy_decision(self):
        """Valid BUY decision should pass."""
        decision = self._make_decision()
        assert validate_decision(decision) is True
        assert decision["action"] == "BUY"

    def test_valid_sell_decision(self):
        """Valid SELL decision should pass."""
        decision = self._make_decision(
            action="SELL",
            entry_price=3200.00,
            stop_loss=3215.00,
            take_profit=3170.00,
        )
        assert validate_decision(decision) is True

    def test_hold_decision(self):
        """HOLD decision should always pass."""
        decision = self._make_decision(action="HOLD")
        assert validate_decision(decision) is True

    def test_missing_field(self):
        """Should fail if a required field is missing."""
        decision = self._make_decision()
        del decision["stop_loss"]
        assert validate_decision(decision) is False

    def test_invalid_action(self):
        """Should fail for unknown action."""
        decision = self._make_decision(action="YOLO")
        assert validate_decision(decision) is False

    def test_position_size_capped(self):
        """Position size > 2% should be capped."""
        decision = self._make_decision(position_size_pct=5.0, confidence=80)
        validate_decision(decision)
        assert decision["position_size_pct"] == 2.0

    def test_low_confidence_forces_hold(self):
        """Confidence < 60 should force HOLD."""
        decision = self._make_decision(confidence=40)
        validate_decision(decision)
        assert decision["action"] == "HOLD"

    def test_bad_rr_ratio_forces_hold(self):
        """R:R ratio < 2:1 should force HOLD."""
        decision = self._make_decision(
            entry_price=3200.00,
            stop_loss=3185.00,  # risk = 15
            take_profit=3210.00,  # reward = 10, R:R = 0.67:1
        )
        validate_decision(decision)
        assert decision["action"] == "HOLD"

    def test_buy_sl_above_entry_fails(self):
        """BUY with SL above entry should fail."""
        decision = self._make_decision(
            action="BUY",
            entry_price=3200.00,
            stop_loss=3210.00,  # SL above entry = invalid for BUY
            take_profit=3250.00,
        )
        assert validate_decision(decision) is False

    def test_sell_sl_below_entry_fails(self):
        """SELL with SL below entry should fail."""
        decision = self._make_decision(
            action="SELL",
            entry_price=3200.00,
            stop_loss=3190.00,  # SL below entry = invalid for SELL
            take_profit=3150.00,
        )
        assert validate_decision(decision) is False

    def test_zero_entry_fails(self):
        """Zero entry price should fail."""
        decision = self._make_decision(entry_price=0)
        assert validate_decision(decision) is False

    def test_sl_equals_entry_fails(self):
        """SL equal to entry should fail."""
        decision = self._make_decision(
            entry_price=3200.00,
            stop_loss=3200.00,
        )
        assert validate_decision(decision) is False
