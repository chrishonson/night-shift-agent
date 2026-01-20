# Night Shift Agent v3.6

An autonomous AI coding assistant that processes tasks, writes code, verifies builds, and creates PRs.

## Project Structure
- `agent_gemini.py` - Main agent script (single-file architecture, ~713 lines)
- `scripts/check_models.py` - Utility to list Gemini models
- `.env.example` - Template for required environment variables

## Architecture

### Classes
| Class | Purpose |
|-------|---------|
| `NightShiftAgent` | Main controller - orchestrates git, tasks, and LLM |
| `LLMClient` | Handles Gemini/Claude CLI calls with automatic failover |
| `Toolbox` | Implements file/shell operations, dispatches tool calls |
| `BuildState` | Tracks build status, checkpoints, enables auto-revert |
| `RateLimiter` | Prevents command spam (3 identical commands in 30s window) |

### Tools (5 available)
- `read_file` - Read file contents
- `write_file` - Write/create files (respects protected files)
- `replace` - Find/replace text in files
- `list_files` - List project files (excludes build dirs, .git, etc.)
- `run_shell` - Execute shell commands

## Development

### Setup
```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Add GH_BOT_TOKEN
```

### Running
```bash
python agent_gemini.py --project-dir /path/to/target
```

### Environment Variables
| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GH_BOT_TOKEN` | Yes | - | GitHub PAT for commits/PRs |
| `BOT_USERNAME` | No | `agentnightshift` | Git commit author |
| `PREFERRED_AGENT_PROVIDER` | No | `gemini` | LLM provider: `gemini` or `claude` |
| `PREFERRED_AGENT_MODEL` | No | `gemini-3-flash-preview` | Model name |

## Key Patterns

- **Single-file agent** - All logic in `agent_gemini.py`
- **LLM Failover** - Auto-switches Gemini ↔ Claude on quota errors
- **Tool dispatch** - Normalizes arg names from various LLM output formats
- **Checkpoint/auto-revert** - Reverts to last good state after 5 consecutive build failures
- **Rate limiting** - Blocks repeated identical commands
- **Prompt logging** - Separate `prompts_*.log` for full LLM conversations

## Protected Files (in target projects)
Do not modify these in target projects:
- `build.gradle.kts`, `settings.gradle.kts`
- `gradle.properties`, `libs.versions.toml`
- `gradle-wrapper.properties`

## Configuration Constants (agent_gemini.py)
| Constant | Value | Description |
|----------|-------|-------------|
| `MAX_ITERATIONS` | 50 | Max tool calls per task |
| `MAX_RETRIES` | 5 | LLM API retry attempts |
| `MAX_CI_FIX_ATTEMPTS` | 5 | CI fix attempts before giving up |
| `MAX_CONTEXT_CHARS` | 30000 | Context window pruning threshold |
| `BRANCH_PREFIX` | `nightshift` | Feature branch prefix |

## Logs
Session logs saved to `.agent_logs/` in the target project directory:
- `session_*.log` - Agent operations, tool calls, build results
- `prompts_*.log` - Complete LLM prompts and responses
