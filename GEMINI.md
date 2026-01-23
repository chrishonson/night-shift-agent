# GEMINI.md

This file provides guidance to Gemini CLI when working with code in this repository.

## What This Is

Night Shift Agent is a single-file autonomous coding agent (~1300 lines in `agent_night_shift.py`) that reads tasks from a `tasks.txt` file in a target project, writes code using LLM-driven tool calls, verifies builds, commits changes, creates PRs, and monitors CI.

## Commands

```bash
# Setup
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Add GH_BOT_TOKEN

# Run agent against a target project
python agent_night_shift.py --project-dir /path/to/target

# Start/stop agent in background
./scripts/start_agent.sh /path/to/target
./scripts/stop_agent.sh

# Reset target project and run fresh test
./reset_test.sh                    # Uses default target
./reset_test.sh /path/to/project   # Custom target

# List available Gemini models (requires GOOGLE_API_KEY)
python scripts/check_models.py
```

## Architecture

The agent is intentionally single-file. All classes live in `agent_night_shift.py`:

- `NightShiftAgent` - Main controller: git ops, task loop, PR creation, CI monitoring
- `ProviderManager` - LLM failover chain: Gemini → Claude → OpenRouter → Ollama
- `LLMProvider` - Abstract base class for all providers
- `GeminiCLIProvider` - Gemini CLI integration (uses `gemini` command)
- `ClaudeCLIProvider` - Claude CLI integration (uses `claude --print`)
- `OllamaProvider` - Local Ollama integration
- `OpenRouterAPIProvider` - OpenRouter API integration
- `Toolbox` - Dispatches tool calls to file/shell operations
- `BuildState` - Tracks build pass/fail, file checkpoints, auto-revert
- `RateLimiter` - Blocks repeated identical commands (3 in 30s window)

## Tool Output Format

The agent expects LLM responses to contain tool calls as plain text JSON wrapped in `<agent_action>` tags:

```
<agent_action>{"action": "read_file", "args": {"path": "src/Main.kt"}}</agent_action>
```

Available tools:
- `read_file` - Read file contents
- `write_file` - Write/create files
- `replace` - Find/replace text in files
- `list_files` - List project files
- `run_shell` - Execute shell commands (exploration only)
- `run_tests` - Run unit tests (TDD red/green phases)
- `verify_build` - Full verification build (required for task completion)

## Provider Failover

Providers are tried in order. On quota/rate limit errors, the agent switches to the next:

1. Gemini CLI (`gemini` command)
2. Claude CLI (`claude --print`)
3. OpenRouter API (requires `OPENROUTER_API_KEY`)
4. Ollama (local, `http://localhost:11434`)

## Environment Variables

| Variable | Required | Default |
|----------|----------|---------|
| `GH_BOT_TOKEN` | Yes | - |
| `BOT_USERNAME` | No | `agentnightshift` |
| `PREFERRED_AGENT_MODEL` | No | `gemini-2.5-flash-lite` |
| `OLLAMA_MODEL` | No | `deepseek-r1:32b` |
| `OPENROUTER_API_KEY` | No | - |
| `OPENROUTER_MODEL` | No | `google/gemini-2.0-flash-exp:free` |

## Key Constants

| Constant | Value |
|----------|-------|
| `MAX_ITERATIONS` | 80 |
| `MAX_RETRIES` | 2 |
| `MAX_CI_FIX_ATTEMPTS` | 5 |
| `MAX_CONTEXT_CHARS` | 30000 |
| `MAX_TOOL_OUTPUT_CHARS` | 50000 |
| `BRANCH_PREFIX` | `nightshift` |

## Protected Files

The agent refuses to modify these files in target projects:
- `build.gradle.kts`, `settings.gradle.kts`, `gradle.properties`
- `libs.versions.toml`, `gradle-wrapper.properties`, `tasks.txt`

## Logs

Saved to `.agent_logs/` in the target project:
- `session_*.log` - Agent operations, tool calls, build results
- `prompts_*.log` - Complete LLM prompts and responses

## Key Behaviors

- **TDD Enforcement**: System prompt requires test-first development
- **Verification Required**: Tasks only complete when `verify_build` passes
- **Auto-Revert**: After 5 consecutive build failures, reverts files to last checkpoint
- **Context Pruning**: Drops oldest message pairs when context exceeds 30k chars
- **CI Monitoring**: After PR creation, polls GitHub Actions and attempts auto-fix on failures
- **Subprocess Safety**: All subprocess calls use `stdin=subprocess.DEVNULL`
