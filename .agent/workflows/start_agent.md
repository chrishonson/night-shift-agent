---
description: Start the Night Shift Agent in the background for a specific target project.
---

To start the agent, run the following command, replacing `<target_directory>` with the absolute path to your target project:

1. Run the start script
// turbo
./scripts/start_agent.sh <target_directory>

**Example:**
`./scripts/start_agent.sh /Users/username/Projects/my_app`

The script will Output:
- The PID of the new agent process.
- The path to the log file being generated.
