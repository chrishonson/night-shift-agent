#!/bin/bash
# Supervisor script: Reset target project and run Night Shift Agent test
# Usage: ./reset_test.sh [project-dir]

set -e

PROJECT_DIR="${1:-/Users/nick/AndroidStudioProjects/kmp-agentic-ci-template}"
LOG_FILE="/tmp/agent_run.log"

echo "=== Night Shift Agent Test Runner ==="
echo "Target: $PROJECT_DIR"
echo ""

# Kill any running agent
echo "Stopping any running agents..."
pkill -f "agent_night_shift.py" 2>/dev/null || true
pkill -f "claude --print" 2>/dev/null || true
sleep 1

# Reset target project
echo "Resetting target project to origin/main..."
cd "$PROJECT_DIR"
git fetch origin
git checkout main
git reset --hard origin/main
git clean -fd

echo ""
echo "tasks.txt contents:"
cat tasks.txt 2>/dev/null || echo "(no tasks.txt found)"
echo ""

# Return to agent directory and run
cd "$(dirname "$0")"
echo "Starting agent..."
python agent_night_shift.py --project-dir "$PROJECT_DIR" > "$LOG_FILE" 2>&1 &
AGENT_PID=$!
echo "Agent started with PID $AGENT_PID"
echo "Log file: $LOG_FILE"
echo ""
echo "Tailing log (Ctrl+C to stop watching, agent continues in background)..."
echo "============================================================"
sleep 2
tail -f "$LOG_FILE"
