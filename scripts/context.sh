#!/bin/bash
# scripts/context.sh
# Dumps current agent status for AI context
# Usage: ./scripts/context.sh /path/to/target/project

TARGET_DIR=$1
if [ -z "$TARGET_DIR" ]; then
    echo "Usage: $0 <target_project_dir>"
    exit 1
fi

echo "=== AGENT PROCESS STATUS ==="
ps aux | grep "[a]gent_night_shift.py" || echo "Agent not running."
echo ""

echo "=== LATEST LOGS ==="
LATEST_LOG=$(ls -t "$TARGET_DIR/.agent_logs/session_"*.log 2>/dev/null | head -1)
if [ -n "$LATEST_LOG" ]; then
    echo "Log: $LATEST_LOG"
    echo "--- Last 20 lines ---"
    tail -n 20 "$LATEST_LOG"
else
    echo "No log found."
fi
