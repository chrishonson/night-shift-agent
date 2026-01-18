#!/usr/bin/env python3
"""
🌙 Night Shift Agent v3.6 - Autonomous Coding Assistant
========================================================
Features:
- Direct push to origin (enables UI tests on PRs)
- Build verification before task completion
- CI monitoring with automatic fixes
- Protected files list to prevent corruption
- Project directory argument for running against any repo
"""

import os
import subprocess
import sys
import re
import time
import json
import logging
import argparse
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# Parse arguments first to get project directory
parser = argparse.ArgumentParser(description='🌙 Night Shift Agent - Autonomous Coding Assistant')
parser.add_argument('--project-dir', '-p', type=str, default='.', 
                    help='Path to the project directory (default: current directory)')
parser.add_argument('--continue-on-branch', '-c', action='store_true',
                    help='Continue using the current git branch instead of creating a new one')
args = parser.parse_args()

# Change to project directory
PROJECT_DIR = Path(args.project_dir).resolve()
if not PROJECT_DIR.exists():
    print(f"❌ Project directory not found: {PROJECT_DIR}")
    sys.exit(1)
os.chdir(PROJECT_DIR)

# Load .env from project directory if it exists, then from agent directory
load_dotenv(PROJECT_DIR / '.env')
load_dotenv()  # Also load from agent's directory as fallback

# =============================================================================
# CONFIGURATION
# =============================================================================

AGENT_PROVIDER = os.getenv("PREFERRED_AGENT_PROVIDER", "claude")  # "gemini" or "claude"
AGENT_MODEL = os.getenv("PREFERRED_AGENT_MODEL", "sonnet")  # gemini model or claude alias (sonnet, opus)
GH_BOT_TOKEN = os.getenv("GH_BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "agentnightshift")

MAX_ITERATIONS = 50
MAX_RETRIES = 5
MAX_CI_FIX_ATTEMPTS = 5
RETRY_BASE_DELAY = 5
CI_POLL_INTERVAL = 60
MAX_FILES_IN_CONTEXT = 50
REQUIRE_BUILD_VERIFICATION = True
MAX_CONTEXT_CHARS = 30000  # Conservative limit for CLI/shell reliability

BRANCH_PREFIX = "nightshift"

PROTECTED_FILES = {"build.gradle.kts", "settings.gradle.kts", "gradle.properties", "libs.versions.toml", "gradle-wrapper.properties"}

LOG_DIR = Path(".agent_logs")
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.DEBUG,  # Changed to DEBUG for troubleshooting
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("NightShiftAgent")

# Client removed - using CLI

class BuildState:
    def __init__(self):
        self.build_attempted = False
        self.build_passed = False
        self.last_error = None
        self.last_successful_files = {}  # path -> content snapshot after last good build
        self.files_changed_since_success = []  # track what changed since last success
    def reset(self): 
        self.build_attempted = False
        self.build_passed = False
        self.last_error = None
        # Keep snapshots across task boundaries for debugging
    def is_verified(self) -> bool:
        return self.build_attempted and self.build_passed if REQUIRE_BUILD_VERIFICATION else True
    def checkpoint(self, files_written: list):
        """Snapshot file contents after successful build."""
        for path in files_written:
            try:
                with open(path, 'r') as f:
                    self.last_successful_files[path] = f.read()
            except: pass
        self.files_changed_since_success = []
        logger.info(f"📸 Checkpoint saved: {len(self.last_successful_files)} files in known-good state")

build_state = BuildState()

# Rate limiting for command spam prevention
class CommandRateLimiter:
    def __init__(self, window_seconds=30, max_identical=3):
        self.recent_commands = []  # (timestamp, command)
        self.window = window_seconds
        self.max_identical = max_identical
    
    def should_allow(self, command: str) -> tuple[bool, str]:
        current_time = time.time()
        # Clean old entries
        self.recent_commands = [(t, c) for t, c in self.recent_commands if current_time - t < self.window]
        # Count identical commands in window
        identical_count = sum(1 for _, c in self.recent_commands if c == command)
        if identical_count >= self.max_identical:
            return False, f"Rate limited: same command executed {identical_count} times in {self.window}s"
        self.recent_commands.append((current_time, command))
        return True, ""

rate_limiter = CommandRateLimiter()

# =============================================================================
# TOOLS
# =============================================================================

def read_file(path: str = None, file_path: str = None) -> str:
    target_path = path or file_path
    if not target_path: return "Error: No path provided"
    try:
        with open(target_path, "r") as f: content = f.read()
        logger.info(f"📖 Read file: {target_path} ({len(content)} bytes)")
        return content
    except Exception as e:
        logger.error(f"❌ Failed to read {target_path}: {e}")
        return f"Error reading file {target_path}: {e}"

def write_file(path: str = None, content: str = None, file_path: str = None, code: str = None, file_content: str = None) -> str:
    target_path = path or file_path
    target_content = content or code or file_content or ""
    
    if not target_path: return "Error: No path provided"
    
    filename = os.path.basename(target_path)
    if filename in PROTECTED_FILES:
        logger.warning(f"🛡️ BLOCKED: {target_path}")
        return f"ERROR: {filename} is protected. Fix source code instead."
    try:
        # Read existing content for diff logging
        old_content = ""
        if os.path.exists(target_path):
            try:
                with open(target_path, "r") as f:
                    old_content = f.read()
            except: pass
        
        os.makedirs(os.path.dirname(target_path) if os.path.dirname(target_path) else ".", exist_ok=True)
        with open(target_path, "w") as f: f.write(target_content)
        logger.info(f"✍️ Wrote file: {target_path} ({len(target_content)} bytes)")
        
        # Log what changed (compact diff summary)
        if old_content:
            old_lines = old_content.splitlines()
            new_lines = target_content.splitlines()
            added = len(new_lines) - len(old_lines)
            change_desc = f"+{added} lines" if added > 0 else f"{added} lines" if added < 0 else "same line count"
            logger.info(f"   📝 Change: {change_desc}, was {len(old_content)}B -> now {len(target_content)}B")
            # Log first few changed lines for debugging
            import difflib
            diff = list(difflib.unified_diff(old_lines[:50], new_lines[:50], lineterm='', n=0))
            if diff:
                # Only log first 5 diff lines to keep logs manageable
                for line in diff[2:7]:  # Skip the --- +++ headers
                    logger.info(f"   {line[:100]}{'...' if len(line) > 100 else ''}")
                if len(diff) > 7:
                    logger.info(f"   ... and {len(diff) - 7} more diff lines")
        else:
            logger.info(f"   📝 New file created")
        
        # Track what we've changed since last successful build
        build_state.files_changed_since_success.append(target_path)
        build_state.build_passed = False
        build_state.build_attempted = False
        return f"Successfully wrote to {target_path}"
    except Exception as e:
        logger.error(f"❌ Failed to write {target_path}: {e}")
        return f"Error writing to file {target_path}: {e}"

def list_files(path: str = ".") -> str:
    files = []
    ignore = {".git", ".gradle", ".idea", ".venv", "__pycache__", "build", ".kotlin", "node_modules"}
    logger.info(f"📂 Listing files in: {path}")
    for root, dirs, filenames in os.walk(path):
        dirs[:] = [d for d in dirs if d not in ignore and not d.startswith(".")]
        for f in filenames:
            if not f.startswith(".") and not any(f.endswith(e) for e in [".jar", ".class", ".pyc"]):
                files.append(os.path.join(root, f))
                if len(files) >= MAX_FILES_IN_CONTEXT: return "\n".join(files + ["...(truncated)"])
    return "\n".join(files)

def run_shell(command: str) -> str:
    global build_state
    
    # Rate limiting check
    allowed, reason = rate_limiter.should_allow(command)
    if not allowed:
        logger.warning(f"🚫 {reason}")
        return f"Error: {reason}. Try a different approach or read error logs first."
    
    logger.info(f"🤖 Executing: {command}")
    try:
        env = os.environ.copy()
        if GH_BOT_TOKEN and command.strip().startswith("gh "):
            env["GITHUB_TOKEN"] = GH_BOT_TOKEN
            env["GH_TOKEN"] = GH_BOT_TOKEN
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=600, env=env)
        output = result.stdout + result.stderr
        # Only treat as build command if it's actually running gradlew/gradle
        is_build_command = command.strip().startswith("./gradlew") or command.strip().startswith("gradlew") or command.strip().startswith("gradle ")
        
        if is_build_command:
            build_state.build_attempted = True
            build_state.build_passed = result.returncode == 0
            if result.returncode == 0:
                logger.info("✅ Command succeeded")
                # Checkpoint: save state of files changed since last success
                if build_state.files_changed_since_success:
                    logger.info(f"📸 Build passed after changing: {build_state.files_changed_since_success}")
                    build_state.checkpoint(build_state.files_changed_since_success)
            else:
                logger.warning(f"⚠️ Command failed (exit {result.returncode})")
                # Log what we changed that might have broken it
                if build_state.files_changed_since_success:
                    logger.warning(f"   ⚠️ Files changed since last success: {build_state.files_changed_since_success}")
                    # Log what we could revert to
                    revertable = [p for p in build_state.files_changed_since_success if p in build_state.last_successful_files]
                    if revertable:
                        logger.info(f"   💡 Could revert to checkpoint: {revertable}")
        
        if result.returncode != 0:
            return f"Command failed (exit {result.returncode}):\n{output}"
        return output
    except subprocess.TimeoutExpired: return "Error: Command timed out"
    except Exception as e: return f"Error: {e}"

# Tools list removed - handled by system prompt

def extract_json_objects(text: str) -> list:
    """Extract JSON objects, prioritizing <agent_action> tags."""
    objects = []
    
    # Strategy 1: Look for <agent_action> tags (Best for Gemini CLI)
    matches = re.findall(r'<agent_action>(.*?)</agent_action>', text, re.DOTALL)
    for match in matches:
        try:
            # Clean up potential markdown code blocks inside tags
            clean_json = strip_markdown_code_blocks(match.strip())
            return [clean_json] # Return immediately if found
        except:
            pass
            
    # Strategy 2: Fallback to finding raw JSON objects (Legacy/Claude)
    i = 0
    while i < len(text):
        if text[i] == '{':
            depth = 0
            start = i
            in_string = False
            escape_next = False
            while i < len(text):
                char = text[i]
                if escape_next:
                    escape_next = False
                elif char == '\\' and in_string:
                    escape_next = True
                elif char == '"' and not escape_next:
                    in_string = not in_string
                elif not in_string:
                    if char == '{':
                        depth += 1
                    elif char == '}':
                        depth -= 1
                        if depth == 0:
                            json_candidate = text[start:i+1]
                            # Validate it's actually valid JSON
                            try:
                                json.loads(json_candidate)
                                objects.append(json_candidate)
                            except json.JSONDecodeError:
                                logger.debug(f"   🔍 Invalid JSON candidate (len={len(json_candidate)}): {json_candidate[:100]}...")
                            break
                i += 1
        i += 1
    if objects:
        logger.debug(f"   🔍 Extracted {len(objects)} JSON object(s) from response")
    return objects

def search_file_content(pattern, dir_path=".", include=None):
    cmd = f"grep -r '{pattern}' {dir_path}"
    if include:
        cmd += f" --include='{include}'"
    return run_shell(cmd)

def replace_in_file(path: str = None, file_path: str = None, old_string: str = None, new_string: str = None, instruction: str = None, **kwargs) -> str:
    """Replace text in a file (Gemini CLI's 'replace' tool)."""
    target_path = path or file_path
    if not target_path:
        return "Error: No file_path (or path) provided"
    if not old_string or new_string is None:
        return "Error: old_string and new_string are required"
    
    try:
        with open(target_path, "r") as f:
            content = f.read()
            
        if new_string in content:
            return f"Success: Text already present in {target_path}"
        
        if old_string not in content:
            return f"Error: Could not find the specified text in {target_path}"
        
        new_content = content.replace(old_string, new_string, 1)
        with open(target_path, "w") as f:
            f.write(new_content)
        
        logger.info(f"✏️ Replaced text in: {target_path}")
        build_state.files_changed_since_success.append(target_path)
        build_state.build_passed = False
        build_state.build_attempted = False
        return f"Successfully replaced text in {target_path}"
    except Exception as e:
        return f"Error replacing in {target_path}: {e}"

available_functions = {
    "read_file": read_file, 
    "write_file": write_file, 
    "list_files": list_files, 
    "run_shell": run_shell,
    "run_shell_command": run_shell,
    "search_file_content": search_file_content,
    "replace": replace_in_file
}

def strip_markdown_code_blocks(text: str) -> str:
    """Strip markdown code block wrappers from text."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split('\n')
        # Remove opening ``` or ```json
        if lines[0].startswith("```"):
            lines = lines[1:]
        # Remove closing ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return '\n'.join(lines).strip()
    return text

def call_gemini_cli(prompt: str):
    """Call Gemini CLI"""
    logger.info("🤖 Calling Gemini CLI...")
    for attempt in range(MAX_RETRIES):
        try:
            # Let Gemini CLI execute in text mode (no --output-format json)
            # This prevents it from intercepting tool calls
            result = subprocess.run(
                ["gemini", "--model", AGENT_MODEL],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=300
            )
            
            # Debug: log raw output
            logger.debug(f"   📤 Gemini stdout length: {len(result.stdout)}")
            logger.debug(f"   📤 Gemini stderr: {result.stderr[:200] if result.stderr else 'none'}")
            
            if result.returncode != 0:
                logger.warning(f"⚠️ CLI Error (Attempt {attempt+1}/{MAX_RETRIES}): {result.stderr}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                return None

            # Raw text output logic (no JSON wrapper)
            response_text = result.stdout.strip()
            # Strip markdown code blocks if present
            response_text = strip_markdown_code_blocks(response_text)
            logger.debug(f"   📥 Gemini response length: {len(response_text)}, starts with: {response_text[:100]}...")
            return response_text


        except Exception as e:
            logger.warning(f"⚠️ CLI Exception (Attempt {attempt+1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
            else:
                return None
    return None

def call_claude_cli(prompt: str):
    """Call Claude Code CLI in print mode (text only), relying on prompt for structure."""
    logger.info(f"🤖 Calling Claude CLI... (Prompt length: {len(prompt)} chars)")
    for attempt in range(MAX_RETRIES):
        try:
            # Write prompt to temp file
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
                f.write(prompt)
                temp_prompt_path = f.name
            
            try:
                # No --json-schema or --output-format json -> Raw text output
                model_arg = f"--model {AGENT_MODEL}" if AGENT_MODEL else ""
                cmd = f"cat {temp_prompt_path} | claude -p {model_arg} --dangerously-skip-permissions"
                logger.debug(f"   📤 Claude command: {cmd}")
                
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=300
                )
                
                if result.returncode != 0:
                    logger.warning(f"⚠️ CLI Error (Attempt {attempt+1}/{MAX_RETRIES}): {result.stderr}")
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                        continue
                    return None

                response_text = result.stdout.strip()
                # Strip markdown code blocks if present (helper handles it)
                response_text = strip_markdown_code_blocks(response_text)
                
                logger.debug(f"   📥 Claude response length: {len(response_text)}, starts with: {response_text[:100]}...")
                return response_text
                
            finally:
                if os.path.exists(temp_prompt_path):
                    os.unlink(temp_prompt_path)

        except Exception as e:
            logger.warning(f"⚠️ CLI Exception (Attempt {attempt+1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
            else:
                return None
    return None

def call_llm_cli(prompt: str):
    """Route to the appropriate CLI based on AGENT_PROVIDER"""
    if AGENT_PROVIDER == "claude":
        return call_claude_cli(prompt)
    else:
        return call_gemini_cli(prompt)


# =============================================================================
# GIT HELPERS
# =============================================================================

def run_cmd(command: str, timeout: int = 120) -> tuple[bool, str]:
    try:
        env = os.environ.copy()
        if GH_BOT_TOKEN and command.strip().startswith("gh "):
            env["GITHUB_TOKEN"] = GH_BOT_TOKEN
            env["GH_TOKEN"] = GH_BOT_TOKEN
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout, env=env)
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except Exception as e: return False, str(e)

def get_repo_info() -> dict:
    success, output = run_cmd("gh repo view --json nameWithOwner,url")
    try: return json.loads(output) if success else {}
    except: return {}

def setup_origin_with_bot_token() -> bool:
    logger.info("🔧 Configuring git authentication...")
    repo_info = get_repo_info()
    repo_name = repo_info.get("nameWithOwner", "")
    if not repo_name:
        logger.error("❌ Could not determine repository")
        return False
    logger.info(f"📦 Repository: {repo_name}")
    if GH_BOT_TOKEN:
        run_cmd(f'git config user.name "{BOT_USERNAME}"')
        run_cmd(f'git config user.email "{BOT_USERNAME}@users.noreply.github.com"')
        origin_url = f"https://{BOT_USERNAME}:{GH_BOT_TOKEN}@github.com/{repo_name}.git"
        run_cmd(f'git remote set-url origin "{origin_url}"')
        logger.info("✅ Git configured with bot credentials")
    return True

def find_existing_open_pr() -> tuple[str, str]:
    """Check if there's an existing open PR from a nightshift branch. Returns (branch_name, pr_url) or empty strings."""
    repo_info = get_repo_info()
    repo_name = repo_info.get("nameWithOwner", "")
    success, output = run_cmd(f'gh pr list --state open --repo {repo_name} --author {BOT_USERNAME} --json headRefName,url --jq ".[] | select(.headRefName | startswith(\\"{BRANCH_PREFIX}/\\"))"')
    if success and output.strip():
        try:
            # Parse the first matching PR
            lines = output.strip().split('\n')
            if lines:
                pr_data = json.loads(lines[0])
                return pr_data.get("headRefName", ""), pr_data.get("url", "")
        except: pass
    return "", ""

def create_feature_branch() -> str:
    # Check if we should continue on current branch
    if args.continue_on_branch:
        success, output = run_cmd("git rev-parse --abbrev-ref HEAD")
        if success:
            branch_name = output.strip()
            logger.info(f"🌿 Continuing on current branch: {branch_name}")
            return branch_name
        else:
            logger.error("❌ Failed to get current branch name")
            sys.exit(1)

    # Check for existing open PR first
    existing_branch, existing_pr = find_existing_open_pr()
    if existing_branch:
        logger.info(f"🔄 Found existing open PR: {existing_pr}")
        logger.info(f"🌿 Reusing branch: {existing_branch}")
        run_cmd(f"git fetch origin {existing_branch}")
        run_cmd(f"git checkout {existing_branch}")
        run_cmd(f"git pull origin {existing_branch}")
        return existing_branch
    
    # No existing PR, create new branch
    branch_name = f"{BRANCH_PREFIX}/{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    logger.info(f"🌿 Creating feature branch: {branch_name}")
    
    # Safe checkout workflow
    success, output = run_cmd("git checkout main")
    if not success:
        logger.error(f"❌ Failed to checkout main: {output}")
        # Try to stash changes if checkout fails due to dirty state
        logger.warning("⚠️ Stashing local changes to proceed...")
        run_cmd("git stash push -m 'Night Shift Agent Stash'")
        success, output = run_cmd("git checkout main")
        if not success:
             logger.error("❌ Still failed to checkout main. Aborting.")
             sys.exit(1)

    run_cmd("git pull origin main")
    
    run_cmd(f"git checkout -b {branch_name}")
    logger.info(f"✅ Created branch: {branch_name}")
    return branch_name

def push_branch(branch_name: str) -> bool:
    logger.info(f"📤 Pushing branch to origin: {branch_name}")
    success, output = run_cmd(f"git push -u origin {branch_name} --force")
    if success: logger.info("✅ Pushed successfully")
    else: logger.error(f"❌ Push failed: {output}")
    return success

def create_pull_request(branch_name: str, title: str, body: str) -> tuple[bool, str]:
    logger.info("📝 Creating Pull Request...")
    repo_info = get_repo_info()
    repo_name = repo_info.get("nameWithOwner", "")
    if not repo_name: return False, "No repo"
    
    # Check if PR already exists for this branch
    success, existing_pr = run_cmd(f'gh pr view {branch_name} --repo {repo_name} --json url --jq .url')
    if success and existing_pr.strip():
        pr_url = existing_pr.strip()
        logger.info(f"✅ PR already exists: {pr_url}")
        return True, pr_url
    
    safe_title = title.replace('"', '\\"')
    safe_body = body.replace('"', '\\"')
    cmd = f'gh pr create --repo {repo_name} --head "{branch_name}" --title "{safe_title}" --body "{safe_body}"'
    success, output = run_cmd(cmd, timeout=60)
    if success:
        pr_url = output.strip().split('\n')[-1]
        logger.info(f"✅ Created PR: {pr_url}")
        return True, pr_url
    logger.error(f"❌ Failed: {output}")
    return False, output

def get_pr_number_from_branch(branch_name: str) -> str:
    repo_info = get_repo_info()
    repo_name = repo_info.get("nameWithOwner", "")
    success, output = run_cmd(f'gh pr list --state all --repo {repo_name} --head "{branch_name}" --json number --jq ".[0].number"')
    return output.strip() if success else ""

def is_pr_merged(branch_name: str) -> bool:
    pr_number = get_pr_number_from_branch(branch_name)
    if not pr_number: return False
    repo_info = get_repo_info()
    repo_name = repo_info.get("nameWithOwner", "")
    success, output = run_cmd(f'gh pr view {pr_number} --repo {repo_name} --json state --jq .state')
    return output.strip() == "MERGED" if success else False

def get_pr_status(branch_name: str) -> dict:
    pr_number = get_pr_number_from_branch(branch_name)
    if not pr_number: return {"success": False}
    repo_info = get_repo_info()
    success, output = run_cmd(f'gh pr checks {pr_number} --repo {repo_info.get("nameWithOwner", "")} --json name,state,conclusion')
    if success:
        try:
            checks = json.loads(output)
            return {"success": True, "all_passed": all(c.get("conclusion") == "success" for c in checks if c.get("state") == "completed"),
                    "any_failed": any(c.get("conclusion") == "failure" for c in checks),
                    "pending": any(c.get("state") in ["pending", "queued", "in_progress"] for c in checks)}
        except: pass
    return {"success": False}

def get_pr_check_logs(branch_name: str) -> str:
    pr_number = get_pr_number_from_branch(branch_name)
    if not pr_number: return "No PR"
    repo_info = get_repo_info()
    success, output = run_cmd(f'gh pr checks {pr_number} --repo {repo_info.get("nameWithOwner", "")} --json name,conclusion,detailsUrl')
    if not success: return output
    try:
        checks = json.loads(output)
        failed = [c for c in checks if c.get("conclusion") == "failure"]
        return "\n".join([f"- {c.get('name')}: {c.get('detailsUrl')}" for c in failed]) if failed else "No failures"
    except: return output

# =============================================================================
# TASK PROCESSING
# =============================================================================

def process_task(task: str, arch: str, files: str) -> bool:
    global build_state
    build_state.reset()
    system_prompt = f"""You are Night Shift Agent for a Kotlin Multiplatform project.

ARCHITECTURE:
{arch}

FILES:
{files}

RULES:
1. Create/modify Kotlin source files only
2. After code changes, run './gradlew assembleDebug :composeApp:linkDebugFrameworkIosSimulatorArm64 detekt' (NOT 'build') - this verifies BOTH Android and iOS
3. If build fails, fix YOUR CODE (not build files)
4. Commit when build passes
5. NEVER modify build.gradle.kts or settings.gradle.kts
6. VERIFICATION: You explicitly CANNOT assume code works until you see 'run_shell' output "BUILD SUCCESSFUL". You MUST Run 'run_shell' to verify.

TOOL USAGE FORMAT:
CRITICAL: You are a function-calling agent. You must NOT write conversational text.
You must ONLY output valid JSON objects to request actions.

CRITICAL: You MUST wrap your JSON response in <agent_action> tags.
Example:
<agent_action>
{{
  "action": "write_file",
  "args": {{ "path": "Main.kt", "content": "..." }}
}}
</agent_action>

AVAILABLE ACTIONS:
- read_file(path)
- write_file(path, content)
- replace(path, old_string, new_string)
- list_files(path)
- run_shell(command)

IMPORTANT: Output a JSON object with the "action" key, wrapped in <agent_action> tags.
"""
    
    base_prompt = system_prompt + f"\n\nTASK: {task}\n\nUSER: Start by searching or reading files. You must output a valid <agent_action>."
    history_chunks = []
    
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 5
    
    for iteration in range(MAX_ITERATIONS):
        logger.info(f"🔄 Iteration {iteration + 1}/{MAX_ITERATIONS}")
        
        # history pruning
        current_history = "".join(history_chunks)
        while len(history_chunks) > 2 and (len(base_prompt) + len(current_history)) > MAX_CONTEXT_CHARS:
             # Keep the last few turns, drop the oldest
             dropped = history_chunks.pop(0)
             current_history = "".join(history_chunks)
             logger.info(f"✂️ Pruned context: dropped {len(dropped)} chars")
             
        full_prompt = base_prompt + current_history

        response_text = call_llm_cli(full_prompt)
        if not response_text:
            logger.error(f"❌ No response from {AGENT_PROVIDER} CLI")
            return False
            
        current_turn = f"\n\nASSISTANT: {response_text}"
        
        # Parse for JSON format tool calls - handle multiple JSON blocks
        tool_calls_executed = 0
        if "{" in response_text and "}" in response_text:
            # Extract all JSON objects from the response
            json_objects = extract_json_objects(response_text)
            logger.debug(f"   🔍 Found {len(json_objects)} JSON objects in response")
            for json_str in json_objects:
                try:
                    data = json.loads(json_str)
                    # Support 'action' (standard) or 'tool' (legacy) keys
                    tool_name = data.get("action") or data.get("tool")
                    args = data.get("args", {})
                    
                    if tool_name:
                        # Standardize args (file_path -> path, file_content -> content)
                        if "path" not in args and "file_path" in args:
                            args["path"] = args.pop("file_path")
                        if "content" not in args and "file_content" in args:
                            args["content"] = args.pop("file_content")
                            
                        logger.info(f"🛠️ Tool Call: {tool_name}")
                        logger.debug(f"   🛠️ Args: {str(args)[:200]}...")
                        
                        func = available_functions.get(tool_name)
                        if func:
                            resp = func(**args)
                            if len(str(resp)) > 2000: resp = str(resp)[:1000] + f"...[truncated {len(str(resp))-2000} chars]..." + str(resp)[-1000:]
                            current_turn += f"\n\nTOOL OUTPUT ({tool_name}): {str(resp)}"
                            tool_calls_executed += 1
                        else:
                            logger.warning(f"   ⚠️ Unknown tool: {tool_name}")
                    else:
                        logger.debug(f"   🔍 JSON object missing 'tool' or 'args': {list(data.keys())}")
                except Exception as e:
                    logger.debug(f"   🔍 Failed to parse JSON object: {e}, content: {json_str[:100]}...")
        
        if tool_calls_executed > 0:
            # Track consecutive build failures
            if build_state.build_attempted and not build_state.build_passed:
                consecutive_failures += 1
                logger.warning(f"   ⚠️ Consecutive build failures: {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}")
                
                # Auto-revert after too many consecutive failures
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES and build_state.last_successful_files:
                    logger.warning(f"🔄 AUTO-REVERT: {consecutive_failures} consecutive failures, restoring checkpoint...")
                    reverted_files = []
                    for path, content in build_state.last_successful_files.items():
                        if path in build_state.files_changed_since_success:
                            try:
                                with open(path, 'w') as f:
                                    f.write(content)
                                reverted_files.append(path)
                                logger.info(f"   ↩️ Reverted: {path}")
                            except Exception as e:
                                logger.error(f"   ❌ Failed to revert {path}: {e}")
                    
                    build_state.files_changed_since_success = []
                    consecutive_failures = 0
                    
                    if reverted_files:
                        current_turn += f"\n\nSYSTEM: Auto-reverted {len(reverted_files)} files to last working state due to repeated failures. Files: {reverted_files}. Try a simpler approach."
                        logger.info(f"📸 Reverted {len(reverted_files)} files to checkpoint. Agent notified to try simpler approach.")
            elif build_state.build_attempted and build_state.build_passed:
                consecutive_failures = 0  # Reset on success
            
            if build_state.is_verified():
                logger.info("✅ Build verified (triggered by tool execution)")
                return True
            else:
                # Tool ran, but didn't verify. Warn the agent to run the actual build command.
                current_turn += "\n\nSYSTEM: You executed a tool, but the build is NOT verified. To complete the task, you MUST run the verification command: ./gradlew assembleDebug :composeApp:linkDebugFrameworkIosSimulatorArm64 detekt"

            history_chunks.append(current_turn)
            continue  # Continue to next iteration after processing tool calls
        else:
            # No tool calls detected - prompt the agent to act
            logger.warning("⚠️ No tool calls detected in response")
            current_turn += "\n\nSYSTEM: ERROR: No tool execution detected. You have NOT verified the build in this session. You MUST output a JSON tool call to run: ./gradlew assembleDebug :composeApp:linkDebugFrameworkIosSimulatorArm64 detekt"

        logger.info(f"\n🧠 Agent Report:\n{response_text}")
        if build_state.is_verified():
            logger.info("✅ Build verified")
            return True
        current_turn += "\n\nUSER: System: Run 'run_shell(\"./gradlew assembleDebug :composeApp:linkDebugFrameworkIosSimulatorArm64 detekt\")' to verify."
        history_chunks.append(current_turn)
    return False

def fix_ci_failure(branch_name: str, arch: str, files: str) -> bool:
    global build_state
    build_state.reset()
    logs = get_pr_check_logs(branch_name)
    
    base_prompt = f"""You are Night Shift Agent.
CI Failed for branch {branch_name}.
Logs:
{logs}

TOOL USAGE: OUTPUT JSON {{ "tool": "name", "args": {{...}} }}
TOOLS: read_file, write_file, list_files, run_shell

Fix code (not build files), then run './gradlew assembleDebug :composeApp:linkDebugFrameworkIosSimulatorArm64 detekt' to verify BOTH Android and iOS.
"""
    history_chunks = []

    for _ in range(MAX_ITERATIONS):
        # history pruning
        current_history = "".join(history_chunks)
        while len(history_chunks) > 2 and (len(base_prompt) + len(current_history)) > MAX_CONTEXT_CHARS:
             dropped = history_chunks.pop(0)
             current_history = "".join(history_chunks)
        
        full_prompt = base_prompt + current_history
        
        response_text = call_llm_cli(full_prompt)
        if not response_text: return False
        
        current_turn = f"\n\nASSISTANT: {response_text}"
        
        # Parse for JSON format tool calls - handle multiple JSON blocks
        tool_calls_executed = 0
        if "{" in response_text and "}" in response_text:
            json_objects = extract_json_objects(response_text)
            for json_str in json_objects:
                try:
                    data = json.loads(json_str)
                    if "tool" in data and "args" in data:
                        tool_name = data["tool"]
                        tool_args = data["args"]
                        logger.info(f"🛠️ Tool Call: {tool_name}")
                        func = available_functions.get(tool_name)
                        if func:
                            resp = func(**tool_args)
                            current_turn += f"\n\nTOOL OUTPUT ({tool_name}): {str(resp)[:2000]}"
                            tool_calls_executed += 1
                except: pass
        
        if tool_calls_executed > 0:
            history_chunks.append(current_turn)
            continue  # Continue to next iteration after processing tool calls
            
        if build_state.is_verified(): return True
        current_turn += "\n\nUSER: Run './gradlew assembleDebug :composeApp:linkDebugFrameworkIosSimulatorArm64 detekt'."
        history_chunks.append(current_turn)
    return False

# =============================================================================
# MAIN
# =============================================================================

def main():
    logger.info("""
    ╔══════════════════════════════════════════════════════════════╗
    ║  🌙 Night Shift Agent v3.6                                   ║
    ║  Autonomous Coding Assistant with Direct Push Workflow       ║
    ╚══════════════════════════════════════════════════════════════╝
    """)
    
    if not GH_BOT_TOKEN:
        logger.warning("⚠️ GH_BOT_TOKEN not set - git operations may fail")
    
    logger.info(f"📁 Project: {PROJECT_DIR}")
    logger.info(f"🤖 Provider: {AGENT_PROVIDER}, Model: {AGENT_MODEL}")
    logger.info(f"📝 Log file: {LOG_FILE}")
    
    arch = read_file("ARCHITECTURE.md") if os.path.exists("ARCHITECTURE.md") else ""
    files = list_files()
    
    if not os.path.exists("tasks.txt"):
        logger.warning("⚠️ No tasks.txt found")
        return
    
    if not setup_origin_with_bot_token():
        logger.error("❌ Failed to configure git")
        sys.exit(1)
    
    feature_branch = create_feature_branch()
    
    while True:
        with open("tasks.txt", "r") as f: lines = f.readlines()
        task_index = -1
        current_task = ""
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not any(stripped.startswith(p) for p in ["[x]", "[!]", "#"]):
                task_index = i
                current_task = stripped
                break
        if task_index == -1:
            logger.info("📋 No more tasks to process")
            break
        
        logger.info(f"\n{'='*60}")
        logger.info(f"▶️ Processing Task {task_index + 1}: {current_task}")
        logger.info(f"{'='*60}")
        
        if process_task(current_task, arch, files):
            lines[task_index] = f"[x] {lines[task_index].lstrip()}"
            logger.info(f"✅ Done: {current_task}")
        else:
            lines[task_index] = f"[!] {lines[task_index].lstrip()}"
            logger.error(f"❌ Failed: {current_task}")
        
        with open("tasks.txt", "w") as f: f.writelines(lines)
        time.sleep(2)
    
    with open("tasks.txt", "r") as f: final_lines = f.readlines()
    tasks_succeeded = sum(1 for l in final_lines if l.startswith("[x]"))
    tasks_failed = sum(1 for l in final_lines if l.startswith("[!]"))
    completed = [l.replace("[x]", "").strip() for l in final_lines if l.startswith("[x]")]
    failed = [l.replace("[!]", "").strip() for l in final_lines if l.startswith("[!]")]
    
    # Log accurate task summary
    if tasks_failed > 0:
        logger.warning(f"⚠️ Task Summary: {tasks_succeeded} succeeded, {tasks_failed} failed")
    else:
        logger.info(f"✅ All {tasks_succeeded} tasks completed successfully!")
    
    if tasks_succeeded == 0:
        logger.warning("⚠️ No tasks completed successfully. Skipping PR.")
        run_cmd("git checkout main")
        return
    
    logger.info("\n" + "="*60)
    logger.info("📤 Creating Pull Request")
    logger.info("="*60)
    
    # Commit all changes
    logger.info("📝 Committing changes...")
    run_cmd("git add -A")
    # Sanitize task names for commit message (remove quotes, limit length)
    def sanitize_task(t):
        return t.replace('"', '').replace("'", "")[:50]
    safe_tasks = [sanitize_task(t) for t in completed[:3]]
    commit_msg = f"🌙 Night Shift: {', '.join(safe_tasks)}{'...' if len(completed) > 3 else ''}"
    # Use single quotes for outer shell and escape any single quotes in message
    escaped_msg = commit_msg.replace("'", "'\"'\"'")
    success, output = run_cmd(f"git commit -m '{escaped_msg}'")
    if not success and "nothing to commit" in output:
        logger.warning("⚠️ No changes to commit")
    elif not success:
        logger.error(f"❌ Failed to commit: {output}")
        return
    else:
        logger.info("✅ Changes committed")
    
    if not push_branch(feature_branch):
        logger.error("❌ Failed to push")
        return
    
    pr_title = f"🌙 Night Shift: {tasks_succeeded} task(s)" + (f" ({tasks_failed} failed)" if tasks_failed > 0 else "")
    pr_body = f"## 🌙 Night Shift Agent Report\n\n**Succeeded**: {tasks_succeeded}\n**Failed**: {tasks_failed}\n\n### ✅ Completed\n" + "\n".join([f"- [x] {t}" for t in completed])
    if failed:
        pr_body += "\n\n### ❌ Failed\n" + "\n".join([f"- [ ] {t}" for t in failed])
    
    pr_success, pr_url = create_pull_request(feature_branch, pr_title, pr_body)
    if not pr_success:
        logger.error(f"❌ Failed to create PR: {pr_url}")
        return
    
    logger.info(f"✅ PR Created: {pr_url}")
    
    logger.info("\n" + "="*60)
    logger.info("🔍 Monitoring CI Status (will wait up to 30 minutes)")
    logger.info("="*60)
    
    MAX_CI_WAIT_POLLS = 30
    ci_passed = False
    
    for poll in range(MAX_CI_WAIT_POLLS):
        logger.info(f"⏳ Poll {poll + 1}/{MAX_CI_WAIT_POLLS}: Waiting {CI_POLL_INTERVAL}s for CI...")
        time.sleep(CI_POLL_INTERVAL)
        
        status = get_pr_status(feature_branch)
        if not status.get("success"):
            logger.info("⏳ Could not get status, retrying...")
            continue
        
        if status.get("pending"):
            logger.info("⏳ CI still running...")
            
            # Check if merged while running
            if is_pr_merged(feature_branch):
                logger.info("🎉 PR Merged! Stopping monitor.")
                ci_passed = True
                break
                
            continue
        
        if status.get("all_passed"):
            logger.info("🎉 ALL CI CHECKS PASSED!")
            ci_passed = True
            break
        
        if status.get("any_failed"):
            logger.warning(f"❌ CI failed! Attempting fix...")
            if fix_ci_failure(feature_branch, arch, files):
                push_branch(feature_branch)
                logger.info("📤 Pushed CI fix, waiting for new run...")
            else:
                logger.error("❌ Could not fix CI failure")
    
    # Final Summary
    logger.info("\n" + "="*60)
    logger.info("📊 FINAL SESSION SUMMARY")
    logger.info("="*60)
    logger.info(f"   Tasks succeeded: {tasks_succeeded}")
    if tasks_failed > 0:
        logger.warning(f"   Tasks failed: {tasks_failed}")
    logger.info(f"   PR URL: {pr_url}")
    
    if ci_passed:
        logger.info("   CI Status: ✅ ALL CHECKS PASSED - PR IS READY TO MERGE!")
    else:
        logger.warning("   CI Status: ⚠️ CI did not complete successfully")
        logger.warning("   Manual review may be required")
    
    logger.info("="*60)
    
    run_cmd("git checkout main")

if __name__ == "__main__":
    main()
