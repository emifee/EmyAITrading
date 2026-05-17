# Emy AI Trading System — Developer Documentation

Welcome to the **Emy AI Trading System**. This document is designed for developers, quants, and engineers to understand exactly how the system is architected, the theoretical edge (how it makes money), and the complex risk-management constraints wrapped around the AI.

---

## 1. Core Trading Philosophy (How It Makes Money)

Emy AI does not rely on a single, rigid strategy. Instead, it relies on **Regime-Aware Adaptability** powered by Large Language Models (Claude 3.5 Sonnet).

Financial markets rotate through distinct "regimes" (Trending, Ranging, Transitioning). Most algorithmic bots fail because they run a Trend-Following strategy during a Ranging market, resulting in "death by a thousand cuts." 

Emy AI solves this by using Python to mathematically identify the market regime in real-time, and then physically swapping out Claude's "Prompt Playbook" before asking it for a decision.

- **Trending Market**: Claude is instructed to buy pullbacks to EMAs and VWAP.
- **Ranging Market**: Claude is instructed to ignore EMAs, sell the upper Bollinger Bands, and buy the session lows (Mean Reversion).
- **Transitioning Market**: Claude is forced into a neutral gear, restricted to taking only 2.5:1 R:R trades that follow a confirmed liquidity sweep.

**The "Edge"**: The AI finds the high-probability entries using complex pattern recognition (Liquidity Sweeps, Market Structure, Fibonacci), while Python serves as the "Invisible Hand" that mathematically enforces perfect risk management and session filtering.

---

## 2. System Architecture

The system is built on an asynchronous, event-driven architecture using Python's **Twisted Reactor**. It operates in three distinct tiers:

### Tier 1: Real-Time Data Aggregation (The Eyes)
- **`data/tick_aggregator.py`**: Connects via TCP to the cTrader FIX/OpenAPI. It ingests sub-second tick data and builds synthetic 1-minute and 15-minute candles locally in memory. This avoids constant, expensive REST API requests to the broker.
- **`data/indicators.py`**: Uses `pandas` and `ta` libraries to calculate ATR, RSI, MACD, Bollinger Bands, EMAs, VWAP, and custom Liquidity Sweep detection.

### Tier 2: The Local Monitor (The Shield)
- Runs every 5 minutes (or via tick-events) locally for $0.00 compute cost.
- **`main.py (monitor_cycle)`**: Manages open trades without waking up the AI. It handles:
  - Moving Stop Losses to Breakeven (+ commission buffer) at 1:1 R:R.
  - Activating a Trailing Stop Loss as trades go deep into profit.
  - Enforcing Friday "Weekend Gap" emergency closures.
  - **"Guard Dog" Logic**: If the price gets within 15% of a Stop Loss or Take Profit, it asynchronously wakes up Tier 3 for an emergency AI evaluation.

### Tier 3: The AI Analyst (The Brain)
- Runs every 30 minutes (or when triggered by the Guard Dog).
- **`main.py (analysis_cycle)`**: Compiles all technical data into a heavily structured text prompt.
- **`ai/prompt_builder.py`**: Injects the correct strategy playbook based on the current Market Regime.
- Sends the payload to Anthropic's `claude-3-5-sonnet` API (utilizing Prompt Caching to reduce costs by 90%). 
- Parses the JSON response to execute `BUY`, `SELL`, `HOLD`, `CLOSE_TRADE`, or `PARTIAL_CLOSE`.

---

## 3. The "Invisible Hand" Risk Management

Large Language Models are excellent at pattern recognition but terrible at exact math. To guarantee the system stays profitable, Python enforces strict mathematical guardrails that overrule Claude if necessary.

### Dynamic ATR Stop Loss Override
If Claude decides to enter a trade with a dangerously tight Stop Loss (which would likely get taken out by algorithmic wicks), Python intercepts the order. It calculates `1.5 * ATR (Average True Range)` and mathematically widens the Stop Loss (and Take Profit) to ensure the trade is placed safely outside the statistical market noise.

### Automated Risk Sizing & Position Scaling
Defined in `trading/risk_manager.py` -> `calculate_position_size()`. 
Position sizes are not static. They are dynamically scaled based on two factors:
1. **The Market Regime**: 
   - `TRENDING`: 100% Risk (0.30 lots max)
   - `RANGING_CHOPPY`: 75% Risk
   - `TRANSITIONING`: 50% Risk (Capital preservation during chaos)
2. **The Losing Streak**: If the bot hits a losing streak, position sizes are halved. If it loses 5 in a row, a global Circuit Breaker trips and halts trading until manually reset by the admin via Telegram.
3. **Drawdown Scaling**: Python tracks the `_peak_balance` in memory. If current equity drops >5% from the peak, all position sizes are globally multiplied by 0.5x to defend capital, regardless of the streak count.

### Asymmetric R:R Floors
Python mathematically enforces strict Risk/Reward floors based on the regime. If Claude outputs a setup that does not meet these floors, it is rejected:
- `TRENDING`: 1.5:1 minimum R:R.
- `RANGING_CHOPPY`: 2.0:1 minimum R:R.
- `TRANSITIONING`: 2.5:1 minimum R:R.

### Time-of-Day Kill Zones
Gold (XAUUSD) volume dies during the Asian Session, leading to massive fake-outs. Inside `trading/regime_manager.py`, Python checks the UTC time. Between 21:00 UTC and 06:00 UTC, the system mathematically forces the regime into `RANGING_CHOPPY`, preventing the AI from buying low-volume breakouts.

### Dual-Timeframe Regime Engine
The `RegimeManager` looks at both **M15 ADX** and **H1 ADX** simultaneously. 
- It requires the M15 ADX to cross above 25 (Fast Breakout Trigger).
- It requires the H1 ADX to be above 20 (Slow HTF Confirmation).
If both timeframes do not agree, the market is classified as `TRANSITIONING`, which invokes strict entry criteria.

## 4. Signal Quality Optimizations (Prompt Engineering)

### Dynamic Indicator Stripping
LLMs perform worse when flooded with conflicting data. `ai/prompt_builder.py` dynamically strips irrelevant indicators from the prompt based on the regime:
- **TRENDING**: Claude only sees EMAs, VWAP, ATR, and MACD.
- **RANGING**: Claude only sees Bollinger Bands, Session Levels, RSI.
This mathematically prevents Claude from hallucinating signals off the wrong indicators.

### Strict Confidence Enforcement
Claude is forced to output `"confidence_level": "low" | "medium" | "high"` in its JSON response. If it outputs `"low"`, Python's `decision_parser.py` forcefully rejects the trade.

---

## 5. Key Files & Directory Structure

* `main.py`: The application entry point. Initializes the Twisted reactor, connects to cTrader, and spins up the asynchronous loops (`monitor_cycle`, `analysis_cycle`).
* `config.py`: Centralized environment variables and mathematical constants (Lot sizes, Buffers, Multipliers, API keys).
* `ai/prompt_builder.py`: Formats the tabular pandas data into readable Markdown for Claude. Injects dynamic playbooks and strips indicators.
* `ai/decision_parser.py`: Sanitizes and parses the JSON output from Claude. Enforces Confidence strings.
* `data/ctrader_client.py`: Async wrappers for the cTrader OpenAPI protocol (Order placement, modification, closure).
* `data/trade_journal.py`: Logs trade entries and exits to SQLite. Implements Per-Regime Expectancy Tracking.
* `trading/risk_manager.py`: Calculates ATR-based position sizing, streak management, drawdown scaling, and global drawdown tracking.
* `trading/regime_manager.py`: Evaluates ADX across multiple timeframes and enforces Session Kill Zones.

## 6. Development & Testing
To test the core AI logic without executing real trades, developers can run `python test_trade.py`. This script mocks the cTrader client, generates simulated market data, invokes Claude, and prints the theoretical output and JSON parsing results.
