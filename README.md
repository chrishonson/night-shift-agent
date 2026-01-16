# 🌙 Night Shift Agent

> **An AI-powered autonomous coding assistant that works while you sleep.**

Night Shift Agent is a Python-based autonomous coding agent powered by **Gemini AI**. It reads a task list, writes code, verifies builds, creates pull requests, and monitors CI—all without human intervention.

## Why Night Shift?

Most AI coding agents run in the cloud or sandboxed environments. **Night Shift Agent runs locally on your machine**—because mobile development demands it.

Mobile apps require platform SDKs (Android SDK, Xcode), emulators, proprietary build systems (Gradle, CocoaPods), and hardware-specific testing that simply can't run in a generic cloud container. This agent is designed to work *with* your local development environment, not around it.

---

## ⚡ Quick Start

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
python agent_gemini.py --project-dir /path/to/your/project
```

---

## 🤖 How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│                     🌙 Night Shift Agent                        │
│                                                                 │
│   tasks.txt ──▶ Gemini AI ──▶ Code Changes ──▶ PR + CI Monitor  │
│                     │                                           │
│              ┌──────┴──────┐                                    │
│              │   4 Tools   │                                    │
│              ├─────────────┤                                    │
│              │ read_file   │                                    │
│              │ write_file  │                                    │
│              │ list_files  │                                    │
│              │ run_shell   │                                    │
│              └─────────────┘                                    │
└─────────────────────────────────────────────────────────────────┘
```

The agent operates in a loop:
1. **Check** for existing open PRs from previous runs
2. **Read** the next task from `tasks.txt`
3. **Understand** the codebase using `list_files` and `read_file`
4. **Write** code changes with `write_file`
5. **Verify** the build with your project's build command
6. **Commit** when verification passes
7. **Repeat** until all tasks are complete
8. **Push** and create/update Pull Request
9. **Monitor** CI, auto-fixing failures when possible

### PR Continuity

If you stop the agent and restart it later, it will:
- **Find your existing open PR** from a previous run
- **Check out that branch** instead of creating a new one
- **Continue working** on the same PR
- **Read any feedback comments** you left on the PR

---

## 📋 Configuration

Create a `.env` file (copy from `.env.example`):

```bash
# Required
GH_BOT_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx  # GitHub PAT with repo access

# Optional
BOT_USERNAME=agentnightshift           # Git commit author (default: agentnightshift)
AGENT_MODEL=gemini-3-flash-preview     # Gemini model (default: gemini-3-flash-preview)
```

### GitHub Token Permissions

Your `GH_BOT_TOKEN` needs these permissions:
- **Contents**: Read and write
- **Pull requests**: Read and write
- **Metadata**: Read-only
- **Actions**: Read-only (for CI monitoring)

---

## 📁 Task File Format

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

## 🛡️ Protected Files

The agent **cannot modify** these critical build files (configurable in script):

- `build.gradle.kts`
- `settings.gradle.kts`
- `gradle.properties`
- `libs.versions.toml`
- `gradle-wrapper.properties`

---

## 📊 Logs

Session logs are saved to `.agent_logs/` in the project directory:

```
.agent_logs/
├── session_20251225_144005.log
├── session_20251225_153012.log
└── ...
```

---

## �️ Reliability Features

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

To prevent command spam (e.g., retrying the same failing build 40+ times), the agent enforces rate limits:

- **Same command limit**: Max 3 identical commands within 30 seconds
- When rate-limited, the agent is told to read error logs and try a different approach

### Enhanced Diff Logging

Every file write now logs a compact diff showing what changed:

```
✍️ Wrote file: ChatService.kt (1898 bytes)
   📝 Change: +15 lines, was 1447B -> now 1898B
   +import some.new.package
   -old_function()
   +new_function()
```

This makes it easy to identify exactly what change broke a build when reviewing logs.

---

## �🔧 Project Integration

### For Kotlin Multiplatform Projects

See [kmp-agentic-ci-template](https://github.com/chrishonson/kmp-agentic-ci-template) for a complete example including:
- MVI Architecture documentation
- GitHub Actions CI pipeline
- Self-hosted runner setup for UI tests
- Branch protection configuration

### For All Projects

1. Join the **Branch Protection** club!
   - Since the agent needs write access, you **MUST** protect your `main` branch.
   - Go to Repo Settings -> Branches -> Add Rule -> `main` -> Check "Require a pull request before merging".
2. Create an `ARCHITECTURE.md` in your project root describing patterns/conventions.
3. Create a `tasks.txt` with your tasks.
4. Copy your `.env` file into the project root (so the agent can read credentials).
5. Run the agent:

```bash
# Start fresh
python agent_gemini.py --project-dir /path/to/project

# Continue on the current branch (useful specifically when adding more tasks to an existing PR)
python agent_gemini.py --project-dir /path/to/project --continue-on-branch
```

---

## 🏃 Self-Hosted Runner Setup

If your project includes UI tests that run on a self-hosted runner (like `connectedAndroidTest`), you'll need to configure your local machine as a GitHub Actions runner for each new repository.

### First-Time Setup

If you've never set up a runner, follow the complete setup in your project's `docs/RUNNER_SETUP.md`.

### Switching to a New Repository

If you already have a runner configured for a different repo and need to switch:

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

Instead of switching per-repo, you can register a runner at the **organization level**:

1. Go to: `github.com/organizations/YOUR_ORG/settings/actions/runners`
2. Add a new runner at org level
3. The runner will be available to all repos in that org

### Troubleshooting

| Issue | Solution |
|-------|----------|
| Job stuck in "Queued" | Runner not running or configured for different repo |
| "Runner offline" in GitHub | Run `sudo ./svc.sh start` in `~/actions-runner` |
| Permission denied | Ensure runner user has access to Android SDK / emulator |

---

## 📚 Documentation

| Document | Description |
|----------|-------------|
| [Usage Guide](./docs/USAGE.md) | Detailed usage instructions |
| [KMP Template](https://github.com/chrishonson/kmp-agentic-ci-template) | Example mobile project |

---

## 🧪 Why This Exists

This project explores **agentic CI/CD**—the idea that an AI agent can:
1. Receive high-level tasks
2. Autonomously implement them
3. Verify correctness through existing guardrails (builds, tests, linters)
4. Submit changes for human review via pull requests
5. Iterate on feedback by continuing work on rejected PRs

---

*Built with [Gemini AI](https://ai.google.dev/).*
