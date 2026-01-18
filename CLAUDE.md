# Night Shift Agent

An autonomous AI coding assistant that processes tasks, writes code, verifies builds, and creates PRs.

## Project Structure
- `agent_gemini.py` - Main agent script (single-file architecture)
- `docs/USAGE.md` - Usage documentation
- `scripts/check_models.py` - Utility to list Gemini models

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

## Key Patterns
- Single-file agent design using Gemini CLI
- Tool-based architecture: `read_file`, `write_file`, `list_files`, `run_shell`
- Checkpoint/auto-revert system for reliability
- Rate limiting to prevent command spam

## Protected Files (in target projects)
Do not modify these in target projects:
- `build.gradle.kts`, `settings.gradle.kts`
- `gradle.properties`, `libs.versions.toml`
- `gradle-wrapper.properties`

## Configuration Constants (agent_gemini.py)
- `MAX_ITERATIONS = 50` - Max tool calls per task
- `MAX_RETRIES = 3` - API retry attempts
- `MAX_CI_FIX_ATTEMPTS = 5` - CI fix attempts
- `BRANCH_PREFIX = "nightshift"` - Feature branch prefix
