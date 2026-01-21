#!/bin/bash
# scripts/stop_agent.sh
# Kills any running agent_night_shift.py processes

PIDS=$(ps aux | grep "[a]gent_night_shift.py" | awk '{print $2}')

if [ -z "$PIDS" ]; then
    echo "ℹ️ No running agent found."
    exit 0
fi

echo "found PIDS: $PIDS"
echo "Killing..."
kill $PIDS

# Wait and check
sleep 1
if ps -p $PIDS > /dev/null 2>&1; then
    echo "⚠️ Forced killing..."
    kill -9 $PIDS
fi

echo "✅ Agent(s) stopped."
