# 🚀 Emy AI Trading System: Future Roadmap

This document serves as a repository for future architectural ideas and features. These concepts have been brainstormed and validated, but are shelved for future implementation to prioritize current stability and budget.

---

## 📡 Option B: The "Python Tripwire" (Free 15-Min Radar)
**The Problem:** Currently, the system calls Claude every 30 minutes. Lowering this to 15 minutes would perfectly double the API costs, which is not ideal for tight budgets.
**The Solution:** Build a 100% FREE Python monitoring loop that acts as an early-warning radar.
* **How it works:** Python calculates M15 indicators (EMA crosses, RSI, ATR, Volume) locally every 1 to 5 minutes.
* **The Trigger:** If Python detects a major structural event (e.g., Price touches the Asian Session High, or Volume spikes by 300%), it instantly wakes up Claude Sonnet and forces an out-of-schedule analysis cycle.
* **The Benefit:** We get lightning-fast execution on breakouts (equivalent to 1-minute monitoring) while keeping API costs at near zero, because Claude only wakes up when there is actual price action happening.

---

## 🌍 Option C: Multi-Pair MoE Architecture (Mixture of Experts)
**The Problem:** Gold (XAUUSD) can get stuck in tight, untradeable ranges for days. We want to trade EURUSD and USDJPY, but sending 3 charts to Claude Sonnet every 30 minutes would triple (3x) the API cost.
**The Solution:** A two-tier "Radar and Sniper" AI Architecture.
* **Tier 1 (The Radar):** We use an extremely cheap LLM (like Claude 3.5 Haiku) or the *Python Tripwire* to cheaply scan XAUUSD, EURUSD, and USDJPY every 15 minutes. 
* **Tier 2 (The Sniper):** If Haiku spots a "Perfect A+ Setup" on EURUSD, it passes ONLY that specific pair to Claude 3.5 Sonnet. Sonnet then performs the deep, expensive Chain-of-Thought execution analysis.
* **The Benefit:** Institutional-grade multi-pair analysis without the institutional-grade API bills. We catch the best setups across the entire Forex market while only paying for Sonnet when it's time to pull the trigger.

---

## 📈 Option A (Implemented May 2026)
*Dynamic Confidence-Based Position Sizing has already been implemented.*
- 60-65% Confidence = 0.5% Risk
- 66-75% Confidence = 1.0% Risk
- 76%+ Confidence = 2.0% Risk

---

## 🏃 Option D: Partial Scaling with a Runner (Advanced TP Management)
**The Problem:** Currently, the system closes 100% of its volume when hitting the Take Profit. This secures wins but causes the system to miss out on massive "Black Swan" trend continuations (e.g., Gold running an extra 300 pips after hitting TP).
**The Solution:** Build a Python-based automatic partial scaling engine.
* **How it works:** Python actively monitors the open position. The exact second price hits TP1, Python intercepts it and executes a "Partial Close" API command to sell 80% of the position volume.
* **The Trailing Stop:** Simultaneously, Python moves the Stop Loss to Breakeven (or TP1) and extends the Take Profit into the distance (TP2).
* **The Benefit:** We secure guaranteed profit on the initial 80%, mathematically eliminating all risk, while leaving a 20% "runner" to capture massive infinite-upside trends without spending extra API tokens.

---

## 🛡️ Option E: Institutional Win-Rate Filters (70-80% Optimization)
**The Problem:** The system currently relies entirely on technical analysis. This leaves it vulnerable to random, chaotic market events (like news drops) that cause unnecessary losses and lower the overall win rate.
**The Solution:** Build a series of "Smart Python Filters" that actively forbid Claude from trading in low-probability environments.
1. **The Macro Calendar Filter:** Connect Python to an Economic Calendar API. If High-Impact news (NFP, CPI) is scheduled within 2 hours, block all new trades to avoid random volatility stop-outs.
2. **Retail Sentiment Integration:** Pull "Client Sentiment" data from the broker (e.g., "82% of retail is short"). Feed this to Claude to use as a contrarian indicator, trading *against* the retail herd.
3. **Session Liquidity Guard:** Hardcode Python to only allow entries during London and New York overlaps, avoiding the choppy, low-volume "Dead Zones" of the late Asian session.
4. **Absolute Trend Alignment:** Force a rule where Claude can only buy if both the Daily (D1) and 4-Hour (H4) EMAs are bullish, completely eliminating low-probability counter-trend trades.

---

## 🔬 Phase 6: The "Statistical Incubation" Protocol (Current Status)
**The Golden Rule of Quant Trading: Do not over-engineer a system that is already profitable.**
Adding too many features can break what is already working. Therefore, the system is now entering a **14-Day Statistical Incubation Phase**. No new features will be added. We simply let it run, let it breathe, and monitor the live data.

### 🚨 Contingency & Diagnostic Plan (If the bot bleeds this week)
If the system starts bleeding capital in the upcoming week, we do **not** panic and rewrite the code. Instead, we follow this diagnostic tracking protocol:

1. **Check the Hourly Edge Report:** Review `edge_report.txt`. Did the bleed happen exclusively in a new "Dead Zone" (e.g., 18:00 UTC)? If so, the fix is simply hardcoding a time-block.
2. **Review the Market Regime:** Was the market in a `TRANSITIONING` or `UNKNOWN` state all week? The system hates low-volatility chop. We can increase the ADX requirement to completely avoid chop.
3. **Analyze the Confidence vs. Result:** Did the bot lose trades where it was only 60% confident? If yes, we simply raise `MIN_CONFIDENCE` from 60 to 70. 
4. **Examine the Fundamental Trigger:** Did the losses occur exactly 5 minutes after a High-Impact news drop (like NFP)? If so, the Macro Feed is working, but Claude failed to respect it. We would then hardcode Python to forcefully kill all trades during High-Impact news windows.

By tracking these 4 metrics during the incubation phase, we can fix any "bleeding" with microscopic surgical tweaks, rather than massive code rewrites.
