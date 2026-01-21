#!/bin/bash
# script/start_agent.sh
# Usage: ./scripts/start_agent.sh /path/to/target/project

TARGET_DIR=$1

if [ -z "$TARGET_DIR" ]; then
    echo "Usage: $0 <target_project_dir>"
    exit 1
fi

# Ensure we are in the night-shift-agent root
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
AGENT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$AGENT_ROOT"

# Activate venv
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    echo "Error: .venv not found in $AGENT_ROOT"
    exit 1
fi

LOG_FILE="/tmp/agent_run_$(date +%Y%m%d_%H%M%S).log"

echo "🚀 Starting Night Shift Agent..."
echo "Target: $TARGET_DIR"
echo "Logs: $LOG_FILE"

nohup python agent_night_shift.py --project-dir "$TARGET_DIR" > "$LOG_FILE" 2>&1 &
PID=$!

echo "✅ Agent started with PID: $PID"
echo "To monitor: tail -f $LOG_FILE"
