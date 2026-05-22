import os
import json
import logging
import requests
from dotenv import load_dotenv

log = logging.getLogger("GeminiClient")

class GeminiRotator:
    def __init__(self):
        load_dotenv()
        
        # Parse comma-separated keys from .env
        keys_str = os.getenv("GEMINI_API_KEYS", "")
        if keys_str:
            self.api_keys = [k.strip() for k in keys_str.split(",") if k.strip()]
        else:
            # Fallback
            single_key = os.getenv("GEMINI_API_KEY", "")
            self.api_keys = [single_key] if single_key else []
            
        self.current_key_idx = 0
        
        if self.api_keys:
            active_key = self.api_keys[self.current_key_idx]
            masked_key = f"...{active_key[-4:]}" if len(active_key) > 4 else "INVALID"
            log.info(f"🔑 Gemini Lookout loaded API Key {self.current_key_idx + 1}/{len(self.api_keys)} ({masked_key})")

    def _rotate_key(self):
        """Switch to the next API key in the list"""
        if not self.api_keys or len(self.api_keys) <= 1:
            log.error("❌ Gemini hit Rate Limit (429) but no backup keys available in GEMINI_API_KEYS!")
            return False
            
        old_idx = self.current_key_idx
        self.current_key_idx = (self.current_key_idx + 1) % len(self.api_keys)
        
        active_key = self.api_keys[self.current_key_idx]
        masked_key = f"...{active_key[-4:]}" if len(active_key) > 4 else "INVALID"
        log.warning(f"🔄 Gemini Rate Limit (429) Hit! Swapping key: {old_idx} -> {self.current_key_idx} ({masked_key})")
        return True

    def evaluate_lookout_instructions(self, symbol: str, full_context: str, instructions: str) -> dict:
        """
        Sends the FULL market state (from Claude prompt) and Claude's instructions to Gemini.
        Returns a dictionary: {"action": "WAKE_CLAUDE" | "CLOSE_TRADE" | "MOVE_SL_BE" | "IGNORE", "reason": str}
        """
        if not self.api_keys:
            log.warning("Gemini client not initialized. Missing API keys.")
            return {"action": "IGNORE", "reason": "Gemini not configured"}

        if not instructions or instructions.strip() == "":
            return {"action": "IGNORE", "reason": "No instructions provided"}

        prompt = f"""
You are the Executive Trade Manager AI for an automated trading system.
The main trading brain (Claude) is sleeping.
Before sleeping, Claude left you these EXACT instructions for managing the current open positions or entries:
"{instructions}"

CURRENT FULL MARKET CONTEXT FOR {symbol}:
{full_context}

Evaluate the market state against Claude's instructions and the historical memory above.
You DO NOT have the authority to execute trades or close trades yourself!
Your ONLY job is to act as an aggressive alarm system for a high-frequency scalper.

🚨 SCENARIO MATCHING 🚨
Claude will usually provide you with multiple scenarios (e.g. A, B, C). 
If ANY of those scenarios are met, you MUST output "WAKE_CLAUDE"!
Because Claude is trying to scalp 10-20 times a day, you must be extremely TRIGGER-HAPPY. If the market is getting even slightly close to one of the scenarios, or if you see a sudden volume spike or 1-minute momentum burst, WAKE_CLAUDE early so he can scalp it. Do not wait for the exact dollar amount.
If the market is doing something unexpected that Claude didn't mention, or if an open trade looks like it is failing, output "WAKE_CLAUDE".
🚨 EMERGENCY BAILOUT: If there is an OPEN TRADE on this symbol, and you detect a sudden, violent momentum shift AGAINST the trade (e.g. a huge bearish engulfing candle while we are long), DO NOT wait for Claude. Output "PANIC_CLOSE" to immediately cut the loss and protect the account!
If you determine that Claude's setup is completely destroyed or INVALID based on current market structure (e.g. Claude wanted a pullback to VWAP, but price blew past it and structure broke), output "INVALIDATE".
Otherwise, if everything is completely dead and volume is zero, output "IGNORE".

You must respond with a valid JSON object containing exactly two keys:
1. "action": MUST BE EXACTLY ONE OF: "WAKE_CLAUDE", "IGNORE", "INVALIDATE", "PANIC_CLOSE"
2. "reason": A brief 1-sentence explanation of why you made this decision. IF you woke Claude up because a specific scenario was met, you MUST explicitly state which scenario (e.g. "Scenario A met: price pulled back to VWAP and printed a bullish wick").
"""
        max_retries = len(self.api_keys) if self.api_keys else 1
        
        for attempt in range(max_retries):
            api_key = self.api_keys[self.current_key_idx]
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
            
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.1
                }
            }
            
            try:
                response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    candidates = data.get("candidates", [])
                    if not candidates:
                        return {"action": "IGNORE", "reason": "Gemini returned empty response"}
                        
                    text_response = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    
                    try:
                        result = json.loads(text_response)
                        if "action" in result and "reason" in result:
                            # Backward compatibility check
                            if result.get("wake_boss") == True and result["action"] == "IGNORE":
                                result["action"] = "WAKE_CLAUDE"
                            return result
                        else:
                            return {"action": "IGNORE", "reason": "Invalid JSON schema from Gemini"}
                    except json.JSONDecodeError:
                        return {"action": "IGNORE", "reason": "JSON decode error"}
                        
                elif response.status_code == 429:
                    time.sleep(1.0)
                    if self._rotate_key():
                        continue
                    else:
                        return {"action": "IGNORE", "reason": "Gemini Rate Limit Exceeded (No backups)"}
                elif response.status_code == 503:
                    return {"action": "IGNORE", "reason": "Gemini servers overloaded (503)"}
                else:
                    return {"action": "IGNORE", "reason": f"Gemini API Error: {response.status_code}"}
                    
            except Exception as e:
                return {"action": "IGNORE", "reason": f"Gemini Request Error: {e}"}
                    
        return {"action": "IGNORE", "reason": "Max retries exceeded"}

    def evaluate_hunter_setup(self, symbol: str, full_context: str) -> dict:
        """
        Sends FULL market context to Gemini to see if it's worth waking Claude up for a new trade setup.
        Returns a dictionary: {"action": "WAKE_CLAUDE" | "IGNORE", "reason": str}
        """
        if not self.api_keys:
            return {"action": "IGNORE", "reason": "Gemini not configured"}

        prompt = f"""
You are the Hunter Scout AI for an automated trading system.
The main trading brain (Claude) is sleeping to save API costs.
Python has detected a volatility spike for {symbol}.

CURRENT FULL MARKET CONTEXT FOR {symbol}:
{full_context}

Is there a potential trading setup forming? 
Look for trends, pullbacks, or liquidity sweeps.
DO NOT wait for a perfect setup! If you see ANY momentum, ANY sweep, ANY breakout, or ANY interesting market structure forming, you MUST output "WAKE_CLAUDE".
If the chart looks even 40% aligned with a potential setup, WAKE_CLAUDE immediately. 
It is much better to wake Claude up unnecessarily than to miss a trade. Be aggressive!
Only if the market is completely dead, flat, and utterly messy with zero potential, output "IGNORE".
Otherwise, wake Claude.

You must respond with a valid JSON object containing exactly two keys:
1. "action": MUST BE EXACTLY ONE OF: "WAKE_CLAUDE", "IGNORE"
2. "reason": A brief 1-sentence explanation of why you made this decision.
"""
        max_retries = len(self.api_keys) if self.api_keys else 1
        
        for attempt in range(max_retries):
            api_key = self.api_keys[self.current_key_idx]
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
            
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.1
                }
            }
            
            try:
                response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
                if response.status_code == 200:
                    text_response = response.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    try:
                        result = json.loads(text_response)
                        if "action" in result:
                            return result
                    except:
                        pass
                elif response.status_code == 429:
                    import time
                    time.sleep(1.5)  # Backoff before rotating
                    if self._rotate_key(): continue
            except:
                pass
        return {"action": "IGNORE", "reason": "Failed to get hunter response (Rate Limited or Error)"}

# Singleton instance
gemini_rotator = GeminiRotator()
