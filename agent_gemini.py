#!/usr/bin/env python3
"""
🌙 Night Shift Agent v3.2 - Autonomous Coding Assistant
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

MODEL_NAME = os.getenv("AGENT_MODEL", "gemini-3-flash-preview")
GH_BOT_TOKEN = os.getenv("GH_BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "agentnightshift")

MAX_ITERATIONS = 50
MAX_RETRIES = 3
MAX_CI_FIX_ATTEMPTS = 5
RETRY_BASE_DELAY = 2
CI_POLL_INTERVAL = 60
MAX_FILES_IN_CONTEXT = 100
REQUIRE_BUILD_VERIFICATION = True

BRANCH_PREFIX = "nightshift"

PROTECTED_FILES = {"build.gradle.kts", "settings.gradle.kts", "gradle.properties", "libs.versions.toml", "gradle-wrapper.properties"}

LOG_DIR = Path(".agent_logs")
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
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
    def reset(self): self.__init__()
    def is_verified(self) -> bool:
        return self.build_attempted and self.build_passed if REQUIRE_BUILD_VERIFICATION else True

build_state = BuildState()

# =============================================================================
# TOOLS
# =============================================================================

def read_file(path: str) -> str:
    try:
        with open(path, "r") as f: content = f.read()
        logger.info(f"📖 Read file: {path} ({len(content)} bytes)")
        return content
    except Exception as e:
        logger.error(f"❌ Failed to read {path}: {e}")
        return f"Error reading file {path}: {e}"

def write_file(path: str, content: str) -> str:
    filename = os.path.basename(path)
    if filename in PROTECTED_FILES:
        logger.warning(f"🛡️ BLOCKED: {path}")
        return f"ERROR: {filename} is protected. Fix source code instead."
    try:
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w") as f: f.write(content)
        logger.info(f"✍️ Wrote file: {path} ({len(content)} bytes)")
        build_state.build_passed = False
        build_state.build_attempted = False
        return f"Successfully wrote to {path}"
    except Exception as e:
        logger.error(f"❌ Failed to write {path}: {e}")
        return f"Error writing to file {path}: {e}"

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
    logger.info(f"🤖 Executing: {command}")
    try:
        env = os.environ.copy()
        if GH_BOT_TOKEN and command.strip().startswith("gh "):
            env["GITHUB_TOKEN"] = GH_BOT_TOKEN
            env["GH_TOKEN"] = GH_BOT_TOKEN
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=600, env=env)
        output = result.stdout + result.stderr
        if any(kw in command.lower() for kw in ["gradlew", "gradle", "build", "compile", "assemble"]):
            build_state.build_attempted = True
            build_state.build_passed = result.returncode == 0
            if result.returncode == 0: logger.info("✅ Command succeeded")
            else: logger.warning(f"⚠️ Command failed (exit {result.returncode})")
        if result.returncode != 0:
            return f"Command failed (exit {result.returncode}):\n{output}"
        return output
    except subprocess.TimeoutExpired: return "Error: Command timed out"
    except Exception as e: return f"Error: {e}"

# Tools list removed - handled by system prompt

def extract_json_objects(text: str) -> list:
    """Extract all valid JSON objects from a string by tracking brace depth."""
    objects = []
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
                            objects.append(text[start:i+1])
                            break
                i += 1
        i += 1
    return objects

available_functions = {"read_file": read_file, "write_file": write_file, "list_files": list_files, "run_shell": run_shell}

def call_gemini_cli(prompt: str):
    logger.info("🤖 Calling Gemini CLI...")
    for attempt in range(MAX_RETRIES):
        try:
            # Pushing prompt via stdin
            result = subprocess.run(
                ["gemini", "--model", MODEL_NAME, "--output-format", "text"],
                input=prompt,
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
            return result.stdout.strip()
        except Exception as e:
            logger.warning(f"⚠️ CLI Exception (Attempt {attempt+1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
            else:
                return None
    return None

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
2. After code changes, run './gradlew assembleDebug detekt' (NOT 'build')
3. If build fails, fix YOUR CODE (not build files)
4. Commit when build passes
5. NEVER modify build.gradle.kts or settings.gradle.kts

TOOL USAGE FORMAT:
To Use a tool, OUTPUT STRICT JSON ONLY:
{{
  "tool": "tool_name",
  "args": {{ "arg1": "value" }}
}}

AVAILABLE TOOLS:
- read_file(path)
- write_file(path, content)
- list_files(path)
- run_shell(command)
"""
    full_history = system_prompt + f"\n\nTASK: {task}\n"
    
    for iteration in range(MAX_ITERATIONS):
        logger.info(f"🔄 Iteration {iteration + 1}/{MAX_ITERATIONS}")
        
        response_text = call_gemini_cli(full_history)
        if not response_text:
            logger.error("❌ No response from Gemini CLI")
            return False
            
        full_history += f"\n\nASSISTANT: {response_text}"
        
        # Parse for JSON format tool calls - handle multiple JSON blocks
        tool_calls_executed = 0
        if "{" in response_text and "}" in response_text:
            # Extract all JSON objects from the response
            json_objects = extract_json_objects(response_text)
            for json_str in json_objects:
                try:
                    data = json.loads(json_str)
                    if "tool" in data and "args" in data:
                        tool_name = data["tool"]
                        args = data["args"]
                        logger.info(f"🛠️ Tool Call: {tool_name}")
                        
                        func = available_functions.get(tool_name)
                        if func:
                            resp = func(**args)
                            if len(str(resp)) > 10000: resp = str(resp)[:5000] + "...[truncated]..." + str(resp)[-5000:]
                            full_history += f"\n\nTOOL OUTPUT ({tool_name}): {str(resp)}"
                            tool_calls_executed += 1
                except:
                    pass # Not a valid tool call JSON
        
        if tool_calls_executed > 0:
            continue  # Continue to next iteration after processing tool calls

        logger.info(f"\n🧠 Agent Report:\n{response_text}")
        if build_state.is_verified():
            logger.info("✅ Build verified")
            return True
        full_history += "\n\nUSER: System: Run 'run_shell(\"./gradlew assembleDebug detekt\")' to verify."
    return False

def fix_ci_failure(branch_name: str, arch: str, files: str) -> bool:
    global build_state
    build_state.reset()
    logs = get_pr_check_logs(branch_name)
    
    current_prompt = f"""You are Night Shift Agent.
CI Failed for branch {branch_name}.
Logs:
{logs}

TOOL USAGE: OUTPUT JSON {{ "tool": "name", "args": {{...}} }}
TOOLS: read_file, write_file, list_files, run_shell

Fix code (not build files), then run './gradlew assembleDebug detekt'.
"""
    full_history = current_prompt

    for _ in range(MAX_ITERATIONS):
        response_text = call_gemini_cli(full_history)
        if not response_text: return False
        
        full_history += f"\n\nASSISTANT: {response_text}"
        
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
                            full_history += f"\n\nTOOL OUTPUT ({tool_name}): {str(resp)[:5000]}"
                            tool_calls_executed += 1
                except: pass
        
        if tool_calls_executed > 0:
            continue  # Continue to next iteration after processing tool calls
            
        if build_state.is_verified(): return True
        full_history += "\n\nUSER: Run './gradlew assembleDebug detekt'."
    return False

# =============================================================================
# MAIN
# =============================================================================

def main():
    logger.info("""
    ╔══════════════════════════════════════════════════════════════╗
    ║  🌙 Night Shift Agent v3.2                                   ║
    ║  Autonomous Coding Assistant with Direct Push Workflow       ║
    ╚══════════════════════════════════════════════════════════════╝
    """)
    
    if not GH_BOT_TOKEN:
        logger.warning("⚠️ GH_BOT_TOKEN not set - git operations may fail")
    
    logger.info(f"📁 Project: {PROJECT_DIR}")
    logger.info(f"🤖 Model: {MODEL_NAME}")
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
            logger.info("✅ All tasks completed!")
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
    completed = [l.replace("[x]", "").strip() for l in final_lines if l.startswith("[x]")]
    
    if tasks_succeeded == 0:
        logger.warning("⚠️ No tasks completed. Skipping PR.")
        run_cmd("git checkout main")
        return
    
    logger.info("\n" + "="*60)
    logger.info("📤 Creating Pull Request")
    logger.info("="*60)
    
    # Commit all changes
    logger.info("📝 Committing changes...")
    run_cmd("git add -A")
    commit_msg = f"🌙 Night Shift: {', '.join(completed[:3])}{'...' if len(completed) > 3 else ''}"
    success, output = run_cmd(f'git commit -m "{commit_msg}"')
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
    
    pr_title = f"🌙 Night Shift: {tasks_succeeded} task(s)"
    pr_body = f"## 🌙 Night Shift Agent Report\n\n**Tasks**: {tasks_succeeded}\n\n" + "\n".join([f"- [x] {t}" for t in completed])
    
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
    logger.info(f"   Tasks processed: {tasks_succeeded}")
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
