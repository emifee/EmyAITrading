import sys
import os

print("\n--- TESTING ADVANCED MEMORY MODULES ---")

try:
    from ai.ai_memory import (
        record_decision, get_memory_prompt, record_regime, 
        _memory, _rule_violations, _save_to_disk, load_from_disk
    )
    
    symbol = "XAUUSD"
    
    # Force load from disk if exists, or start fresh
    load_from_disk()
    
    # 1. Test Drawdown & Strategy Evolution Memory
    # Simulating a decision
    mock_decision = {
        "action": "BUY",
        "confidence": 85,
        "reason": "Testing strategy evolution memory",
        "stop_loss": 2340.0,
        "take_profit": 2360.0
    }
    
    print("-> Recording a mock trade decision...")
    record_decision(symbol, mock_decision, current_price=2345.0)
    
    # 2. Test Regime Memory
    print("-> Recording a mock market regime...")
    record_regime(symbol, "TRANSITIONING")
    
    # 3. Fetch the Memory Prompt
    print("-> Generating Claude Memory Prompt...")
    memory_prompt = get_memory_prompt(symbol)
    
    print("\nSUCCESS: Memory modules executed without errors!")
    print("\n--- MEMORY PROMPT OUTPUT ---")
    # Print the first 500 characters of the prompt to show it's working
    print(memory_prompt[:500] + "...\n[TRUNCATED FOR DISPLAY]")
    
    # Verify the memory structures
    print("\n--- MEMORY STATE INTEGRITY ---")
    print(f"Memory keys present: {list(_memory.keys())}")
    if symbol in _memory and "regime_timeline" in _memory[symbol]:
        print("SUCCESS: Regime timeline tracking is active.")
    if symbol in _memory and "strategy_evolution" in _memory[symbol]:
        print("SUCCESS: Strategy evolution tracking is active.")
        
except Exception as e:
    print(f"FAILED: Memory test raised exception: {e}")

# 4. Test Drawdown and Slippage Tracking in main.py (via imports)
print("\n--- TESTING DRAWDOWN/SLIPPAGE LOGIC SYNTAX ---")
try:
    from trading.risk_manager import check_drawdown
    print("SUCCESS: Drawdown tracking logic is syntactically sound.")
except Exception as e:
    print(f"FAILED: Drawdown logic raised exception: {e}")
