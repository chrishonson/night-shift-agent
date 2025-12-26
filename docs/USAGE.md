# Night Shift Agent Usage Guide

## Installation

```bash
# Clone the repository
git clone https://github.com/chrishonson/night-shift-agent.git
cd night-shift-agent

# Create Python virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
# Edit .env with your GitHub token
```

## Running the Agent

### Basic Usage

```bash
# Run against a project in a sibling directory
python agent_gemini.py --project-dir ../my-project

# Run against an absolute path
python agent_gemini.py --project-dir /Users/me/Projects/my-app
```

### What the Agent Needs in Your Project

1. **`tasks.txt`** - A file with tasks, one per line
2. **`ARCHITECTURE.md`** (optional) - Describes your project's patterns/conventions
3. **Build command** - The agent runs `./gradlew assembleDebug detekt` by default

### Task File Format

Create a `tasks.txt` in your project root:

```text
# Comments start with #
Add a logout button to the profile screen
Implement unit tests for AuthStore
Fix the email validation regex
```

### Workflow

1. Agent reads `tasks.txt`
2. For each uncompleted task:
   - Reads relevant files
   - Makes code changes
   - Runs build verification
   - Marks task as `[x]` (done) or `[!]` (failed)
3. Creates a PR with all changes
4. Monitors CI and attempts fixes if needed

## Configuration Options

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GH_BOT_TOKEN` | Yes | - | GitHub PAT for pushing and PRs |
| `BOT_USERNAME` | No | `agentnightshift` | Git commit author |
| `AGENT_MODEL` | No | `gemini-3-flash-preview` | Gemini model to use |

### In-Script Configuration

Edit `agent_gemini.py` to customize:

```python
MAX_ITERATIONS = 50          # Max tool calls per task
MAX_RETRIES = 3              # API retry attempts
MAX_CI_FIX_ATTEMPTS = 5      # CI fix attempts
CI_POLL_INTERVAL = 60        # Seconds between CI checks
BRANCH_PREFIX = "nightshift" # Feature branch prefix

# Files the agent cannot modify
PROTECTED_FILES = {
    "build.gradle.kts",
    "settings.gradle.kts",
    # Add more as needed
}
```

## Integrating with Your Project

### Step 1: Create Architecture Documentation

Create `ARCHITECTURE.md` in your project root:

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

### Step 2: Add tasks.txt to .gitignore

```bash
echo "tasks.txt" >> .gitignore
```

### Step 3: Create Your First Task

```bash
echo "Add a greeting message to the home screen" > tasks.txt
```

### Step 4: Run the Agent

```bash
cd /path/to/night-shift-agent
source .venv/bin/activate
python agent_gemini.py --project-dir /path/to/your/project
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `GH_BOT_TOKEN not set` | Add token to `.env` file |
| `No tasks.txt found` | Create `tasks.txt` in project root |
| Agent modifies wrong files | Add files to `PROTECTED_FILES` |
| Build always fails | Check that build command works manually |
| CI status not detected | Verify `gh` CLI is authenticated |

## Best Practices

1. **Small, focused tasks** - "Add a button" beats "Refactor the entire UI"
2. **Good architecture docs** - The more context, the better the output
3. **Review PRs carefully** - The agent is autonomous, not infallible
4. **Use branch protection** - Require CI to pass before merge
