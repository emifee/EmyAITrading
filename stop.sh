#!/bin/bash
# stop.sh — Stop the Emy AI Trading System
echo "🛑 Stopping Emy AI Trading System..."
screen -S emy -X quit 2>/dev/null
pkill -f "python main.py" 2>/dev/null
pkill -f "caffeinate" 2>/dev/null
sleep 2
echo "✅ System stopped."
