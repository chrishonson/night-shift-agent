---
description: Dump the current agent process status and the latest log entries for context.
---

To get a snapshot of the agent's status and recent logs for a specific project:

1. Run the context script
// turbo
./scripts/context.sh <target_directory>

**Example:**
`./scripts/context.sh /Users/username/Projects/my_app`

This will show:
- Whether the agent process is running.
- The last 20 lines of the most recent log file in the target project's `.agent_logs` directory.
