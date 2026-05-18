"""
decision_parser.py — Validates and sanitizes Claude's trading decisions.

Ensures all required fields are present and enforces hard risk limits
before any decision reaches the order executor.
Supports Trend + Liquidity Sweep strategy output format.
"""

import json
from utils.logger import log
import config


REQUIRED_FIELDS = [
    "action",
    "confidence",
    "confidence_level",
    "entry_price",
    "stop_loss",
    "take_profit",
    "position_size_pct",
    "reason",
]


def parse_raw_response(raw: str) -> dict:
    """
    Parse Claude's raw text response into a JSON dict.

    Handles common issues like markdown code fences and extra text.

    Args:
        raw: Raw text from Claude's response.

    Returns:
        dict: Parsed decision dictionary.

    Raises:
        ValueError: If the response cannot be parsed as JSON.
    """
    try:
        # Strip markdown code fences
        cleaned = raw.strip()
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()

        # Try to extract JSON if there's surrounding text
        if not cleaned.startswith("{"):
            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start != -1 and end > start:
                cleaned = cleaned[start:end]

        decision = json.loads(cleaned)

        # Ensure optional strategy fields have defaults
        decision.setdefault("take_profit_2", 0.0)
        decision.setdefault("trend_bias", "UNKNOWN")
        decision.setdefault("sweep_detected", False)
        decision.setdefault("key_level", 0.0)
        decision.setdefault("session_grade", "C")

        log.debug(f"Parsed Claude response: {decision}")
        return decision

    except json.JSONDecodeError as e:
        log.warning(f"Claude returned text instead of JSON — building fallback response")
        log.debug(f"Raw response: {raw[:300]}")

        # Try to detect intent from the text
        raw_lower = raw.lower()
        action = "HOLD"
        reason = raw[:200] if len(raw) > 200 else raw

        # Check if Claude is recommending closing
        if any(w in raw_lower for w in ["close", "exit", "get out", "cut loss"]):
            action = "CLOSE_TRADE"
        elif any(w in raw_lower for w in ["move sl", "breakeven", "move stop"]):
            action = "MOVE_SL_BE"

        # Strip any XML tags from the reason to prevent Telegram Markdown parsing errors
        import re
        clean_reason = re.sub(r'<[^>]+>', '', reason).strip()
        # Fallback to a basic string if it's completely empty after stripping
        if not clean_reason:
            clean_reason = "Unparseable raw response from Claude."

        fallback = {
            "action": action,
            "confidence": 40,
            "confidence_level": "low",
            "entry_price": 0.0,
            "stop_loss": 0.0,
            "take_profit": 0.0,
            "take_profit_2": 0.0,
            "position_size_pct": 1.0,
            "trend_bias": "UNKNOWN",
            "sweep_detected": False,
            "key_level": 0.0,
            "session_grade": "B",
            "position_action_reason": clean_reason,
            "reason": f"[Text fallback] {clean_reason}",
        }
        log.info(f"Fallback decision: {action} — {clean_reason[:100]}")
        return fallback


def validate_decision(decision: dict, has_position: bool = False, market_regime: str = "UNKNOWN") -> bool:
    """
    Validate Claude's decision and enforce hard risk limits.

    Rules enforced:
    - All required fields must be present
    - Action must be BUY, SELL, or HOLD
    - Position size capped at MAX_RISK_PER_TRADE
    - Confidence below MIN_CONFIDENCE or confidence_level == 'low' forces HOLD
    - Asymmetric Risk:Reward floors based on regime
    - HOLD override only when no position open

    Args:
        decision: Parsed JSON decision from Claude.
        has_position: True if there's an open position being managed.
        market_regime: The current market regime string.

    Returns:
        bool: True if the decision is valid (may have been modified).
    """
    # Check all required fields
    for field in REQUIRED_FIELDS:
        if field not in decision:
            # Fallback for confidence_level hallucination
            if field == "confidence_level":
                log.warning("Claude missed 'confidence_level' string, defaulting to 'low'")
                decision["confidence_level"] = "low"
            else:
                log.warning(f"Missing required field in Claude decision: '{field}'")
                return False

    # Validate action
    valid_actions = ["BUY", "SELL", "HOLD", "CLOSE_TRADE", "MOVE_SL_BE", "PARTIAL_CLOSE"]
    if decision["action"] not in valid_actions:
        log.warning(f"Invalid action from Claude: '{decision['action']}'")
        return False

    # If HOLD — check if Claude is being overly cautious (only for NEW entries)
    if decision["action"] == "HOLD":
        conf = decision.get("confidence", 0)
        entry = decision.get("entry_price", 0)
        sl = decision.get("stop_loss", 0)
        tp = decision.get("take_profit", 0)
        reason = decision.get("reason", "").lower()

        # If managing a position, HOLD means "keep the trade" — don't override
        if has_position:
            log.info(f"Claude says HOLD on open position — keeping trade | confidence: {conf}%")
            return True

        # If confidence >= threshold AND Claude provided valid entry/SL/TP
        # → Claude described a trade setup but chickened out. Override to execute.
        if (conf >= config.MIN_CONFIDENCE and
                decision.get("confidence_level") != "low" and
                isinstance(entry, (int, float)) and entry > 0 and
                isinstance(sl, (int, float)) and sl > 0 and
                isinstance(tp, (int, float)) and tp > 0):

            # Detect direction from the reason text or SL/TP relationship
            if sl < entry and tp > entry:
                override_action = "BUY"
            elif sl > entry and tp < entry:
                override_action = "SELL"
            else:
                # Can't determine — also check keywords
                buy_words = ["bullish", "long", "buy", "above ema", "hh/hl", "higher high", "support"]
                sell_words = ["bearish", "short", "sell", "below ema", "lh/ll", "lower low", "resistance"]
                buy_score = sum(1 for w in buy_words if w in reason)
                sell_score = sum(1 for w in sell_words if w in reason)
                if buy_score > sell_score:
                    override_action = "BUY"
                elif sell_score > buy_score:
                    override_action = "SELL"
                else:
                    log.info(f"Claude says HOLD at {conf}% — can't determine direction, accepting HOLD")
                    return True

            log.warning(
                f"🔥 OVERRIDE: Claude said HOLD at {conf}% but provided valid "
                f"entry/SL/TP — overriding to {override_action}"
            )
            decision["action"] = override_action
            decision["reason"] = f"[AUTO-OVERRIDE from HOLD@{conf}%] {decision.get('reason', '')}"
            # Don't return — fall through to normal validation
        else:
            log.info(f"Claude says HOLD — confidence: {conf}% | reason: {decision.get('reason', 'N/A')}")
            return True

    # Position management actions — validate and return
    if decision["action"] in ["CLOSE_TRADE", "MOVE_SL_BE", "PARTIAL_CLOSE"]:
        reason = decision.get('position_action_reason', decision.get('reason', 'N/A'))
        log.info(f"Claude says {decision['action']} — reason: {reason}")
        return True

    # ─── Enforce Hard Limits ──────────────────────────────────

    # Cap position size at max risk
    if decision["position_size_pct"] > config.MAX_RISK_PER_TRADE:
        log.warning(
            f"Position size {decision['position_size_pct']}% exceeds max "
            f"{config.MAX_RISK_PER_TRADE}% — capping"
        )
        decision["position_size_pct"] = config.MAX_RISK_PER_TRADE

    # Confidence threshold (Numeric and String)
    if decision["confidence"] < config.MIN_CONFIDENCE or decision.get("confidence_level") == "low":
        log.warning(
            f"Confidence '{decision.get('confidence_level')}' ({decision['confidence']}%) "
            f"failed strict validation — forcing HOLD"
        )
        decision["action"] = "HOLD"
        decision["reason"] = f"[Python Block: Strict Confidence Failed] {decision.get('reason', '')}"
        return True

    # Validate numeric fields
    for field in ["entry_price", "stop_loss", "take_profit"]:
        if not isinstance(decision[field], (int, float)) or decision[field] <= 0:
            log.warning(f"Invalid {field}: {decision[field]}")
            return False

    # Risk:Reward check
    entry = decision["entry_price"]
    sl = decision["stop_loss"]
    tp = decision["take_profit"]

    risk = abs(entry - sl)
    reward = abs(tp - entry)

    if risk == 0:
        log.warning("Stop loss equals entry price — invalid")
        return False

    rr_ratio = reward / risk
    # Global R:R Floor
    min_rr_floor = config.MIN_RISK_REWARD

    if rr_ratio < min_rr_floor:
        log.warning(
            f"Risk:Reward {rr_ratio:.1f}:1 below global minimum floor "
            f"of {min_rr_floor}:1 — forcing HOLD"
        )
        decision["action"] = "HOLD"
        decision["reason"] = f"[Python Block: R:R {rr_ratio:.1f}:1 is below {min_rr_floor}:1 minimum floor] {decision.get('reason', '')}"
        return True

    # ─── Option A: Dynamic Confidence-Based Position Sizing ───
    conf = decision.get("confidence", 0)
    if conf >= 76:
        decision["position_size_pct"] = 2.0  # Max Risk (A+ Sniper Setup)
        log.info(f"🎯 Dynamic Sizing: Confidence {conf}% (A+) -> Risk set to 2.0%")
    elif conf >= 66:
        decision["position_size_pct"] = 1.0  # Standard Risk (A-Grade)
        log.info(f"⚖️ Dynamic Sizing: Confidence {conf}% (A) -> Risk set to 1.0%")
    else:
        decision["position_size_pct"] = 0.5  # Low Risk (B-Grade / 60-65%)
        log.info(f"🛡️ Dynamic Sizing: Confidence {conf}% (B) -> Risk set to 0.5%")
    # Validate SL/TP direction
    if decision["action"] == "BUY":
        if sl >= entry:
            log.warning(f"BUY but SL ({sl}) >= entry ({entry}) — invalid")
            return False
        if tp <= entry:
            log.warning(f"BUY but TP ({tp}) <= entry ({entry}) — invalid")
            return False

    elif decision["action"] == "SELL":
        if sl <= entry:
            log.warning(f"SELL but SL ({sl}) <= entry ({entry}) — invalid")
            return False
        if tp >= entry:
            log.warning(f"SELL but TP ({tp}) >= entry ({entry}) — invalid")
            return False

    # Log sweep info if present
    sweep_info = ""
    if decision.get("sweep_detected"):
        sweep_info = f" | Sweep: ✅ @ ${decision.get('key_level', 0):,.2f}"

    log.info(
        f"Decision validated: {decision['action']} @ ${entry:,.2f} | "
        f"SL: ${sl:,.2f} | TP: ${tp:,.2f} | "
        f"R:R = {rr_ratio:.1f}:1 | Confidence: {decision['confidence']} | "
        f"Session: {decision.get('session_grade', '?')}{sweep_info}"
    )

    return True
