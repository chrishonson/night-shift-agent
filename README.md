# Night Shift Agent v3.6

> **An AI-powered autonomous coding assistant that works while you sleep.**

Night Shift Agent is a Python-based autonomous coding agent powered by **Gemini AI** (with automatic failover to Claude, OpenRouter, and Ollama). It reads a task list, writes code, verifies builds, creates pull requests, and monitors CI—all without human intervention.

## Why Night Shift?

Most AI coding agents run in the cloud or sandboxed environments. **Night Shift Agent runs locally on your machine**—because mobile development demands it.

Mobile apps require platform SDKs (Android SDK, Xcode), emulators, proprietary build systems (Gradle, CocoaPods), and hardware-specific testing that simply can't run in a generic cloud container. This agent is designed to work *with* your local development environment, not around it.

---

## Quick Start

```bash
# 1. Clone the agent
git clone https://github.com/chrishonson/night-shift-agent.git
cd night-shift-agent

# 2. Setup Python environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env with your credentials

# 4. Run against your project
python agent_night_shift.py --project-dir /path/to/your/project
```

---

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│                   Night Shift Agent v3.6                        │
│                                                                 │
│   tasks.txt ──▶ LLM (Gemini/Claude) ──▶ Code Changes ──▶ PR     │
│                        │                                        │
│              ┌─────────┴─────────┐                              │
│              │      7 Tools      │                              │
│              ├───────────────────┤                              │
│              │ read_file         │                              │
│              │ write_file        │                              │
│              │ replace           │                              │
│              │ list_files        │                              │
│              │ run_shell         │                              │
│              │ run_tests         │                              │
│              │ verify_build      │                              │
│              └───────────────────┘                              │
└─────────────────────────────────────────────────────────────────┘
```

The agent operates in a TDD loop:
1. **Read** the next uncompleted task from `tasks.txt`
2. **Understand** the codebase using `list_files` and `read_file`
3. **Write tests first** using `write_file` (TDD red phase)
4. **Run tests** with `run_tests` to confirm they fail
5. **Implement** code to make tests pass (TDD green phase)
6. **Verify** with `verify_build` (build + tests + coverage)
7. **Commit** when verification passes
8. **Repeat** until all tasks are complete
9. **Push** and create Pull Request

### LLM Provider Failover

The agent supports **automatic failover** between LLM providers:

1. **Gemini CLI** (primary) - uses `gemini` command
2. **Claude CLI** - uses `claude --print`
3. **OpenRouter API** - requires `OPENROUTER_API_KEY`
4. **Ollama** - local inference at `localhost:11434`

When any provider hits a rate limit or quota, it automatically switches to the next.

---

## Configuration

Create a `.env` file (copy from `.env.example`):

```bash
# Required
GH_BOT_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx  # GitHub PAT with repo access

# Optional - Agent Configuration
BOT_USERNAME=agentnightshift                    # Git commit author (default: agentnightshift)
PREFERRED_AGENT_MODEL=gemini-2.5-flash-lite     # Gemini model (default: gemini-2.5-flash-lite)

# Optional - Additional Providers
OLLAMA_MODEL=deepseek-r1:32b                    # Ollama model (default: deepseek-r1:32b)
OPENROUTER_API_KEY=sk-or-...                    # OpenRouter API key
OPENROUTER_MODEL=google/gemini-2.0-flash-exp:free  # OpenRouter model
```

### GitHub Token Permissions

Your `GH_BOT_TOKEN` needs these permissions:
- **Contents**: Read and write
- **Pull requests**: Read and write
- **Metadata**: Read-only
- **Actions**: Read-only (for CI monitoring)

---

## Task File Format

Create a `tasks.txt` in your target project:

```text
Add a dark mode toggle to the settings screen
Implement unit tests for UserStore
Fix the login button accessibility label
```

After processing:
```text
[x] Add a dark mode toggle to the settings screen
[x] Implement unit tests for UserStore
[!] Fix the login button accessibility label
```

- `[x]` = Completed successfully
- `[!]` = Failed (agent couldn't complete)
- Lines starting with `#` are ignored (comments)

---

## Protected Files

The agent **cannot modify** these critical build files (configurable in script):

- `build.gradle.kts`
- `settings.gradle.kts`
- `gradle.properties`
- `libs.versions.toml`
- `gradle-wrapper.properties`
- `tasks.txt`

---

## Logs

Session logs are saved to `.agent_logs/` in the target project directory:

```
.agent_logs/
├── session_20251225_144005.log    # Main agent activity log
├── prompts_20251225_144005.log    # Full LLM prompts & responses
└── ...
```

| File | Contents |
|------|----------|
| `session_*.log` | Agent operations, tool calls, build results |
| `prompts_*.log` | Complete LLM conversations (useful for debugging) |

---

## Reliability Features

The agent includes several safeguards to prevent runaway failures:

### Checkpoints & Auto-Revert

After each **successful build**, the agent saves a snapshot of all modified files. If the agent encounters **5 consecutive build failures**, it will:

1. Automatically revert all changed files to the last known-good checkpoint
2. Notify itself to "try a simpler approach"
3. Reset the failure counter and continue

This prevents the agent from spiraling into an infinite loop of failed attempts.

**Log example:**
```
⚠️ Consecutive build failures: 5/5
🔄 AUTO-REVERT: 5 consecutive failures, restoring checkpoint...
   ↩️ Reverted: ChatService.kt
   ↩️ Reverted: App.kt
📸 Reverted 2 files to checkpoint. Agent notified to try simpler approach.
```

### Rate Limiting

To prevent command spam (e.g., retrying the same failing build), the agent enforces rate limits:

- **Same command limit**: Max 3 identical commands within 30 seconds
- When rate-limited, the agent is told to read error logs and try a different approach

### Context Pruning

When conversation context exceeds 30,000 characters, the agent automatically drops the oldest message pairs (preserving system prompt and current task) to stay within limits.

---

## Project Integration

### For Kotlin Multiplatform Projects

See [kmp-agentic-ci-template](https://github.com/chrishonson/kmp-agentic-ci-template) for a complete example including:
- MVI Architecture documentation
- GitHub Actions CI pipeline
- Self-hosted runner setup for UI tests
- Branch protection configuration

### For All Projects

1. **Enable Branch Protection**
   - Since the agent needs write access, you **MUST** protect your `main` branch.
   - Go to Repo Settings → Branches → Add Rule → `main` → Check "Require a pull request before merging".

2. **Create an `ARCHITECTURE.md`** in your project root describing patterns/conventions.
   <details>
   <summary>Click to see ARCHITECTURE.md example</summary>

   ```markdown
   # Project Architecture

   ## Patterns
   - We use MVI (Model-View-Intent) architecture
   - State is immutable
   - ViewModels are called "Stores"

   ## Naming Conventions
   - Screen composables: `[Name]Screen.kt`
   - State classes: `[Name]State`
   - Intent sealed interfaces: `[Name]Intent`

   ## Testing
   - Unit tests go in `src/test/`
   - UI tests go in `src/androidTest/`
   ```
   </details>

3. **Create a `tasks.txt`** with your tasks (and maybe add it to `.gitignore`!).

4. **Run the agent**:

```bash
python agent_night_shift.py --project-dir /path/to/project
```

---

## Self-Hosted Runner Setup

If your project includes UI tests that run on a self-hosted runner (like `connectedAndroidTest`), you'll need to configure your local machine as a GitHub Actions runner.

### First-Time Setup

Follow the complete setup in your project's `docs/RUNNER_SETUP.md`.

### Switching to a New Repository

If you already have a runner configured for a different repo:

```bash
# 1. Stop the current runner service
cd ~/actions-runner
sudo ./svc.sh stop

# 2. Remove the old configuration
./config.sh remove

# 3. Get a new token from GitHub:
#    Go to: GitHub → YOUR_NEW_REPO → Settings → Actions → Runners → New self-hosted runner
#    Copy the registration token shown

# 4. Configure for the new repo
./config.sh --url https://github.com/OWNER/NEW_REPO --token YOUR_NEW_TOKEN

# 5. Restart the service
sudo ./svc.sh start

# 6. Verify it's running
sudo ./svc.sh status
```

### Running Multiple Repos (Organization Runner)

Register a runner at the **organization level** instead:

1. Go to: `github.com/organizations/YOUR_ORG/settings/actions/runners`
2. Add a new runner at org level
3. The runner will be available to all repos in that org

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `GH_BOT_TOKEN not set` | Add token to `.env` file |
| `No tasks.txt found` | Create `tasks.txt` in project root |
| Agent modifies wrong files | Add files to `PROTECTED_FILES` in `agent_night_shift.py` |
| Build always fails | Check that build command works manually |
| CI status not detected | Verify `gh` CLI is authenticated |

---

## Best Practices

1. **Small, focused tasks** — "Add a logout button" works better than "Refactor the entire auth flow"
2. **Context is King** — A detailed `ARCHITECTURE.md` helps the agent mimic your coding style
3. **Review PRs** — The agent is autonomous but not infallible; always review code before merging
4. **Branch Protection** — Use CI guardrails to prevent broken code from hitting main

---

## Why This Exists

This project explores **agentic CI/CD**—the idea that an AI agent can:
1. Receive high-level tasks
2. Autonomously implement them
3. Verify correctness through existing guardrails (builds, tests, linters)
4. Submit changes for human review via pull requests
5. Iterate on feedback by continuing work on rejected PRs

---

| Document | Description |
|----------|-------------|
| [KMP Template](https://github.com/chrishonson/kmp-agentic-ci-template) | Example mobile project |

---

*Built with [Gemini AI](https://ai.google.dev/) and [Claude AI](https://claude.ai/).*
