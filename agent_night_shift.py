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
import urllib.request
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
# Gemini CLI models (gemini-cli-core config/models.js, v0.25.1)
# - gemini-2.5-pro
# - gemini-2.5-flash
# - gemini-2.5-flash-lite
# - gemini-3-pro-preview
# - gemini-3-flash-preview
# Aliases: auto, auto-gemini-2.5, auto-gemini-3, pro, flash, flash-lite
# Embedding: gemini-embedding-001
DEFAULT_PROVIDER = "gemini"
DEFAULT_MODEL_GEMINI = "gemini-2.5-flash"
DEFAULT_MODEL_OPENROUTER = "google/gemini-2.0-flash-exp:free"
DEFAULT_MODEL_CLAUDE = "claude-3-5-sonnet-20241022" 
DEFAULT_MODEL_OLLAMA = "deepseek-r1:32b"
OLLAMA_BASE_URL = "http://localhost:11434/api/generate" 

MAX_ITERATIONS = 80
MAX_RETRIES = 2
MAX_CI_FIX_ATTEMPTS = 5
RETRY_BASE_DELAY = 5
MAX_FILES_IN_CONTEXT = 50
MAX_CONTEXT_CHARS = 30000
MAX_TOOL_OUTPUT_CHARS = 50000  # 50KB max per tool output to prevent context explosion
REPLACE_STALL_THRESHOLD = 3
REQUIRE_BUILD_VERIFICATION = True
BRANCH_PREFIX = "nightshift"

PROTECTED_FILES = {
    "build.gradle.kts", "settings.gradle.kts", "gradle.properties", 
    "libs.versions.toml", "gradle-wrapper.properties", "tasks.txt"
}

CI_POLL_INTERVAL = 60  # Seconds
MAX_CI_WAIT_POLLS = 30 # 30 * 60s = 30 minutes

# Logger Setup - Console + file handlers
SESSION_TIMESTAMP = datetime.now().strftime('%Y%m%d_%H%M%S')

logger = logging.getLogger("NightShiftAgent")
logger.setLevel(logging.DEBUG)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S'))
logger.addHandler(console_handler)

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
    def ask(self, prompt_or_messages) -> Optional[str]:
        """Sends prompt (str) or messages (list of dicts) to the model and returns text response."""
        pass
        
    @property
    @abstractmethod
    def name(self) -> str:
        """Friendly name for logging."""
        pass
    
    def messages_to_string(self, messages: list) -> str:
        """Converts a list of message dicts to a single string for CLI providers."""
        parts = []
        for msg in messages:
            role = msg.get("role", "user").upper()
            content = msg.get("content", "")
            if role == "SYSTEM":
                parts.append(content)  # System prompt goes first, no prefix
            elif role == "USER":
                parts.append(f"\n\nUSER: {content}")
            elif role == "ASSISTANT":
                parts.append(f"\n\nASSISTANT: {content}")
        return "".join(parts).strip()
        
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

    def ask(self, prompt_or_messages) -> Optional[str]:
        # Flatten messages list to string for CLI
        if isinstance(prompt_or_messages, list):
            prompt = self.messages_to_string(prompt_or_messages)
        else:
            prompt = prompt_or_messages
            
        prompt_logger.debug(
            f"{'='*80}\n>>> PROMPT TO {self.name}\n{'='*80}\n{prompt[:2000]}\n{'='*80}\n"
        )
        
        for attempt in range(MAX_RETRIES):
            try:
                # Use positional argument for prompt (stdin/--prompt is deprecated)
                result = subprocess.run(
                    ["gemini", "--model", self.model, "--sandbox", "false", "--output-format", "json", prompt],
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

    def ask(self, prompt_or_messages) -> Optional[str]:
        # Flatten messages list to string for CLI
        if isinstance(prompt_or_messages, list):
            prompt = self.messages_to_string(prompt_or_messages)
        else:
            prompt = prompt_or_messages
            
        prompt_logger.debug(
            f"{'='*80}\n>>> PROMPT TO {self.name}\n{'='*80}\n{prompt[:2000]}\n{'='*80}\n"
        )
        
        for attempt in range(MAX_RETRIES):
            try:
                import tempfile
                with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as f:
                    f.write(prompt)
                    temp_path = f.name
                
                try:
                    # Use --print for non-interactive mode, --tools "" to disable native tools
                    # This forces text-only output using <agent_action> tags
                    model_arg = f"--model {self.model}" if self.model else ""
                    cmd = f"cat {temp_path} | claude --print {model_arg} --tools \"\""
                    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
                    
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

class OllamaProvider(LLMProvider):
    """Ollama provider with KV cache optimization.
    
    Ollama's /api/chat endpoint automatically caches previous tokens in the KV cache
    as long as the model stays loaded. By setting keep_alive=-1, we keep the model
    loaded indefinitely, so the system prompt is only fully processed on the first call.
    Subsequent calls only process the new/delta tokens.
    """
    def __init__(self, model=DEFAULT_MODEL_OLLAMA):
        self.model = model
        self.base_url = "http://localhost:11434/api/chat"  # Chat API endpoint
        self.keep_alive = -1  # Keep model loaded indefinitely for KV cache persistence

    @property
    def name(self): return f"Ollama ({self.model})"

    def ask(self, prompt_or_messages) -> Optional[str]:
        # Convert string prompt to messages list if needed
        if isinstance(prompt_or_messages, str):
            messages = [{"role": "user", "content": prompt_or_messages}]
        else:
            messages = prompt_or_messages
        
        prompt_logger.debug(
            f"{'='*80}\n>>> PROMPT TO {self.name}\n{'='*80}\n{json.dumps(messages, indent=2)[:2000]}\n{'='*80}\n"
        )
        
        for attempt in range(MAX_RETRIES):
            try:
                # Prepare JSON payload for Ollama /api/chat
                # keep_alive=-1 keeps the model loaded indefinitely, preserving KV cache
                # This means the system prompt is only fully tokenized on first call;
                # subsequent calls reuse cached KV states for the prefix
                data = {
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "keep_alive": self.keep_alive,
                    "options": {
                        "temperature": 0.6,   # R1 models need non-zero temp
                        "num_ctx": 32768      # Large context for DeepSeek-R1
                    }
                }
                
                req = urllib.request.Request(
                    self.base_url, 
                    data=json.dumps(data).encode('utf-8'),
                    headers={'Content-Type': 'application/json'}
                )
                
                with urllib.request.urlopen(req, timeout=900) as response:
                    resp_body = response.read().decode('utf-8')
                    result = json.loads(resp_body)
                    
                    self._log_raw_response(attempt, resp_body)
                    
                    # Chat API returns message.content
                    raw_text = result.get("message", {}).get("content", "")
                    
                    # Strip <think>...</think> blocks from reasoning models
                    raw_text = re.sub(r'<think>.*?</think>', '', raw_text, flags=re.DOTALL)
                    
                    parsed = self.strip_markdown_code_blocks(raw_text.strip())
                    
                    prompt_logger.debug(f"{'='*80}\n<<< PARSED RESPONSE\n{'='*80}\n{parsed}\n{'='*80}\n")
                    return parsed

            except Exception as e:
                logger.warning(f"⚠️ {self.name} Exception ({attempt+1}/{MAX_RETRIES}): {e}")
                self._backoff(attempt)
        
        return None

    def _log_raw_response(self, attempt, body):
         prompt_logger.debug(
            f"{'='*80}\n<<< RAW RESPONSE ({attempt+1})\n{'='*80}\n"
            f"{body[:1000]}... (truncated)\n{'='*80}\n"
        )

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

    def ask(self, prompt_or_messages) -> Optional[str]:
        api_key = os.getenv("OPENROUTER_API_KEY")
        model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL_OPENROUTER)
        
        if not api_key:
            logger.info(f"⏭️ Skipping {self.name}: OPENROUTER_API_KEY not set")
            raise QuotaExceededError("OpenRouter Key Missing") # Treat as unavailable

        import urllib.request
        import json

        # Convert string to messages list if needed
        if isinstance(prompt_or_messages, list):
            messages = prompt_or_messages
        else:
            messages = [{"role": "user", "content": prompt_or_messages}]

        prompt_logger.debug(
            f"{'='*80}\n>>> PROMPT TO {self.name}\n{'='*80}\n{json.dumps(messages, indent=2)[:2000]}\n{'='*80}\n"
        )
        
        # We need generic requests, but trying to avoid external deps if possible.
        # But for OpenRouter, curl/requests is needed. Let's use standard lib urllib to avoid forcing 'requests' install.
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/chrishonson/night-shift-agent",
        }
        data = {
            "model": model,
            "messages": messages
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
        gemini_model = os.getenv("PREFERRED_AGENT_MODEL", DEFAULT_MODEL_GEMINI)
        ollama_model = os.getenv("OLLAMA_MODEL", DEFAULT_MODEL_OLLAMA)
        
        # Define all providers
        ollama_p = OllamaProvider(model=ollama_model) # Primary: Local & Free
        gemini_p = GeminiCLIProvider(model=gemini_model)
        claude_p = ClaudeCLIProvider() # Default claude model
        openrouter_p = OpenRouterAPIProvider() # Fallback to openrouter
        
        # User preferred order: Gemini -> Claude -> OpenRouter -> Ollama
        self.providers = [gemini_p, claude_p, openrouter_p, ollama_p]
            
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

    def exec_command(self, command: str, env: dict = None, timeout: int = 600) -> subprocess.CompletedProcess:
        """Centralized helper for safe subprocess execution."""
        if env is None: env = os.environ.copy()
        try:
            return subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                stdin=subprocess.DEVNULL
            )
        except Exception as e:
            # Create a fake failed result if execution blows up (e.g. OOM)
            logger.error(f"FATAL subprocess error: {e}")
            raise e

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
            result = self.exec_command(command, env=env)
            
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

    def _parse_and_contextualize_errors(self, output: str) -> str:
        """Parses compiler errors, reads source files, and returns a focused error report."""
        # Regex patterns for common Kotlin/Gradle errors
        # 1. e: file:///path/to/File.kt:10:20: error: message
        # 2. e: /path/to/File.kt: (10, 20): message
        error_patterns = [
            r"(?P<level>[ew]):\s+(?P<path>file://[^:]+|/[^:]+):\s*\(?(?P<line>\d+)[:,\s]+(?P<col>\d+)\)?:\s*(?P<msg>.*)",
            r"(?P<path>[^:\n]+\.kt):\s*(?P<line>\d+):\s*(?P<col>\d+):\s*error:\s*(?P<msg>.*)"
        ]
        
        extracted_errors = []
        files_to_read = set()
        
        for line in output.split('\n'):
            for pattern in error_patterns:
                match = re.search(pattern, line)
                if match:
                    path_str = match.group("path").strip()
                    # Fix file:// prefix
                    if path_str.startswith("file://"):
                        path_str = path_str[7:]
                    
                    # Normalize absolute paths
                    if os.path.exists(path_str):
                         files_to_read.add(os.path.abspath(path_str))
                    
                    extracted_errors.append(line)
                    break # Single match per line
                    
        if not extracted_errors:
            # Fallback output if no specific errors parsed
             if len(output) > MAX_TOOL_OUTPUT_CHARS:
                half = MAX_TOOL_OUTPUT_CHARS // 2
                return output[:half] + "\n\n... [TRUNCATED] ...\n\n" + output[-half:]
             return output

        # Read files
        file_contexts = []
        for file_path in files_to_read:
             try:
                 with open(file_path, 'r') as f:
                     content = f.read()
                     file_contexts.append(f"--- FILE: {file_path} ---\n{content}\n")
             except Exception as e:
                 file_contexts.append(f"--- FILE: {file_path} ---\n[Error reading file: {e}]\n")
        
        # Assemble report
        report_parts = [
            "❌ BUILD FAILED - ERROR REPORT",
            "\n=== EXTRACTED ERRORS ==="
        ]
        report_parts.extend(extracted_errors)
        
        report_parts.append("\n=== SOURCE FILES CONTEXT ===")
        report_parts.extend(file_contexts)
        
        report_parts.append("\n=== RAW OUTPUT TAIL (Last 2000 chars) ===")
        report_parts.append(output[-2000:])
        
        return "\n".join(report_parts)

    def run_tests(self) -> str:
        """Run tests only (for TDD red/green phases). Does NOT count as final verification."""
        TEST_CMD = "./gradlew testDebugUnitTest"
        
        logger.info(f"🧪 Running tests: {TEST_CMD}")
        
        env = os.environ.copy()
        try:
            result = self.exec_command(TEST_CMD, env=env, timeout=300)
            output = result.stdout + result.stderr
            logger.debug(f"Full Test Output:\n{output}")
            
            if result.returncode == 0:
                logger.info("✅ Tests PASSED")
                if len(output) > MAX_TOOL_OUTPUT_CHARS:
                    half = MAX_TOOL_OUTPUT_CHARS // 2
                    output = output[:half] + "\n\n... [TRUNCATED] ...\n\n" + output[-half:]
                return f"✅ TESTS PASSED\n\n{output}"
            else:
                logger.info("🔴 Tests FAILED (expected in TDD red phase)")
                return f"🔴 TESTS FAILED (exit {result.returncode}):\n\n{self._parse_and_contextualize_errors(output)}"
        except Exception as e:
            logger.error(f"❌ Test error: {e}")
            return f"Error running tests: {e}"

    def verify_build(self) -> str:
        """Run the official verification build with coverage. This is the ONLY way to pass verification."""
        VERIFICATION_CMD = "./gradlew assembleDebug :composeApp:linkDebugFrameworkIosSimulatorArm64 detekt testDebugUnitTest koverVerify"
        
        logger.info(f"🔍 Running verification build: {VERIFICATION_CMD}")
        
        env = os.environ.copy()
        try:
            result = self.exec_command(VERIFICATION_CMD, env=env)
            
            self.build_state.build_attempted = True
            self.build_state.build_passed = (result.returncode == 0)
            
            output = result.stdout + result.stderr
            logger.debug(f"Full Verification Output:\n{output}")
            
            if result.returncode == 0:
                # Success - truncate if needed
                if len(output) > MAX_TOOL_OUTPUT_CHARS:
                    output = output[-MAX_TOOL_OUTPUT_CHARS:]
                logger.info("✅ Verification PASSED")
                if self.build_state.files_changed_since_success:
                    self.build_state.checkpoint(self.build_state.files_changed_since_success)
                return f"✅ VERIFICATION PASSED (build + tests + coverage)\n\n{output}"
            else:
                # Failure - use smart parsing
                logger.warning(f"❌ Verification FAILED (Exit {result.returncode})")
                return f"❌ VERIFICATION FAILED (exit {result.returncode}):\n\n{self._parse_and_contextualize_errors(output)}"
        except Exception as e:
            logger.error(f"❌ Verification error: {e}")
            return f"Error running verification: {e}"

    def dispatch(self, tool_name, args):
        mapping = {
            "read_file": self.read_file, "write_file": self.write_file,
            "run_shell": self.run_shell, "run_shell_command": self.run_shell,
            "replace": self.replace, "list_files": self.list_files,
            "run_tests": self.run_tests, "test": self.run_tests,  # TDD tool
            "verify_build": self.verify_build, "verify": self.verify_build  # Alias
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
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)

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
        # Check current branch
        current = self.run_cmd_quiet("git branch --show-current").stdout.strip()
        if current.startswith(BRANCH_PREFIX + "/"):
            logger.info(f"🌿 Reusing existing branch: {current}")
            return current

        ts = datetime.now().strftime('%Y%m%d-%H%M%S')
        branch = f"{BRANCH_PREFIX}/{ts}"
        self.run_cmd_quiet("git checkout main")
        self.run_cmd_quiet("git pull origin main")
        self.run_cmd_quiet(f"git checkout -b {branch}")
        logger.info(f"🌿 Created branch: {branch}")
        return branch

    def commit_changes(self, task: str):
        logger.info("📝 Committing changes...")
        self.toolbox.run_shell("git add .")
        
        # Escape quotes in task name
        safe_task = task.replace('"', '\\"')
        commit_msg = f"Night Shift: {safe_task}"
        
        # Check if anything to commit
        status = self.toolbox.run_shell("git status --porcelain")
        if not status.strip():
            logger.warning("⚠️ No changes to commit")
            return False
            
        result = self.toolbox.run_shell(f'git commit -m "{commit_msg}"')
        if "Command failed" in result:
             logger.error(f"❌ Failed to commit: {result}")
             return False
             
        logger.info("✅ Changes committed")
        return True

    def push_and_create_pr(self, completed: list, branch: str):
        logger.info(f"🚀 Pushing branch {branch}...")
        self.toolbox.run_shell(f"git push -u origin {branch}")
        
        # Build nice PR title and body
        tasks_succeeded = len(completed)
        pr_title = f"🌙 Night Shift: {tasks_succeeded} task(s)"
        task_list = "\n".join([f"- [x] {t}" for t in completed])
        pr_body = f"## 🌙 Night Shift Agent Report\\n\\n**Tasks**: {tasks_succeeded}\\n\\n{task_list}"
        
        # Create PR
        logger.info("Compare URL: " + self.toolbox.run_shell(f'gh pr create --title "{pr_title}" --body "{pr_body}" --head {branch} --base main'))
        # We don't fail if PR usage exists (might be updating existing branch)

    def monitor_pr(self, branch: str):
        logger.info("\n" + "="*60)
        logger.info(f"🔍 Monitoring CI Status for branch: {branch}")
        logger.info("="*60)
        
        ci_passed = False
        
        for poll in range(MAX_CI_WAIT_POLLS):
            logger.info(f"⏳ Poll {poll + 1}/{MAX_CI_WAIT_POLLS}: Waiting {CI_POLL_INTERVAL}s for CI...")
            time.sleep(CI_POLL_INTERVAL)
            
            # Check PR status via GH CLI
            # We look for the statusCheckRollup to see if checks are passing/failing
            cmd = f"gh pr view {branch} --json statusCheckRollup,state,mergeable --jq '{{state: .state, checks: .statusCheckRollup, mergeable: .mergeable}}'"
            try:
                output = self.toolbox.run_shell(cmd)
                # Parse the JSON output from the command string which might contain extra text if run_shell is verbose
                # But run_shell returns stdout+stderr. We need to be careful.
                # Let's assume the JSON is the only thing or we can grep it.
                # Actually, run_shell captures output.
                
                # Clean up if there's logging noise? agent logging goes to stderr/file.
                
                # Check for "Command failed"
                if "Command failed" in output:
                    logger.warning("⚠️ Failed to check PR status (API error?)")
                    continue

                # The output might be plain JSON.
                status_data = json.loads(output.strip())
                
                state = status_data.get("state")
                if state == "MERGED":
                    logger.info("🎉 PR Merged! Stopping monitor.")
                    ci_passed = True
                    break
                
                # Check Checks
                checks = status_data.get("checks", [])
                if not checks:
                    logger.info("⏳ No checks reported yet...")
                    continue
                
                # Aggregate status
                # GitHub returns a complex structure. Simplified check:
                # We can also check `gh run list --branch {branch}` usually easier for "Any Failure"
                
            except Exception as e:
                logger.warning(f"⚠️ Error parsing PR status: {e}")
                
            # Alternative: Check Runs directly which is more reliable for "Did it fail?"
            run_cmd = f"gh run list --branch {branch} --limit 1 --json status,conclusion,databaseId --jq '.[0]'"
            run_out = self.toolbox.run_shell(run_cmd)
            if "Command failed" not in run_out and run_out.strip() != "null":
                try:
                    run_data = json.loads(run_out)
                    status = run_data.get("status")         # queued, in_progress, completed
                    conclusion = run_data.get("conclusion") # success, failure, cancelled
                    run_id = run_data.get("databaseId")
                    
                    logger.info(f"   Run ID: {run_id} | Status: {status} | Conclusion: {conclusion}")
                    
                    if status == "completed":
                        if conclusion == "success":
                            logger.info("🎉 CI Checks Passed!")
                            ci_passed = True
                            # Auto-merge?
                            # self.toolbox.run_shell(f"gh pr merge {branch} --auto --squash")
                            break
                        elif conclusion in ["failure", "timed_out"]:
                            logger.error(f"❌ CI Failed (Run {run_id}). Initiating Auto-Fix...")
                            if self.attempt_fix(branch, run_id):
                                logger.info("✅ Auto-Fix applied and pushed. Resetting monitor...")
                                # Reset loop or continue? 
                                # We should typically break inner loop or reset counter, 
                                # but for now let's just use the remaining polls.
                                continue 
                            else:
                                logger.error("❌ Auto-Fix failed. Stopping.")
                                break
                except: pass

        if ci_passed:
            logger.info("✅ Monitoring complete: CI Passed.")
        else:
            logger.warning("⚠️ Monitoring ended: CI did not pass or timed out.")

    def attempt_fix(self, branch: str, run_id: str) -> bool:
        logger.info(f"🚑 Attempting Auto-Fix for Run {run_id}...")
        
        # 1. Fetch Logs
        log_cmd = f"gh run view {run_id} --log-failed" 
        logs = self.toolbox.run_shell(log_cmd)
        
        if len(logs) > 5000:
            logs = logs[-5000:] # Truncate to last 5k chars of error
            
        fix_prompt = f"The CI build failed for branch {branch}. Here are the logs:\n\n{logs}\n\nPlease analyze the error and fix the code. Use 'read_file' to examine files if needed, and 'write_file' or 'replace' to fix the issue. Run verification build before finishing."
        
        # Reuse process_task logic but with new prompt foundation
        # We treat "Fix CI Failure" as a task
        arch_output = self.toolbox.run_shell("tree -L 2 -I 'build|.*' 2>/dev/null || find . -maxdepth 2 -type d ! -path '*/.*' ! -path './build*' 2>/dev/null | head -50")

        arch_doc = self.project_dir / "docs" / "ARCHITECTURE.md"
        if arch_doc.exists():
            try:
                arch_content = arch_doc.read_text()
                arch_output = f"=== Directory Structure ===\n{arch_output}\n\n=== ARCHITECTURE.md ===\n{arch_content}"
            except Exception as e:
                logger.warning(f"Failed to read ARCHITECTURE.md: {e}")

        files = self.toolbox.list_files()
        if self.process_task(fix_prompt, arch_output, files):
            # Commit and push the fix
            if self.commit_changes("Fix CI Failure"):
                self.toolbox.run_shell(f"git push origin {branch}")
                logger.info("📤 CI fix pushed to remote.")
                return True
            else:
                logger.warning("⚠️ No changes to commit after fix attempt.")
                return False
        return False

    def process_task(self, task, context, files):
        self.build_state.reset()
        system_prompt = f"""You are Night Shift Agent, an autonomous coding assistant that follows Test-Driven Development (TDD).

IMPORTANT: This is a TEXT-ONLY interface. Do NOT use native function calling or built-in tools.
You must output ALL tool calls as plain text JSON wrapped in <agent_action></agent_action> tags.
The external system will parse your text output and execute the tools for you. Utilize your <think> block to plan the TDD (if applicable).

PROJECT ARCHITECTURE:
{context}

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

4. run_shell - Execute a shell command (for exploration/debugging ONLY)
   Args: {{"action": "run_shell", "args": {{"command": "ls -la"}}}}
   Returns: Command output (stdout + stderr)
   NOTE: This does NOT count as verification.

5. list_files - List all files in a directory
   Args: {{"action": "list_files", "args": {{"path": "."}}}}
   Returns: Newline-separated list of file paths

6. run_tests - Run unit tests only (for TDD red/green phases)
   Args: {{"action": "run_tests", "args": {{}}}}
   Returns: TESTS PASSED or TESTS FAILED with test output
   NOTE: Use this during TDD cycles. Does NOT count as final verification.

7. verify_build - Run full verification (build + tests + coverage) - REQUIRED before task completion
   Args: {{"action": "verify_build", "args": {{}}}}
   Returns: VERIFICATION PASSED or VERIFICATION FAILED with build output
   NOTE: This runs assembleDebug, iOS framework build, detekt, tests, AND koverVerify.
         The task is NOT complete until this passes.

=== TDD WORKFLOW (MANDATORY) ===

You MUST follow Test-Driven Development for every task:

🔴 RED PHASE:
1. Read existing code and tests to understand the codebase
2. Write a failing test FIRST that defines the expected behavior
3. Call run_tests to confirm the test FAILS (this is expected and correct!)
   - If tests pass, your test isn't testing new behavior - make it more specific

🟢 GREEN PHASE:
4. Write the minimum implementation code to make the test pass
5. Call run_tests to confirm tests now PASS
   - If tests fail, fix your implementation (not the test!)

🔵 REFACTOR PHASE:
6. Clean up the code while keeping tests passing
7. Call run_tests after any refactoring to ensure nothing broke

✅ FINAL VERIFICATION:
8. Call verify_build to run the full verification suite (build + tests + coverage)
9. The task is complete ONLY when verify_build returns VERIFICATION PASSED

RULES:
- NEVER skip the RED phase - always write tests BEFORE implementation
- NEVER use native function calling - output tool calls as plain text only
- Output ONE tool call at a time wrapped in <agent_action> tags
- Wait for tool output before making the next call
- Tests go in: composeApp/src/commonTest/kotlin/... (mirror the main source structure)
- You MUST call verify_build before considering the task complete
- Coverage thresholds are enforced by koverVerify - ensure adequate test coverage
"""
        task_intro = f"TASK: {task}\n\nBegin by reading the relevant files to understand the current implementation."
        
        # Initialize messages list with system prompt and task
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task_intro}
        ]
        consecutive_failures = 0
        replace_repeat_counts = {}

        last_provider_index = self.llm.current_index
        i = 0
        while i < MAX_ITERATIONS:
            logger.info(f"🔄 Iteration {i+1}/{MAX_ITERATIONS}")
            
            # Check for silent provider switch (e.g. QuotaExceeded inside ask())
            # and reset iteration count if it happened to give new model a chance
            if self.llm.current_index != last_provider_index:
                logger.info(f"🔄 Provider switched! Resetting iteration count to 1/{MAX_ITERATIONS}")
                i = 0
                last_provider_index = self.llm.current_index
            
            # Context Pruning: preserve system (0) and task (1), prune middle pairs
            total_chars = sum(len(m.get("content", "")) for m in messages)
            while len(messages) > 4 and total_chars > MAX_CONTEXT_CHARS:
                # Remove oldest user/assistant pair after the task (indices 2, 3)
                messages.pop(2)
                messages.pop(2)  # Was index 3, now 2 after first pop
                total_chars = sum(len(m.get("content", "")) for m in messages)
                logger.info(f"🧹 Pruned context: {len(messages)} messages, {total_chars} chars")

            # LLM Call with messages list
            response = self.llm.ask(messages)
            if not response: return False
            
            messages.append({"role": "assistant", "content": response})
            
            # Extract Actions
            actions = re.findall(r'<agent_action>(.*?)</agent_action>', response, re.DOTALL)
            if not actions and "{" in response:
                # Fallback: Robustly find JSON objects using decoder to handle nesting
                try:
                    decoder = json.JSONDecoder()
                    pos = 0
                    while pos < len(response):
                        match = re.search(r'\{', response[pos:])
                        if not match: break
                        start = pos + match.start()
                        try:
                            obj, end = decoder.raw_decode(response, start)
                            # Verify likely action object
                            if isinstance(obj, dict) and any(k in obj for k in ("action", "tool", "tool_code")):
                                actions.append(json.dumps(obj))
                            pos = end
                        except json.JSONDecodeError:
                            pos = start + 1
                except Exception as e:
                    logger.warning(f"⚠️ JSON Fallback extraction error: {e}")
            
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
                        messages.append({"role": "user", "content": f"TOOL OUTPUT ({tool}): {output}"})

                        if tool == "replace":
                            target_path = args.get("path")
                            key_payload = {
                                "tool": tool,
                                "path": target_path,
                                "old_string": args.get("old_string"),
                                "new_string": args.get("new_string")
                            }
                            key = json.dumps(key_payload, sort_keys=True)

                            output_lower = output.lower()
                            if "text not found" in output_lower or "already present" in output_lower:
                                replace_repeat_counts[key] = replace_repeat_counts.get(key, 0) + 1
                                if target_path and replace_repeat_counts[key] >= REPLACE_STALL_THRESHOLD:
                                    file_output = self.toolbox.dispatch("read_file", {"path": target_path})
                                    if len(file_output) > MAX_TOOL_OUTPUT_CHARS:
                                        file_output = file_output[:MAX_TOOL_OUTPUT_CHARS] + "\n\n[OUTPUT TRUNCATED]"
                                    messages.append({"role": "user", "content": f"TOOL OUTPUT (read_file): {file_output}"})
                                    messages.append({"role": "user", "content": f"SYSTEM: Repeated replace failed {replace_repeat_counts[key]} times for {target_path}. Re-read the file and choose a different approach."})
                                    replace_repeat_counts[key] = 0
                            else:
                                replace_repeat_counts.pop(key, None)

                        tool_run = True
                except Exception as e:
                    logger.warning(f"Failed to parse action: {e}")

            # If no tools were run and files were changed, model might think it's done
            # Trigger verification automatically
            if not tool_run and self.build_state.files_changed_since_success:
                logger.info("🔍 No tool calls detected. Running auto-verification...")
                build_output = self.toolbox.verify_build()
                messages.append({"role": "user", "content": f"AUTO-VERIFICATION OUTPUT:\n{build_output}"})
                
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
                        messages.append({"role": "user", "content": "SYSTEM: Build failed 5 times. Files reverted to last working checkpoint. Try a simpler approach."})
                    else:
                        logger.warning("⚠️ 5 Consecutive failures but no checkpoint exists. Notifying model to try simpler approach.")
                        messages.append({"role": "user", "content": "SYSTEM: Build has failed 5 consecutive times with no working checkpoint to revert to. Please try a simpler, more incremental approach."})
                    consecutive_failures = 0
            
            # Check Success (Build Passed + User Task satisfied implies we should commit)
            if self.build_state.build_passed and self.build_state.is_verified():
                logger.info("✅ Build Passed. Task Complete.")
                return True
            
            i += 1
            time.sleep(2)
        
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

        completed_tasks = []
        for task in tasks:
            logger.info(f"▶️ Processing: {task}")
            # Clean task string handling
            clean_task = task.replace("[ ]", "").replace("[!]", "").strip()
            if self.process_task(clean_task, arch_output, files):
                # Mark task complete in tasks.txt BEFORE commit (so it's included)
                try:
                    tasks_file = self.project_dir / "tasks.txt"
                    if os.path.exists(tasks_file):
                        content = open(tasks_file).read().replace(task, f"[x] {clean_task}")
                        open(tasks_file, "w").write(content)
                        logger.info(f"✅ Marked task complete in tasks.txt: {clean_task}")
                    else:
                        logger.warning(f"⚠️ tasks.txt not found. Creating new one with completed task.")
                        with open(tasks_file, "w") as f:
                            f.write(f"[x] {clean_task}\n")
                except Exception as e:
                    logger.error(f"❌ Failed to update tasks.txt: {e}")

                # Commit this task's changes (including tasks.txt update)
                if self.commit_changes(clean_task):
                    completed_tasks.append(clean_task)

        # Push and create PR only AFTER all tasks are done
        if completed_tasks:
            self.push_and_create_pr(completed_tasks, branch)
            self.monitor_pr(branch)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-dir', default='.')
    args = parser.parse_args()
    
    agent = NightShiftAgent(args.project_dir)
    agent.run()
