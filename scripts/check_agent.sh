#!/bin/bash
# Check if Night Shift Agent is running

# Find PID ignoring grep itself
PID=$(ps aux | grep "[a]gent_night_shift.py" | awk '{print $2}' | head -n 1)

if [ -n "$PID" ]; then
  echo "✅ Agent is RUNNING (PID: $PID)"
  # Try to extract project dir
  ARGS=$(ps -p $PID -o args=)
  if [[ $ARGS == *"--project-dir"* ]]; then
      PROJ=$(echo $ARGS | sed -n 's/.*--project-dir \([^ ]*\).*/\1/p')
      echo "   📂 Target: $PROJ"
  fi
  exit 0
else
  echo "❌ Agent is NOT running"
  exit 1
fi
