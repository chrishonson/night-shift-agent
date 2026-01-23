# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

| Class | Purpose |
|-------|---------|
| `NightShiftAgent` | Main controller: git ops, task loop, PR creation, CI monitoring |
| `ProviderManager` | LLM failover chain: Gemini → Claude → OpenRouter → Ollama |
| `LLMProvider` (ABC) | Base class for `GeminiCLIProvider`, `ClaudeCLIProvider`, `OllamaProvider`, `OpenRouterAPIProvider` |
| `Toolbox` | Dispatches tool calls (`read_file`, `write_file`, `replace`, `list_files`, `run_shell`, `run_tests`, `verify_build`) |
| `BuildState` | Tracks build pass/fail, file checkpoints, auto-revert after 5 consecutive failures |
| `RateLimiter` | Blocks repeated identical commands (3 in 30s window) |

### Tool Output Format

The agent expects LLM responses to contain tool calls as plain text JSON wrapped in `<agent_action>` tags:
```
<agent_action>{"action": "read_file", "args": {"path": "src/Main.kt"}}</agent_action>
```

### Provider Failover

Providers are tried in order. On `QuotaExceededError`, the agent switches to the next provider and resets iteration count:
1. Gemini CLI (`gemini` command)
2. Claude CLI (`claude --print`)
3. OpenRouter API (requires `OPENROUTER_API_KEY`)
4. Ollama (local, `http://localhost:11434`)

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GH_BOT_TOKEN` | Yes | - | GitHub PAT for commits/PRs |
| `BOT_USERNAME` | No | `agentnightshift` | Git commit author |
| `PREFERRED_AGENT_MODEL` | No | `gemini-2.5-flash-lite` | Gemini model |
| `OLLAMA_MODEL` | No | `deepseek-r1:32b` | Ollama model |
| `OPENROUTER_API_KEY` | No | - | OpenRouter API key |
| `OPENROUTER_MODEL` | No | `google/gemini-2.0-flash-exp:free` | OpenRouter model |

## Key Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `MAX_ITERATIONS` | 80 | Max tool calls per task |
| `MAX_RETRIES` | 2 | LLM API retry attempts |
| `MAX_CI_FIX_ATTEMPTS` | 5 | CI fix attempts before giving up |
| `MAX_CONTEXT_CHARS` | 30000 | Context window pruning threshold |
| `MAX_TOOL_OUTPUT_CHARS` | 50000 | Truncation limit for tool output |
| `BRANCH_PREFIX` | `nightshift` | Feature branch prefix |

## Protected Files

The agent refuses to modify these files in target projects:
- `build.gradle.kts`, `settings.gradle.kts`, `gradle.properties`
- `libs.versions.toml`, `gradle-wrapper.properties`, `tasks.txt`

## Logs

Saved to `.agent_logs/` in the target project:
- `session_*.log` - Agent operations, tool calls, build results
- `prompts_*.log` - Complete LLM prompts and responses

## Key Behaviors

- **TDD Enforcement**: System prompt requires test-first development with `run_tests` for red/green phases
- **Verification Required**: Tasks only complete when `verify_build` passes (runs assembleDebug, iOS build, detekt, tests, koverVerify)
- **Auto-Revert**: After 5 consecutive build failures, reverts files to last checkpoint
- **Context Pruning**: Drops oldest message pairs when context exceeds `MAX_CONTEXT_CHARS`
- **CI Monitoring**: After PR creation, polls GitHub Actions and attempts auto-fix on failures
- **Subprocess Safety**: All subprocess calls use `stdin=subprocess.DEVNULL` to prevent hanging
