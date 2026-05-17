#!/bin/bash
# start.sh — Launch Emy AI Trading System in background (24/7)
# 
# Usage: bash start.sh
#
# This runs FOREVER in the background until you stop it.
# Auto-restarts if the bot crashes.
#
# View live:  screen -r emy
# Detach:     Ctrl+A, then D
# Stop:       bash stop.sh   (or: screen -S emy -X quit)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Kill ALL existing instances hard
pkill -9 -f "python main.py" 2>/dev/null
pkill -9 -f "python.*main\.py" 2>/dev/null
sleep 3

# Check for existing screen session
screen -wipe 2>/dev/null
screen -ls 2>/dev/null | grep -q "emy" && {
    echo "⚠️  Existing 'emy' session found — killing it..."
    screen -S emy -X quit 2>/dev/null
    sleep 2
}

# Create logs directory
mkdir -p logs

# Prevent Mac from sleeping (keeps running with lid closed if on charger)
pkill -f "caffeinate" 2>/dev/null
caffeinate -d -i -s -w $$ &

echo "🚀 Starting Emy AI Trading System..."
echo "   📺 View: screen -r emy"
echo "   🛑 Stop: screen -S emy -X quit"

# Start in detached screen session with AUTO-RESTART loop
screen -dmS emy bash -c "
    cd $SCRIPT_DIR
    source venv/bin/activate
    
    while true; do
        echo '🚀 [$(date)] Starting Emy AI bot...' | tee -a logs/system.log
        python main.py 2>&1 | tee -a logs/system.log
        EXIT_CODE=\$?
        echo '⚠️ [$(date)] Bot exited with code \$EXIT_CODE — restarting in 10s...' | tee -a logs/system.log
        sleep 10
    done
"

sleep 3

# Verify it started
if screen -ls | grep -q "emy"; then
    echo "✅ System is running in 'emy' screen session!"
    echo "   ☕ Mac sleep prevention: ON"
    echo "   🔄 Auto-restart: ON (restarts if crashed)"
    echo ""
    echo "📋 Commands:"
    echo "   screen -r emy        → View live output"
    echo "   Ctrl+A then D        → Detach (keep running)"
    echo "   screen -S emy -X quit → Stop the system"
    echo "   tail -f logs/system.log → Watch logs"
    echo ""
    echo "📱 Telegram: Send /status to your bot"
else
    echo "❌ Failed to start. Check logs/system.log"
fi
