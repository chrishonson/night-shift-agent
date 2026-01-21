#!/usr/bin/env python3
"""
🌙 Night Shift Agent v3.6 - Autonomous Coding Assistant
========================================================
Refactored Architecture:
- NightShiftAgent (Main Controller)
- LLMClient (Provider Abstraction with Failover)
- Toolbox (File & Shell Operations)
- BuildState (Context & Verification)
"""

import os
import subprocess
import sys
import re
import time
import json
import logging
import argparse
import difflib
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# =============================================================================
# CONFIGURATION & CONSTANTS
# =============================================================================

load_dotenv()

# Defaults
DEFAULT_PROVIDER = "gemini"
DEFAULT_MODEL_GEMINI = "gemini-3-flash-preview"
DEFAULT_MODEL_OPENROUTER = "mistralai/devstral-2-2512:free"  # Free coding-focused model
DEFAULT_MODEL_CLAUDE = "claude-3-5-sonnet-20241022" 

MAX_ITERATIONS = 50
MAX_RETRIES = 5
MAX_CI_FIX_ATTEMPTS = 5
RETRY_BASE_DELAY = 5
MAX_FILES_IN_CONTEXT = 50
MAX_CONTEXT_CHARS = 30000
MAX_TOOL_OUTPUT_CHARS = 50000  # 50KB max per tool output to prevent context explosion
REQUIRE_BUILD_VERIFICATION = True
BRANCH_PREFIX = "nightshift"

PROTECTED_FILES = {
    "build.gradle.kts", "settings.gradle.kts", "gradle.properties", 
    "libs.versions.toml", "gradle-wrapper.properties", "tasks.txt"
}

# Logger Setup - Console only at module level, file handlers added in NightShiftAgent.__init__
SESSION_TIMESTAMP = datetime.now().strftime('%Y%m%d_%H%M%S')

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)]  # Console only at module load
)
logger = logging.getLogger("NightShiftAgent")

# Prompt logger - file handler added later in project directory
prompt_logger = logging.getLogger("PromptLogger")
prompt_logger.setLevel(logging.DEBUG)
prompt_logger.propagate = False  # Don't duplicate to main logger

# =============================================================================
# EXCEPTIONS
# =============================================================================

class QuotaExceededError(Exception):
    """Exception raised when an LLM provider hits a rate limit or quota."""
    pass

# =============================================================================
# HELPER CLASSES
# =============================================================================

class BuildState:
    """Tracks the state of the build and file changes for verification/rolling back."""
    def __init__(self):
        self.build_attempted = False
        self.build_passed = False
        self.last_error = None
        self.last_successful_files = {}  # path -> content snapshot
        self.files_changed_since_success = [] 

    def reset(self):
        self.build_attempted = False
        self.build_passed = False
        self.last_error = None

    def checkpoint(self, files_written: list):
        """Snapshot file contents after successful build."""
        for path in files_written:
            try:
                with open(path, 'r') as f:
                    self.last_successful_files[path] = f.read()
            except: pass
        self.files_changed_since_success = []
        logger.info(f"📸 Checkpoint saved: {len(self.last_successful_files)} files known-good")

    def is_verified(self):
        return self.build_passed

class RateLimiter:
    """Prevents command spam loops."""
    def __init__(self, window_seconds=30, max_identical=3):
        self.recent_commands = []
        self.window = window_seconds
        self.max_identical = max_identical
    
    def should_allow(self, command: str) -> tuple[bool, str]:
        current_time = time.time()
        self.recent_commands = [(t, c) for t, c in self.recent_commands if current_time - t < self.window]
        identical_count = sum(1 for _, c in self.recent_commands if c == command)
        if identical_count >= self.max_identical:
            return False, f"Rate limited: same command executed {identical_count} times in {self.window}s"
        self.recent_commands.append((current_time, command))
        return True, ""

# =============================================================================
# LLM CLIENT (FAILOVER LOGIC)
# =============================================================================

# =============================================================================
# LLM PROVIDER ABSTRACTION
# =============================================================================

from abc import ABC, abstractmethod
from typing import List, Optional

class LLMProvider(ABC):
    """Abstract base class for LLM providers (CLI or API)."""
    
    @abstractmethod
    def ask(self, prompt: str) -> Optional[str]:
        """Sends prompt to the model and returns text response."""
        pass
        
    @property
    @abstractmethod
    def name(self) -> str:
        """Friendly name for logging."""
        pass
        
    def strip_markdown_code_blocks(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            lines = text.split('\n')
            if lines[0].startswith("```"): lines = lines[1:]
            if lines and lines[-1].strip() == "```": lines = lines[:-1]
            return '\n'.join(lines).strip()
        return text

class GeminiCLIProvider(LLMProvider):
    def __init__(self, model=DEFAULT_MODEL_GEMINI):
        self.model = model
        
    @property
    def name(self): return f"Gemini CLI ({self.model})"

    def ask(self, prompt: str) -> Optional[str]:
        prompt_logger.debug(
            f"{'='*80}\n>>> PROMPT TO {self.name}\n{'='*80}\n{prompt}\n{'='*80}\n"
        )
        
        for attempt in range(MAX_RETRIES):
            try:
                # Use stdin for prompt to avoid "Argument list too long" errors
                # The gemini CLI accepts prompt via stdin when no positional arg given
                result = subprocess.run(
                    ["gemini", "--model", self.model, "--sandbox", "false", "--output-format", "json"],
                    input=prompt,
                    capture_output=True, text=True, timeout=300
                )
                
                self._log_raw_response(attempt, result)
                self._check_quota(result.stderr)
                
                if result.returncode == 0 and result.stdout.strip():
                    return self._parse_json_response(result.stdout)
                
                if result.returncode != 0:
                    logger.warning(f"⚠️ {self.name} Error ({attempt+1}/{MAX_RETRIES}): {result.stderr}")
                    self._backoff(attempt)
                    
            except QuotaExceededError:
                raise
            except Exception as e:
                logger.warning(f"⚠️ {self.name} Exception ({attempt+1}/{MAX_RETRIES}): {e}")
                self._backoff(attempt)
        return None

    def _log_raw_response(self, attempt, result):
        prompt_logger.debug(
            f"{'='*80}\n<<< RAW RESPONSE ({attempt+1}) RC:{result.returncode}\n{'='*80}\n"
            f"STDOUT:\n{result.stdout}\n{'~'*40}\nSTDERR:\n{result.stderr}\n{'='*80}\n"
        )

    def _check_quota(self, stderr):
        lower_err = stderr.lower()
        if any(x in lower_err for x in ["quota exceeded", "resource exhausted", "rate limit"]):
            logger.error(f"🚨 {self.name} Quota Exceeded!")
            raise QuotaExceededError(f"{self.name} Quota Exceeded")
        if "429" in lower_err and ("error" in lower_err or "too many" in lower_err):
            logger.error(f"🚨 {self.name} Rate Limited (429)!")
            raise QuotaExceededError(f"{self.name} Rate Limited")

    def _parse_json_response(self, stdout):
        try:
            data = json.loads(stdout.strip())
            if isinstance(data, list): data = data[0]
            # Extract content from various potential CLI JSON schemas
            response_text = data.get("response") or data.get("content") or json.dumps(data)
            parsed = self.strip_markdown_code_blocks(response_text)
            prompt_logger.debug(f"{'='*80}\n<<< PARSED RESPONSE\n{'='*80}\n{parsed}\n{'='*80}\n")
            return parsed
        except json.JSONDecodeError:
             logger.warning(f"⚠️ Failed to parse JSON: {stdout[:200]}")
             return None

    def _backoff(self, attempt):
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))

class ClaudeCLIProvider(LLMProvider):
    def __init__(self, model=""):
        self.model = model
        
    @property
    def name(self): return f"Claude CLI ({self.model or 'default'})"

    def ask(self, prompt: str) -> Optional[str]:
        prompt_logger.debug(
            f"{'='*80}\n>>> PROMPT TO {self.name}\n{'='*80}\n{prompt}\n{'='*80}\n"
        )
        
        for attempt in range(MAX_RETRIES):
            try:
                import tempfile
                with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
                    f.write(prompt)
                    temp_path = f.name
                
                try:
                    model_arg = f"--model {self.model}" if self.model else ""
                    cmd = f"cat {temp_path} | claude -p {model_arg} --dangerously-skip-permissions"
                    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
                    
                    self._log_raw_response(attempt, result)
                    self._check_quota(result.stdout + result.stderr)

                    if result.returncode != 0:
                        logger.warning(f"⚠️ {self.name} Error ({attempt+1}/{MAX_RETRIES}): {result.stderr}")
                        self._backoff(attempt)
                        continue
                    
                    parsed = self.strip_markdown_code_blocks(result.stdout.strip())
                    prompt_logger.debug(f"{'='*80}\n<<< PARSED RESPONSE\n{'='*80}\n{parsed}\n{'='*80}\n")
                    return parsed
                    
                finally:
                    if os.path.exists(temp_path): os.unlink(temp_path)

            except QuotaExceededError:
                raise
            except Exception as e:
                logger.warning(f"⚠️ {self.name} Exception ({attempt+1}/{MAX_RETRIES}): {e}")
                self._backoff(attempt)
        return None

    def _log_raw_response(self, attempt, result):
        prompt_logger.debug(
            f"{'='*80}\n<<< RAW RESPONSE ({attempt+1}) RC:{result.returncode}\n{'='*80}\n"
            f"STDOUT:\n{result.stdout}\n{'~'*40}\nSTDERR:\n{result.stderr}\n{'='*80}\n"
        )

    def _check_quota(self, combined_output):
        lower = combined_output.lower()
        if any(x in lower for x in ["hit your limit", "rate limit", "quota exceeded"]):
            logger.error(f"🚨 {self.name} Quota Exceeded!")
            raise QuotaExceededError(f"{self.name} Quota Exceeded")

    def _backoff(self, attempt):
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))

class OpenRouterAPIProvider(LLMProvider):
    def __init__(self):
        pass  # Model and key read fresh on each request to allow runtime changes
        
    @property
    def name(self): 
        model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL_OPENROUTER)
        return f"OpenRouter API ({model})"

    def ask(self, prompt: str) -> Optional[str]:
        api_key = os.getenv("OPENROUTER_API_KEY")
        model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL_OPENROUTER)
        
        if not api_key:
            logger.info(f"⏭️ Skipping {self.name}: OPENROUTER_API_KEY not set")
            raise QuotaExceededError("OpenRouter Key Missing") # Treat as unavailable

        prompt_logger.debug(
            f"{'='*80}\n>>> PROMPT TO {self.name}\n{'='*80}\n{prompt}\n{'='*80}\n"
        )
        
        # We need generic requests, but trying to avoid external deps if possible.
        # But for OpenRouter, curl/requests is needed. Let's use standard lib urllib to avoid forcing 'requests' install.
        import urllib.request
        import json
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/chrishonson/night-shift-agent",
        }
        data = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}]
        }
        
        for attempt in range(MAX_RETRIES):
            try:
                req = urllib.request.Request(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    data=json.dumps(data).encode('utf-8')
                )
                
                with urllib.request.urlopen(req, timeout=60) as response:
                    resp_body = response.read().decode('utf-8')
                    # Log raw
                    self._log_raw_response(attempt, 200, resp_body)
                    
                    data = json.loads(resp_body)
                    if "error" in data:
                         # Check for rate limits in API error
                         err_msg = json.dumps(data['error']).lower()
                         if "rate limit" in err_msg or "quota" in err_msg or "insufficient" in err_msg:
                             raise QuotaExceededError(err_msg)
                         raise Exception(f"OpenRouter Error: {err_msg}")
                         
                    content = data['choices'][0]['message']['content']
                    parsed = self.strip_markdown_code_blocks(content)
                    prompt_logger.debug(f"{'='*80}\n<<< PARSED RESPONSE\n{'='*80}\n{parsed}\n{'='*80}\n")
                    return parsed

            except urllib.error.HTTPError as e:
                err_text = e.read().decode('utf-8')
                self._log_raw_response(attempt, e.code, err_text)
                if e.code == 429:
                    raise QuotaExceededError("OpenRouter 429")
                logger.warning(f"⚠️ {self.name} HTTP Error {e.code}: {err_text}")
                self._backoff(attempt)
            except Exception as e:
                logger.warning(f"⚠️ {self.name} Exception: {e}")
                self._backoff(attempt)
        return None

    def _log_raw_response(self, attempt, status, text):
        prompt_logger.debug(
            f"{'='*80}\n<<< RAW RESPONSE ({attempt+1}) Status:{status}\n{'='*80}\n"
            f"{text}\n{'='*80}\n"
        )
    
    def _backoff(self, attempt):
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))

class ProviderManager:
    """Manages a list of providers and handles failover."""
    def __init__(self):
        self.providers: List[LLMProvider] = []
        self.current_index = 0  # Persist which provider is working
        self._init_providers()
        
    def _init_providers(self):
        # Determine order based on env preference
        preferred = os.getenv("PREFERRED_AGENT_PROVIDER", "gemini")
        gemini_model = os.getenv("PREFERRED_AGENT_MODEL", DEFAULT_MODEL_GEMINI)
        
        p1 = GeminiCLIProvider(gemini_model)
        p2 = ClaudeCLIProvider() # Default claude model
        p3 = OpenRouterAPIProvider() # Fallback to openrouter
        
        if preferred == "claude":
            self.providers = [p2, p1, p3]
        elif preferred == "openrouter":
            self.providers = [p3, p1, p2]
        else:
            self.providers = [p1, p2, p3]
            
        logger.info(f"🔌 Provider Chain: {[p.name for p in self.providers]}")

    def ask(self, prompt: str) -> Optional[str]:
        # Start from the last working provider, not always from 0
        start_index = self.current_index
        
        for offset in range(len(self.providers)):
            i = (start_index + offset) % len(self.providers)
            provider = self.providers[i]
            
            try:
                result = provider.ask(prompt)
                # Success! Remember this provider for next time
                self.current_index = i
                return result
            except QuotaExceededError:
                logger.warning(f"🛑 {provider.name} Quota/Key Limit. Switching...")
                next_i = (i + 1) % len(self.providers)
                if next_i != start_index:  # Haven't looped back yet
                    logger.info(f"🔄 Switching to: {self.providers[next_i].name}...")
                    continue
                else:
                    logger.error("❌ All providers exhausted!")
                    return None
            except Exception as e:
                logger.error(f"❌ Critical error in {provider.name}: {e}")
                # Failover on crash too
                next_i = (i + 1) % len(self.providers)
                if next_i != start_index: continue
                return None
        return None

    def strip_markdown_code_blocks(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            lines = text.split('\n')
            if lines[0].startswith("```"): lines = lines[1:]
            if lines and lines[-1].strip() == "```": lines = lines[:-1]
            return '\n'.join(lines).strip()
        return text


# =============================================================================
# TOOLBOX
# =============================================================================

class Toolbox:
    def __init__(self, build_state: BuildState):
        self.build_state = build_state
        self.rate_limiter = RateLimiter()

    def read_file(self, path: str = None, file_path: str = None) -> str:
        target = path or file_path
        if not target: return "Error: No path"
        try:
            with open(target, "r") as f: content = f.read()
            logger.info(f"📖 Read file: {target}")
            return content
        except Exception as e: return f"Error reading {target}: {e}"

    def write_file(self, path: str = None, content: str = None, **kwargs) -> str:
        target = path or kwargs.get('file_path')
        content = content or kwargs.get('code') or kwargs.get('file_content') or ""
        if not target: return "Error: No path"
        
        if os.path.basename(target) in PROTECTED_FILES:
            return f"ERROR: {target} is protected."

        try:
            os.makedirs(os.path.dirname(target) if os.path.dirname(target) else ".", exist_ok=True)
            with open(target, "w") as f: f.write(content)
            logger.info(f"✍️ Wrote file: {target}")
            
            self.build_state.files_changed_since_success.append(target)
            self.build_state.build_passed = False
            self.build_state.build_attempted = False
            return f"Successfully wrote to {target}"
        except Exception as e: return f"Error writing {target}: {e}"

    def run_shell(self, command: str) -> str:
        allowed, reason = self.rate_limiter.should_allow(command)
        if not allowed: return f"Error: {reason}"
        
        # Auto-add exclusions for grep commands to avoid matching build artifacts
        cmd_stripped = command.strip()
        if cmd_stripped.startswith("grep ") and "--exclude-dir" not in command:
            # Add common exclusions for build directories
            exclusions = "--exclude-dir=.git --exclude-dir=.gradle --exclude-dir=build --exclude-dir=node_modules --exclude-dir=.idea"
            # Insert exclusions after 'grep'
            command = command.replace("grep ", f"grep {exclusions} ", 1)
            logger.info(f"🔧 Auto-excluded build dirs: {command}")

        logger.info(f"🤖 Executing: {command}")
        
        env = os.environ.copy()
        if os.getenv("GH_BOT_TOKEN") and cmd_stripped.startswith("gh "):
            env["GITHUB_TOKEN"] = os.getenv("GH_BOT_TOKEN")

        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=600, env=env)
            
            # Build detection - check command start to avoid false positives (e.g., ".gradle" in grep)
            is_gradle_build = any(cmd_stripped.startswith(x) for x in ["./gradlew", "gradlew ", "gradle "])
            if is_gradle_build:
                self.build_state.build_attempted = True
                self.build_state.build_passed = (result.returncode == 0)
                if result.returncode == 0:
                    logger.info("✅ Build Succeeded")
                    if self.build_state.files_changed_since_success:
                        self.build_state.checkpoint(self.build_state.files_changed_since_success)
                else:
                    logger.warning(f"⚠️ Build Failed (Exit {result.returncode})")

            output = result.stdout + result.stderr
            
            # Truncate large outputs to prevent context overflow
            if len(output) > MAX_TOOL_OUTPUT_CHARS:
                truncated_msg = f"\n\n[OUTPUT TRUNCATED - showing last {MAX_TOOL_OUTPUT_CHARS} chars of {len(output)} total]"
                output = output[-MAX_TOOL_OUTPUT_CHARS:] + truncated_msg
            
            if result.returncode != 0:
                return f"Command failed (exit {result.returncode}):\n{output}"
            return output
        except Exception as e: return f"Error: {e}"

    def replace(self, path: str = None, old_string: str = None, new_string: str = None, **kwargs) -> str:
        target = path or kwargs.get('file_path')
        if not target or not old_string or new_string is None: return "Error: Missing args"
        
        try:
            with open(target, "r") as f: content = f.read()
            if new_string in content: return f"Success: Text already present in {target}"
            if old_string not in content: return f"Error: Text not found in {target}"
            
            with open(target, "w") as f: f.write(content.replace(old_string, new_string, 1))
            
            logger.info(f"✏️ Replaced text in: {target}")
            self.build_state.files_changed_since_success.append(target)
            self.build_state.build_passed = False
            self.build_state.build_attempted = False
            return f"Successfully replaced text in {target}"
        except Exception as e: return f"Error: {e}"
    
    def list_files(self, path="."):
        files = []
        ignore = {".git", ".gradle", ".idea", "build", ".kotlin", "node_modules", ".agent_logs"}
        for root, dirs, filenames in os.walk(path):
            dirs[:] = [d for d in dirs if d not in ignore and not d.startswith(".")]
            for f in filenames:
                if not f.startswith(".") and not f.endswith(('.jar','.class','.pyc')):
                    files.append(os.path.join(root, f))
        return "\n".join(files[:MAX_FILES_IN_CONTEXT])

    def dispatch(self, tool_name, args):
        mapping = {
            "read_file": self.read_file, "write_file": self.write_file,
            "run_shell": self.run_shell, "run_shell_command": self.run_shell, # Alias
            "replace": self.replace, "list_files": self.list_files
        }
        
        # Arg Normalization
        if tool_name in ["run_shell", "run_shell_command"]:
            # Extensive alias mapping for creative models
            cmd = (args.get("command") or args.get("cmd") or args.get("code") or 
                   args.get("script") or args.get("command_line") or 
                   args.get("cli") or args.get("exec") or args.get("input") or args.get("bash"))
            if cmd: args["command"] = cmd
        
        if tool_name == "replace":
            old = args.get("search") or args.get("old") or args.get("original") or args.get("pattern")
            new = args.get("replace") or args.get("new") or args.get("content") or args.get("replacement")
            if old: args["old_string"] = old
            if new: args["new_string"] = new
        
        # Normalize path arguments across all file-related tools
        if "dir_path" in args: args["path"] = args.pop("dir_path")
        if "directory" in args: args["path"] = args.pop("directory")
        if "file_path" in args: args["path"] = args.pop("file_path")
        
        # Handle search_file_content by mapping to grep
        if tool_name == "search_file_content":
            pattern = args.get("pattern", "")
            include = args.get("include", "*.kt")
            # Convert to grep command and clear other args
            command = f"grep -rn '{pattern}' --include='{include}' ."
            args = {"command": command}  # Clear all other args, only keep command
            tool_name = "run_shell"
        
        logger.info(f"Arguments for {tool_name}: {args}")

        func = mapping.get(tool_name)
        if func: return func(**args)
        return f"Unknown tool: {tool_name}. Available tools: {', '.join(mapping.keys())}"

# =============================================================================
# MAIN AGENT CONTROLLER
# =============================================================================

class NightShiftAgent:
    def __init__(self, project_dir):
        self.project_dir = Path(project_dir).resolve()
        if not self.project_dir.exists():
            raise ValueError(f"Project dir not found: {project_dir}")
        os.chdir(self.project_dir)
        load_dotenv(self.project_dir / '.env')
        
        # Set up file logging in the project directory
        self._setup_logging()
        
        self.build_state = BuildState()
        self.toolbox = Toolbox(self.build_state)
        self.llm = ProviderManager()
        self.bot_username = os.getenv("BOT_USERNAME", "agentnightshift")
        self.gh_token = os.getenv("GH_BOT_TOKEN")
    
    def _setup_logging(self):
        """Set up file handlers in the project directory."""
        log_dir = self.project_dir / ".agent_logs"
        log_dir.mkdir(exist_ok=True)
        
        log_file = log_dir / f"session_{SESSION_TIMESTAMP}.log"
        prompt_log_file = log_dir / f"prompts_{SESSION_TIMESTAMP}.log"
        
        # Add file handler to main logger
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S'))
        logger.addHandler(file_handler)
        
        # Add file handler to prompt logger
        prompt_handler = logging.FileHandler(prompt_log_file)
        prompt_handler.setFormatter(logging.Formatter('%(asctime)s\n%(message)s\n'))
        prompt_logger.addHandler(prompt_handler)
        
        logger.info(f"📁 Logging to: {log_dir}")

    def run_cmd_quiet(self, cmd):
        env = os.environ.copy()
        if self.gh_token: env["GITHUB_TOKEN"] = self.gh_token
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env)

    def configure_git(self):
        logger.info("🔧 Configuring git...")
        repo = self.run_cmd_quiet("gh repo view --json nameWithOwner --jq .nameWithOwner").stdout.strip()
        if self.gh_token and repo:
            url = f"https://{self.bot_username}:{self.gh_token}@github.com/{repo}.git"
            self.run_cmd_quiet(f'git remote set-url origin "{url}"')
            self.run_cmd_quiet(f'git config user.name "{self.bot_username}"')
            self.run_cmd_quiet(f'git config user.email "{self.bot_username}@users.noreply.github.com"')
            logger.info(f"✅ Authenticated for {repo}")

    def create_branch(self):
        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        branch = f"{BRANCH_PREFIX}/{ts}"
        self.run_cmd_quiet("git checkout main")
        self.run_cmd_quiet("git pull origin main")
        self.run_cmd_quiet(f"git checkout -b {branch}")
        logger.info(f"🌿 Created branch: {branch}")
        return branch

    def process_task(self, task, arch, files):
        self.build_state.reset()
        system_prompt = f"""You are Night Shift Agent, an autonomous coding assistant.

IMPORTANT: This is a TEXT-ONLY interface. Do NOT use native function calling or built-in tools.
You must output ALL tool calls as plain text JSON wrapped in <agent_action></agent_action> tags.
The external system will parse your text output and execute the tools for you.

PROJECT ARCHITECTURE:
{arch}

PROJECT FILES:
{files}

AVAILABLE TOOLS (output as text, do not use native function calling):

1. read_file - Read the contents of a file
   Args: {{"action": "read_file", "args": {{"path": "path/to/file.kt"}}}}
   Returns: The file contents as a string

2. write_file - Write content to a file (creates directories if needed)
   Args: {{"action": "write_file", "args": {{"path": "path/to/file.kt", "content": "file contents here"}}}}
   Returns: Success or error message

3. replace - Replace text within a file (for small edits)
   Args: {{"action": "replace", "args": {{"path": "path/to/file.kt", "old_string": "text to find", "new_string": "replacement text"}}}}
   Returns: Success or error message

4. run_shell - Execute a shell command
   Args: {{"action": "run_shell", "args": {{"command": "./gradlew assembleDebug"}}}}
   Returns: Command output (stdout + stderr)

5. list_files - List all files in a directory
   Args: {{"action": "list_files", "args": {{"path": "."}}}}
   Returns: Newline-separated list of file paths

WORKFLOW:
1. Read files to understand the current code
2. Make changes using write_file or replace
3. Run './gradlew assembleDebug :composeApp:linkDebugFrameworkIosSimulatorArm64 detekt' to verify
4. If build fails, read the error and fix your code
5. Continue until the build passes

RULES:
- NEVER use native function calling - output tool calls as plain text only
- Output ONE tool call at a time wrapped in <agent_action> tags
- Wait for tool output before making the next call
- Always verify your changes compile before considering the task complete
"""
        initial_prompt = system_prompt + f"\nTASK: {task}\n\nBegin by reading the relevant files to understand the current implementation."
        
        prompt_history = []
        consecutive_failures = 0

        for i in range(MAX_ITERATIONS):
            logger.info(f"🔄 Iteration {i+1}/{MAX_ITERATIONS}")
            
            # Context Pruning
            full_text = initial_prompt + "".join(prompt_history)
            while len(prompt_history) > 2 and len(full_text) > MAX_CONTEXT_CHARS:
                prompt_history.pop(0)
                full_text = initial_prompt + "".join(prompt_history)

            # LLM Call
            response = self.llm.ask(full_text)
            if not response: return False
            
            prompt_history.append(f"\n\nASSISTANT: {response}")
            
            # Extract Actions
            actions = re.findall(r'<agent_action>(.*?)</agent_action>', response, re.DOTALL)
            if not actions and "{" in response: # Fallback regex
                 actions = re.findall(r'\{[^{}]*\}', response) # Simplified fallback
            
            tool_run = False
            for action_str in actions:
                try:
                    clean_action = self.llm.strip_markdown_code_blocks(action_str)
                    try:
                        data = json.loads(clean_action)
                    except json.JSONDecodeError:
                        # Fallback: finding JSON object inside the string
                        json_match = re.search(r'\{.*\}', clean_action, re.DOTALL)
                        if json_match:
                            data = json.loads(json_match.group(0))
                        else:
                            raise

                    tool = data.get("action") or data.get("tool")
                    args = data.get("args", {})
                    
                    # Handle Gemini's tool_code format: {"tool_code":"func_name(arg1='val', arg2='val')"}
                    if not tool and "tool_code" in data:
                        tool_code = data["tool_code"]
                        # Parse function call syntax: func_name(arg1='value', arg2='value')
                        match = re.match(r'(\w+)\((.*)\)', tool_code, re.DOTALL)
                        if match:
                            tool = match.group(1)
                            args_str = match.group(2).strip()
                            # Parse kwargs like: pattern='virtualcard', include='*.kt'
                            if args_str:
                                for arg_match in re.finditer(r"(\w+)\s*=\s*['\"]([^'\"]*)['\"]", args_str):
                                    args[arg_match.group(1)] = arg_match.group(2)
                            logger.info(f"📝 Parsed tool_code: {tool}({args})")
                    
                    # Handle top-level args (Gemini-3-Flash-Preview behavior)
                    if not args:
                        args = {k: v for k, v in data.items() if k not in ["action", "tool", "args", "rationale", "thought", "tool_code"]}

                    # Norm args
                    if "file_path" in args: args["path"] = args.pop("file_path") 
                    
                    if tool:
                        logger.info(f"🛠️ Tool: {tool}")
                        output = self.toolbox.dispatch(tool, args)
                        prompt_history.append(f"\n\nTOOL OUTPUT ({tool}): {output}")
                        tool_run = True
                except Exception as e:
                    logger.warning(f"Failed to parse action: {e}")

            # If no tools were run and files were changed, model might think it's done
            # Trigger a verification build automatically
            if not tool_run and self.build_state.files_changed_since_success:
                logger.info("🔍 No tool calls detected. Running verification build...")
                build_output = self.toolbox.run_shell(
                    "./gradlew assembleDebug :composeApp:linkDebugFrameworkIosSimulatorArm64 detekt"
                )
                prompt_history.append(f"\n\nAUTO-VERIFICATION BUILD OUTPUT:\n{build_output}")
                
            # Build Failure Logic
            if tool_run and self.build_state.build_attempted and not self.build_state.build_passed:
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    # Only revert if we actually have a checkpoint to revert to
                    if self.build_state.last_successful_files:
                        logger.warning(f"⚠️ 5 Consecutive failures. Reverting {len(self.build_state.last_successful_files)} files to checkpoint...")
                        for p, c in self.build_state.last_successful_files.items():
                            self.toolbox.write_file(path=p, content=c)
                            logger.info(f"   ↩️ Reverted: {os.path.basename(p)}")
                        prompt_history.append("\n\nSYSTEM: Build failed 5 times. Files reverted to last working checkpoint. Try a simpler approach.")
                    else:
                        logger.warning("⚠️ 5 Consecutive failures but no checkpoint exists. Notifying model to try simpler approach.")
                        prompt_history.append("\n\nSYSTEM: Build has failed 5 consecutive times with no working checkpoint to revert to. Please try a simpler, more incremental approach.")
                    consecutive_failures = 0
            
            # Check Success (Build Passed + User Task satisfied implies we should commit)
            # Simplified: If build passed this turn, commit.
            if self.build_state.build_passed and self.build_state.is_verified():
                logger.info("✅ Build Passed. Committing...")
                # Commit & Push logic here (simplified for refactor brevity)
                subprocess.run("git add .", shell=True)
                safe_task = task.replace('"', '\\"')
                subprocess.run(f'git commit -m "Fix: {safe_task}"', shell=True)
                current_branch = subprocess.getoutput("git rev-parse --abbrev-ref HEAD")
                subprocess.run(f"git push -u origin {current_branch}", shell=True)
                subprocess.run("gh pr create --fill", shell=True)
                return True
        
        return False

    def run(self):
        logger.info(f"🚀 Starting Agent in {self.project_dir}")
        self.configure_git()
        
        tasks_file = self.project_dir / "tasks.txt"
        if not tasks_file.exists():
            return
            
        with open(tasks_file) as f: tasks = [l.strip() for l in f if l.strip() and not l.startswith("[x]")]
        
        if not tasks: return

        branch = self.create_branch()
        
        # Get project architecture - try tree, fall back to find
        arch_output = self.toolbox.run_shell("tree -L 2 -I 'build|.*' 2>/dev/null || find . -maxdepth 2 -type d ! -path '*/.*' ! -path './build*' 2>/dev/null | head -50")
        
        # Also read ARCHITECTURE.md if it exists for richer context
        arch_doc = self.project_dir / "docs" / "ARCHITECTURE.md"
        if arch_doc.exists():
            try:
                arch_content = arch_doc.read_text()
                arch_output = f"=== Directory Structure ===\n{arch_output}\n\n=== ARCHITECTURE.md ===\n{arch_content}"
            except Exception as e:
                logger.warning(f"Failed to read ARCHITECTURE.md: {e}")
        
        files = self.toolbox.list_files()

        for task in tasks:
            logger.info(f"▶️ Processing: {task}")
            # Clean task string handling
            clean_task = task.replace("[ ]", "").replace("[!]", "").strip()
            if self.process_task(clean_task, arch_output, files):
                # Mark done
                # Mark done
                try:
                    if os.path.exists(tasks_file):
                        content = open(tasks_file).read().replace(task, f"[x] {clean_task}")
                        open(tasks_file, "w").write(content)
                    else:
                        logger.warning(f"⚠️ tasks.txt not found. Creating new one with completed task.")
                        with open(tasks_file, "w") as f:
                            f.write(f"[x] {clean_task}\n")
                except Exception as e:
                    logger.error(f"❌ Failed to update tasks.txt: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-dir', default='.')
    args = parser.parse_args()
    
    agent = NightShiftAgent(args.project_dir)
    agent.run()
