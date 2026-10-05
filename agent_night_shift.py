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
import urllib.error
import logging
import argparse
import shlex
import difflib
import random
import threading
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any, Union
from dotenv import load_dotenv
from context_capture import ContextCapture, capture_call

# =============================================================================
# CONFIGURATION & CONSTANTS
# =============================================================================

load_dotenv()

# Control Plane defaults
DEFAULT_CONTROL_PLANE_URL = "https://us-central1-my-brain-88870.cloudfunctions.net/controlPlaneMcp"
DEFAULT_IDENTITY_ID = "night-shift-01"
DEFAULT_LANE = None  # Deprecated: control plane no longer uses lanes

# Secret Manager
DEFAULT_GCP_PROJECT = "my-brain-88870"
DEFAULT_GH_TOKEN_SECRET = "gh-bot-token-agentnightshift"
CONTROL_PLANE_POLL_INTERVAL_BASE = 45.0  # seconds
CONTROL_PLANE_HEARTBEAT_INTERVAL = 60.0  # seconds

# Defaults
# Gemini CLI models (gemini-cli-core config/models.js, v0.25.1)
# - gemini-2.5-pro
# - gemini-2.5-flash
# - gemini-2.5-flash-lite
# - gemini-3-pro-preview
# - gemini-3-flash-preview
# Aliases: auto, auto-gemini-2.5, auto-gemini-3, pro, flash, flash-lite
# Embedding: gemini-embedding-001
# How the agent reaches a model. This is the axis that decides auth, cost and
# failure mode, so every provider declares one.
#   headless  a coding CLI driven in print mode, paid by subscription
#   local     inference on this machine, paid in electricity
#   cloud     an HTTP API billed per token. Built, deliberately not wired.
KIND_HEADLESS = "headless"
KIND_LOCAL = "local"
KIND_CLOUD = "cloud"

# The headless providers the default chain walks, in order. Every headless
# provider below is selectable by name via FORCE_PROVIDER, but only these are
# reached without being asked for.
HEADLESS_ORDER = ["antigravity"]
DEFAULT_MODEL_GEMINI = "gemini-3-flash-preview"
DEFAULT_MODEL_OPENROUTER = "google/gemini-2.0-flash-exp:free"
DEFAULT_MODEL_CLAUDE = "claude-sonnet-5"
# -low could not complete a Compose UI card: it read files for 80 iterations
# without writing the screen. -high completes the same card.
DEFAULT_MODEL_ANTIGRAVITY = "gemini-3.8-flash-high"
# Seconds agy may spend on one print. Large repos produce large prompts and
# 300s was not enough: three calls on the control-plane repo hit it.
ANTIGRAVITY_PRINT_TIMEOUT_S = 900
DEFAULT_MODEL_OLLAMA = "deepseek-r1:32b"
OLLAMA_BASE_URL = "http://localhost:11434/api/generate" 

MAX_TASK_DRAFT_ATTEMPTS = 3
MAX_ITERATIONS = 80
MAX_RETRIES = 2
MAX_CI_FIX_ATTEMPTS = 5
RETRY_BASE_DELAY = 5
MAX_FILES_IN_CONTEXT = 50
MAX_CONTEXT_CHARS = 80000
MAX_TOOL_OUTPUT_CHARS = 50000  # 50KB max per tool output to prevent context explosion
REPLACE_STALL_THRESHOLD = 3
REQUIRE_BUILD_VERIFICATION = True
BRANCH_PREFIX = "nightshift"

# Card #17: Threshold for aborting silent explore-forever runs without writes
# Empirical justification: Successful cards completed in 30, 66, 29, and 29 iterations,
# all writing well before iteration 25. Dead exploration runs burned 80 iterations
# with zero writes and zero verify_build calls (accounting for 160 of 405 iterations, or 40%).
# A threshold of 25 provides ample margin (>15-20 read/shell calls) for legitimate exploration
# before writing, while aborting unrecoverable dead loops early to conserve budget.
DEFAULT_STALL_WITHOUT_WRITE_THRESHOLD = 25
STALL_WITHOUT_WRITE_THRESHOLD = int(
    os.getenv("STALL_WITHOUT_WRITE_THRESHOLD", str(DEFAULT_STALL_WITHOUT_WRITE_THRESHOLD))
)
DEFAULT_ITERATION_DELAY = float(os.getenv("NIGHT_SHIFT_ITERATION_DELAY", "2.0"))

PROTECTED_FILES = {
    "build.gradle.kts", "settings.gradle.kts", "gradle.properties", 
    "libs.versions.toml", "gradle-wrapper.properties", "tasks.txt"
}

# Branches the agent may never push to. It holds a bot token with write access
# and run_shell executes whatever the model emits, so this is the enforcement
# point wherever server-side branch protection is unavailable (a private repo
# on a free GitHub plan cannot have it at all).
PROTECTED_BRANCHES = {"main", "master"}

_SHELL_SEPARATORS = re.compile(r"&&|\|\||;|\n|(?<!\|)\|(?!\|)")


def _push_destination(refspec: str) -> str:
    """The branch a refspec writes to. `HEAD:main` and `+main` both mean main."""
    dest = refspec.split(":")[-1].lstrip("+")
    if dest.startswith("refs/heads/"):
        dest = dest[len("refs/heads/"):]
    return dest


def blocked_push_target(command: str, current_branch=None):
    """Return the protected branch this command would push to, else None.

    A bare `git push` takes its destination from the branch that is checked
    out, so the guard has to ask rather than read it off the command line.
    """
    for segment in _SHELL_SEPARATORS.split(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        # Drop leading VAR=value assignments, then `git` and any global options
        # such as `git -C <dir> push`.
        while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
            tokens = tokens[1:]
        if not tokens or os.path.basename(tokens[0]) != "git":
            continue
        rest = tokens[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]
        if not rest or rest[0] != "push":
            continue

        args = rest[1:]
        if any(a in ("--all", "--mirror") for a in args):
            return sorted(PROTECTED_BRANCHES)[0]

        positionals = []
        skip_next = False
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a.startswith("-"):
                skip_next = a in ("--repo", "-o", "--push-option", "--exec", "--receive-pack")
                continue
            positionals.append(a)

        refspecs = positionals[1:]
        if not refspecs:
            branch = current_branch() if current_branch else None
            if branch in PROTECTED_BRANCHES:
                return branch
            continue
        for refspec in refspecs:
            dest = _push_destination(refspec)
            if dest in PROTECTED_BRANCHES:
                return dest
    return None

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

    kind: str = KIND_HEADLESS
    
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

class AntigravityCLIProvider(LLMProvider):
    """Google's Antigravity CLI (`agy`) in headless print mode.

    Two quirks drive the shape of this class:

    `--print` takes the prompt as its own value, so it must be attached to the
    flag. Piping on stdin makes agy print its help instead of answering.

    Headless mode auto-denies any tool the model reaches for and then returns
    no answer at all. That is what we want, since this agent drives its own
    tools, but it means an attempted tool call yields empty output rather than
    an error. That case is detected and reported as a failed attempt so the
    chain falls through to local rather than looping on nothing.
    """

    kind = KIND_HEADLESS

    # agy prints this to stdout when headless permission denial ate the turn.
    _TOOL_DENIED_MARKER = "no output produced"

    def __init__(self, model=DEFAULT_MODEL_ANTIGRAVITY):
        super().__init__()
        self.model = model

    @property
    def name(self): return f"Antigravity CLI ({self.model})"

    def ask(self, prompt_or_messages) -> Optional[str]:
        if isinstance(prompt_or_messages, list):
            prompt = self.messages_to_string(prompt_or_messages)
        else:
            prompt = prompt_or_messages

        prompt_logger.debug(
            f"{'='*80}\n>>> PROMPT TO {self.name}\n{'='*80}\n{prompt}\n{'='*80}\n"
        )

        for attempt in range(MAX_RETRIES):
            try:
                # No shell, so the prompt needs no quoting. Prompts run ~40KB
                # against a 1MB ARG_MAX, which is ample headroom.
                # agy's own --print-timeout defaults to 5m and our subprocess
                # timeout was also 300s, so the two raced and a slow answer was
                # killed here rather than returned. Give agy the shorter budget
                # of the two so it fails in a way we can read.
                result = subprocess.run(
                    ["agy", f"--model={self.model}",
                     f"--print-timeout={ANTIGRAVITY_PRINT_TIMEOUT_S}s",
                     f"--print={prompt}"],
                    capture_output=True, text=True,
                    timeout=ANTIGRAVITY_PRINT_TIMEOUT_S + 60,
                    stdin=subprocess.DEVNULL
                )

                self._log_raw_response(attempt, result)
                self._check_quota(result.stdout + result.stderr)

                if result.returncode != 0:
                    logger.warning(f"⚠️ {self.name} Error ({attempt+1}/{MAX_RETRIES}): {result.stderr}")
                    self._backoff(attempt)
                    continue

                out = result.stdout.strip()
                if self._TOOL_DENIED_MARKER in out.lower():
                    logger.warning(
                        f"⚠️ {self.name} tried to call a tool, which headless mode denied, "
                        f"so it returned no answer ({attempt+1}/{MAX_RETRIES})."
                    )
                    self._backoff(attempt)
                    continue

                if out:
                    return self.strip_markdown_code_blocks(out)

                logger.warning(f"⚠️ {self.name} returned nothing ({attempt+1}/{MAX_RETRIES}).")
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

    def _check_quota(self, combined_output):
        lower = combined_output.lower()
        if any(x in lower for x in ["quota exceeded", "resource exhausted", "rate limit"]):
            logger.error(f"🚨 {self.name} Quota Exceeded!")
            raise QuotaExceededError(f"{self.name} Quota Exceeded")
        if "429" in lower and ("error" in lower or "too many" in lower):
            logger.error(f"🚨 {self.name} Rate Limited (429)!")
            raise QuotaExceededError(f"{self.name} Rate Limited")

    def _backoff(self, attempt):
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))

class GeminiCLIProvider(LLMProvider):
    kind = KIND_HEADLESS

    def __init__(self, model=DEFAULT_MODEL_GEMINI):
        super().__init__()
        self.model = model
        self._last_prompt = ""
        
    @property
    def name(self): return f"Gemini CLI ({self.model})"

    def ask(self, prompt_or_messages) -> Optional[str]:
        # Flatten messages list to string for CLI
        if isinstance(prompt_or_messages, list):
            prompt = self.messages_to_string(prompt_or_messages)
        else:
            prompt = prompt_or_messages
        self._last_prompt = prompt
            
        prompt_logger.debug(
            f"{'='*80}\n>>> PROMPT TO {self.name}\n{'='*80}\n{prompt}\n{'='*80}\n"
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
    kind = KIND_HEADLESS

    def __init__(self, model=""):
        super().__init__()
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
            f"{'='*80}\n>>> PROMPT TO {self.name}\n{'='*80}\n{prompt}\n{'='*80}\n"
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
    kind = KIND_LOCAL

    """Ollama provider with KV cache optimization.
    
    Ollama's /api/chat endpoint automatically caches previous tokens in the KV cache
    as long as the model stays loaded. By setting keep_alive=-1, we keep the model
    loaded indefinitely, so the system prompt is only fully processed on the first call.
    Subsequent calls only process the new/delta tokens.
    """
    def __init__(self, model=DEFAULT_MODEL_OLLAMA):
        super().__init__()
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
            f"{'='*80}\n>>> PROMPT TO {self.name}\n{'='*80}\n{json.dumps(messages, indent=2)}\n{'='*80}\n"
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
            f"{body}\n{'='*80}\n"
        )

    def _backoff(self, attempt):
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))


class OpenRouterAPIProvider(LLMProvider):
    kind = KIND_CLOUD

    def __init__(self):
        super().__init__()
        
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
            f"{'='*80}\n>>> PROMPT TO {self.name}\n{'='*80}\n{json.dumps(messages, indent=2)}\n{'='*80}\n"
        )
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/chrishonson/night-shift-agent",
        }
        request_payload = {
            "model": model,
            "messages": messages
        }
        
        for attempt in range(MAX_RETRIES):
            try:
                req = urllib.request.Request(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    data=json.dumps(request_payload).encode('utf-8')
                )
                
                with urllib.request.urlopen(req, timeout=60) as response:
                    resp_body = response.read().decode('utf-8')
                    # Log raw
                    self._log_raw_response(attempt, 200, resp_body)
                    
                    resp_data = json.loads(resp_body)
                    if "error" in resp_data:
                         # Check for rate limits in API error
                         err_msg = json.dumps(resp_data['error']).lower()
                         if "rate limit" in err_msg or "quota" in err_msg or "insufficient" in err_msg:
                             raise QuotaExceededError(err_msg)
                         raise Exception(f"OpenRouter Error: {err_msg}")
                         
                    content = resp_data['choices'][0]['message']['content']
                    parsed = self.strip_markdown_code_blocks(content)
                    prompt_logger.debug(f"{'='*80}\n<<< PARSED RESPONSE\n{'='*80}\n{parsed}\n{'='*80}\n")
                    return parsed

            except QuotaExceededError:
                raise
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
        self.context_capture = None
        self._context_decision = 0
        self.providers: List[LLMProvider] = []
        self.force_provider = os.getenv("FORCE_PROVIDER", "").lower().strip()
        if self.force_provider:
            logger.info(f"🔌 FORCE_PROVIDER={self.force_provider}: requested provider override.")
        self._init_providers()

    def _init_providers(self):
        """Build the chain: headless first, local as the fallback under it.

        Cloud is not wired. OpenRouterAPIProvider still exists but nothing
        selects it, so no run can bill per token without a code change.
        """
        gemini_model = os.getenv("GEMINI_MODEL", os.getenv("PREFERRED_AGENT_MODEL", DEFAULT_MODEL_GEMINI))
        ollama_model = os.getenv("OLLAMA_MODEL", DEFAULT_MODEL_OLLAMA)
        claude_model = os.getenv("CLAUDE_MODEL", DEFAULT_MODEL_CLAUDE)
        antigravity_model = os.getenv("ANTIGRAVITY_MODEL", DEFAULT_MODEL_ANTIGRAVITY)

        headless = {
            "antigravity": lambda: AntigravityCLIProvider(model=antigravity_model),
            "claude": lambda: ClaudeCLIProvider(model=claude_model),
            "gemini": lambda: GeminiCLIProvider(model=gemini_model),
        }
        local = OllamaProvider(model=ollama_model)

        if self.force_provider == "ollama":
            self.providers = [local]
        elif self.force_provider in headless:
            self.providers = [headless[self.force_provider](), local]
        else:
            if self.force_provider:
                raise ValueError(f"Unknown FORCE_PROVIDER={self.force_provider}; choose ollama, antigravity, claude or gemini")
            self.providers = [headless[n]() for n in HEADLESS_ORDER] + [local]

        self.current_index = 0
        logger.info(f"🔌 Provider Chain: {[f'{p.name} [{p.kind}]' for p in self.providers]}")

    def ask(self, prompt: str) -> Optional[str]:
        # Start from the last working provider, not always from 0
        start_index = self.current_index
        
        for offset in range(len(self.providers)):
            i = (start_index + offset) % len(self.providers)
            provider = self.providers[i]
            
            self._context_decision += 1
            decision_id = self._context_decision
            capture_call(self.context_capture, "assembled", prompt, provider, decision_id)
            start_t = time.time()
            try:
                result = provider.ask(prompt)
                duration_ms = int((time.time() - start_t) * 1000)
                capture_call(self.context_capture, "provider_finished", decision_id, "returned" if result is not None else "empty", result, None, duration_ms)
                if result is not None:
                    self.current_index = i
                    return result
                
                logger.warning(f"⚠️ {provider.name} returned empty response. Switching...")
                next_i = (i + 1) % len(self.providers)
                if next_i != start_index:
                    logger.info(f"🔄 Switching to: {self.providers[next_i].name}...")
                    continue
                else:
                    logger.error("❌ All providers exhausted!")
                    return None
            except QuotaExceededError as e:
                duration_ms = int((time.time() - start_t) * 1000)
                capture_call(self.context_capture, "provider_finished", decision_id, "quota_error", None, str(e), duration_ms)
                logger.warning(f"🛑 {provider.name} Quota/Key Limit. Switching...")
                next_i = (i + 1) % len(self.providers)
                if next_i != start_index:  # Haven't looped back yet
                    logger.info(f"🔄 Switching to: {self.providers[next_i].name}...")
                    continue
                else:
                    logger.error("❌ All providers exhausted!")
                    return None
            except Exception as e:
                duration_ms = int((time.time() - start_t) * 1000)
                capture_call(self.context_capture, "provider_finished", decision_id, "error", None, str(e), duration_ms)
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
    def __init__(self, build_state: BuildState, project_dir: Path = None):
        self.build_state = build_state
        self.project_dir = Path(project_dir).resolve() if project_dir else Path.cwd()
        self.rate_limiter = RateLimiter()
        self.target_gates = []  # Specific gate_ids declared on current card
        self.last_gate_results = []  # GateResult list from last verification
        self.control_plane = None  # Optional ControlPlaneClient
        self.current_card_id = None

    def exec_command(self, command: str, env: dict = None, timeout: int = 600) -> subprocess.CompletedProcess:
        """Centralized helper for safe subprocess execution."""
        if env is None: env = os.environ.copy()
        
        # Ensure /opt/homebrew/bin is in PATH for Mac
        path = env.get("PATH", "")
        if "/opt/homebrew/bin" not in path:
             env["PATH"] = f"/opt/homebrew/bin:{path}"

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

    def _current_branch(self) -> str:
        try:
            r = subprocess.run(["git", "branch", "--show-current"], cwd=self.project_dir,
                               capture_output=True, text=True, timeout=10,
                               stdin=subprocess.DEVNULL)
            return r.stdout.strip()
        except Exception:
            return ""

    def run_shell(self, command: str) -> str:
        protected = blocked_push_target(command, current_branch=self._current_branch)
        if protected:
            logger.warning(f"\U0001F6E1\uFE0F Refused a push to protected branch '{protected}'")
            return (f"ERROR: '{protected}' is a protected branch and cannot be pushed to. "
                    f"Push your card branch instead and open a pull request.")

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
        if cmd_stripped.startswith("gh ") or cmd_stripped.startswith("git "):
            gh_token = resolve_gh_token()
            if gh_token and cmd_stripped.startswith("gh "):
                env["GITHUB_TOKEN"] = gh_token
            elif gh_token:
                # The token is no longer embedded in the remote URL, so git has
                # to be handed the credential explicitly.
                env.update(git_auth_env(gh_token, os.getenv("BOT_USERNAME", "agentnightshift")))

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
            if old_string in content:
                with open(target, "w") as f: f.write(content.replace(old_string, new_string, 1))
                logger.info(f"✏️ Replaced text in: {target}")
                self.build_state.files_changed_since_success.append(target)
                self.build_state.build_passed = False
                self.build_state.build_attempted = False
                return f"Successfully replaced text in {target}"
            elif new_string in content:
                return f"Success: Text already present in {target}"
            else:
                return f"Error: Text not found in {target}"
        except Exception as e: return f"Error: {e}"
    
    def list_files(self, path="."):
        files = []
        ignore = {".git", ".gradle", ".idea", "build", ".kotlin", "node_modules", ".agent_logs", ".agent_records"}
        for root, dirs, filenames in os.walk(path):
            dirs[:] = [d for d in dirs if d not in ignore and not d.startswith(".")]
            for f in filenames:
                if not f.startswith(".") and not f.endswith(('.jar','.class','.pyc')):
                    files.append(os.path.join(root, f))
        return "\n".join(files[:MAX_FILES_IN_CONTEXT])

    def _parse_and_contextualize_errors(self, output: str) -> str:
        """Parses compiler errors, reads source files, and returns a focused error report."""
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
                    if path_str.startswith("file://"):
                        path_str = path_str[7:]
                    
                    if os.path.exists(path_str):
                         files_to_read.add(os.path.abspath(path_str))
                    
                    extracted_errors.append(line)
                    break
                    
        if not extracted_errors:
             if len(output) > MAX_TOOL_OUTPUT_CHARS:
                half = MAX_TOOL_OUTPUT_CHARS // 2
                return output[:half] + "\n\n... [TRUNCATED] ...\n\n" + output[-half:]
             return output

        file_contexts = []
        for file_path in files_to_read:
             try:
                 with open(file_path, 'r') as f:
                     content = f.read()
                     file_contexts.append(f"--- FILE: {file_path} ---\n{content}\n")
             except Exception as e:
                 file_contexts.append(f"--- FILE: {file_path} ---\n[Error reading file: {e}]\n")
        
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
        verification_file = self.project_dir / "verification.json"
        run_gate_script = self.project_dir / "scripts" / "run-gate.py"

        if verification_file.exists() and run_gate_script.exists():
            # The card declares the gates it is judged by. Anything else makes
            # every TDD loop read red no matter what the tests actually did.
            contract = json.loads(verification_file.read_text())
            available = [g["id"] for g in contract.get("gates", [])]
            if not available:
                return "ERROR: Verification contract has no gates"
            gate_id = self.target_gates[0] if self.target_gates else available[0]
            test_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(run_gate_script))} {shlex.quote(gate_id)}"
        else:
            return "ERROR: Target needs verification.json and scripts/run-gate.py"
        
        logger.info(f"🧪 Running tests: {test_cmd}")
        
        env = os.environ.copy()
        try:
            result = self.exec_command(test_cmd, env=env, timeout=300)
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

    def _verify_via_contract(self, verification_file: Path, run_gate_script: Path) -> str:
        try:
            with open(verification_file, "r") as f:
                contract = json.load(f)
        except Exception as e:
            return f"Error reading verification contract: {e}"

        gates_to_run = []
        if self.target_gates:
            gates_to_run = list(self.target_gates)
        else:
            for g in contract.get("gates", []):
                if "local" in g.get("placement", []):
                    gates_to_run.append(g["id"])

        if not gates_to_run:
            self.build_state.build_attempted = True
            self.build_state.build_passed = False
            self.last_gate_results = []
            return "ERROR: No eligible verification gates declared"

        self.last_gate_results = []
        all_passed = True
        outputs = []

        for gate_id in gates_to_run:
            logger.info(f"🔍 Running contract gate: {gate_id}...")
            start_ms = int(time.time() * 1000)
            cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(run_gate_script))} {shlex.quote(gate_id)}"
            res = self.exec_command(cmd)
            duration_ms = int(time.time() * 1000) - start_ms

            output = (res.stdout + res.stderr).strip()
            if res.returncode == 0:
                self.last_gate_results.append({
                    "gate_id": gate_id,
                    "status": "passed",
                    "duration_ms": duration_ms
                })
                outputs.append(f"Gate '{gate_id}': PASSED ({duration_ms}ms)")
            else:
                all_passed = False
                self.last_gate_results.append({
                    "gate_id": gate_id,
                    "status": "failed",
                    "duration_ms": duration_ms
                })
                outputs.append(f"Gate '{gate_id}': FAILED ({duration_ms}ms)\n{output}")
                break

        self.build_state.build_attempted = True
        self.build_state.build_passed = all_passed

        if all_passed:
            logger.info("✅ Contract verification PASSED on all gates")
            if self.build_state.files_changed_since_success:
                self.build_state.checkpoint(self.build_state.files_changed_since_success)
            return "✅ CONTRACT VERIFICATION PASSED\n" + "\n".join(outputs)
        else:
            logger.warning("❌ Contract verification FAILED")
            combined_output = "\n\n".join(outputs)
            return f"❌ CONTRACT VERIFICATION FAILED\n\n{self._parse_and_contextualize_errors(combined_output)}"

    def verify_build(self) -> str:
        """Run the official verification build. Checks target repository contract first."""
        verification_file = self.project_dir / "verification.json"
        run_gate_script = self.project_dir / "scripts" / "run-gate.py"

        if verification_file.exists() and run_gate_script.exists():
            return self._verify_via_contract(verification_file, run_gate_script)
        self.build_state.build_attempted = True
        self.build_state.build_passed = False
        self.last_gate_results = []
        return "ERROR: Target needs verification.json and scripts/run-gate.py; no implicit build commands"

    def decompose(self, children: list = None, **kwargs) -> str:
        if not self.control_plane or not self.current_card_id:
            return "Error: Control plane decomposition not available for this task."
        child_list = children or kwargs.get("child_cards", [])
        if not child_list:
            return "Error: No child cards specified for decomposition."
        try:
            res = self.control_plane.decompose(self.current_card_id, child_list)
            return f"Successfully decomposed card into {len(res)} children."
        except Exception as e:
            return f"Decomposition failed: {e}"

    def dispatch(self, tool_name, args):
        mapping = {
            "read_file": self.read_file, "write_file": self.write_file,
            "run_shell": self.run_shell, "run_shell_command": self.run_shell,
            "replace": self.replace, "list_files": self.list_files,
            "run_tests": self.run_tests, "test": self.run_tests,  # TDD tool
            "verify_build": self.verify_build, "verify": self.verify_build,  # Alias
            "decompose": self.decompose, "card_decompose": self.decompose
        }
        
        # Arg Normalization
        if tool_name in ["run_shell", "run_shell_command"]:
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
            command = f"grep -rn '{pattern}' --include='{include}' ."
            args = {"command": command}
            tool_name = "run_shell"
        
        logger.info(f"Arguments for {tool_name}: {args}")

        func = mapping.get(tool_name)
        if func: return func(**args)
        return f"Unknown tool: {tool_name}. Available tools: {', '.join(mapping.keys())}"


# =============================================================================
# CONTROL PLANE CLIENT & LEASE WORKER
# =============================================================================

def detect_mobile_repos(git_root: Optional[Union[Path, str]] = None) -> List[str]:
    """
    Detect local repositories that are mobile-shaped (Android / KMP Gradle projects).
    Scans immediate subdirectories of git_root (defaults to GIT_REPOS_DIR or ~/git).
    Identifies mobile projects by presence of:
      - settings.gradle.kts or settings.gradle
      - local.properties (Android SDK location pointer)
      - AndroidManifest.xml (within standard locations or root)
      - build.gradle.kts / build.gradle referencing Android plugins
    """
    if git_root is None:
        env_root = os.getenv("GIT_REPOS_DIR")
        root = Path(env_root).expanduser() if env_root else (Path.home() / "git")
    else:
        root = Path(git_root).expanduser()

    if not root.is_dir():
        return []

    mobile_repos = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not (entry / ".git").exists():
            continue

        is_mobile = False
        if (entry / "settings.gradle.kts").exists() or (entry / "settings.gradle").exists():
            is_mobile = True
        elif (entry / "local.properties").exists():
            is_mobile = True
        elif (entry / "app" / "src" / "main" / "AndroidManifest.xml").exists():
            is_mobile = True
        elif (entry / "AndroidManifest.xml").exists():
            is_mobile = True
        else:
            for build_file in ("build.gradle.kts", "build.gradle"):
                bf = entry / build_file
                if bf.exists():
                    try:
                        content = bf.read_text(encoding="utf-8", errors="ignore")
                        if "android" in content.lower():
                            is_mobile = True
                            break
                    except Exception:
                        pass

        if is_mobile:
            mobile_repos.append(entry.name)

    return mobile_repos


def access_secret(secret_name: str) -> Optional[str]:
    """Read a Secret Manager version through gcloud. In-memory only: the value
    is never persisted and never logged, so a failure reports the cause without
    the payload."""
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", DEFAULT_GCP_PROJECT)
    cmd = ["gcloud", "secrets", "versions", "access", "latest",
           f"--secret={secret_name}", f"--project={project_id}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as e:
        logger.debug(f"Secret Manager access for '{secret_name}' skipped/failed: {e}")
        return None
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return None


# Sentinel so that "resolved to nothing" is cached as firmly as a hit. Without
# it, a host with no gcloud pays for a failed subprocess on every gh command.
_UNRESOLVED = object()
_gh_token_cache = _UNRESOLVED


def reset_gh_token_cache():
    global _gh_token_cache
    _gh_token_cache = _UNRESOLVED


def resolve_gh_token() -> Optional[str]:
    """The bot PAT, from Secret Manager first and .env second.

    Secret Manager leads deliberately. It is the copy that gets rotated, so a
    stale GH_BOT_TOKEN left in a .env file must not outrank it. The env var
    stays as the offline fallback and as the override for a host without
    gcloud.
    """
    global _gh_token_cache
    if _gh_token_cache is not _UNRESOLVED:
        return _gh_token_cache

    token = access_secret(os.getenv("GH_BOT_TOKEN_SECRET", DEFAULT_GH_TOKEN_SECRET))
    if not token:
        env_token = os.getenv("GH_BOT_TOKEN")
        token = env_token.strip() if env_token and env_token.strip() else None

    _gh_token_cache = token
    return token


# The script reads both answers out of the environment rather than embedding
# them, so the credential is never a file on disk. git calls it once per prompt
# with the prompt text as argv[1].
_ASKPASS_BODY = """#!/bin/sh
case "$1" in
  *sername*) printf '%s' "$GIT_BOT_USERNAME" ;;
  *) printf '%s' "$GIT_BOT_TOKEN" ;;
esac
"""
_askpass_path = None


def git_askpass_script() -> str:
    """Path to the askpass helper, written once per process, owner-only."""
    global _askpass_path
    if _askpass_path and os.path.exists(_askpass_path):
        return _askpass_path

    fd, path = tempfile.mkstemp(prefix="nightshift-askpass-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write(_ASKPASS_BODY)
    os.chmod(path, 0o700)
    _askpass_path = path
    return path


def git_auth_env(token: str, username: str) -> dict:
    """Environment that makes git authenticate as the bot.

    The machine's own credential helper is `gh auth git-credential`, which
    answers as the human account. Left enabled it would silently win, and the
    bot's push would be authorised by the wrong identity, so it is cleared for
    the duration of the command via GIT_CONFIG_* rather than by editing config.
    """
    return {
        "GIT_ASKPASS": git_askpass_script(),
        "GIT_BOT_USERNAME": username,
        "GIT_BOT_TOKEN": token,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
    }


class ControlPlaneClient:
    """Client for the control plane MCP server."""
    def __init__(self, base_url: str = None, token: str = None, identity_id: str = DEFAULT_IDENTITY_ID):
        raw_url = base_url or os.getenv("CONTROL_PLANE_URL", DEFAULT_CONTROL_PLANE_URL)
        self.base_url = raw_url.rstrip("/")
        self.identity_id = identity_id
        self.token = token or self._resolve_token()
        self.request_id = 0

    def _resolve_token(self) -> Optional[str]:
        # Environment first here, unlike the bot PAT: this token is per-identity
        # and the env var is how one host runs as a different worker.
        token = os.getenv("CONTROL_PLANE_BEARER_TOKEN")
        if token and token.strip():
            return token.strip()

        return access_secret(f"control-plane-{self.identity_id}")

    def call_tool(self, name: str, arguments: dict = None, timeout: int = 30) -> dict:
        if not self.token:
            raise RuntimeError(
                f"Bearer token for identity '{self.identity_id}' could not be resolved. "
                "Set CONTROL_PLANE_BEARER_TOKEN or ensure gcloud has Secret Manager access."
            )

        self.request_id += 1
        endpoint = f"{self.base_url}/mcp"
        payload = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments or {}
            }
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_text = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Control plane call '{name}' failed HTTP {e.code}: {err_body}")
        except Exception as e:
            raise RuntimeError(f"Control plane call '{name}' failed: {e}")

        response_obj = None
        for line in resp_text.splitlines():
            line_str = line.strip()
            if line_str.startswith("data:"):
                json_part = line_str[5:].strip()
                if json_part:
                    response_obj = json.loads(json_part)
                    break

        if not response_obj:
            response_obj = json.loads(resp_text)

        if "error" in response_obj:
            err = response_obj["error"]
            err_msg = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(f"MCP error in '{name}': {err_msg}")

        result = response_obj.get("result", {})
        content = result.get("content", [])
        if content and isinstance(content, list) and len(content) > 0:
            first = content[0]
            if first.get("type") == "text":
                raw_text = first.get("text", "{}")
                try:
                    return json.loads(raw_text)
                except Exception:
                    return raw_text

        return result

    def claim(self, resources: list = None, repos: list = None, lane: str = None) -> Optional[dict]:
        args = {}
        if resources:
            args["resources"] = resources
        if repos:
            args["repos"] = repos
        if lane:
            args["lane"] = lane
        res = self.call_tool("card_claim", args)
        if not res or (res.get("claimed") is None and "card" not in res):
            return None
        return res

    def heartbeat(self, run_id: str) -> dict:
        return self.call_tool("card_heartbeat", {"run_id": run_id})

    def release(self, run_id: str, outcome: str, gates: list = None, artifacts: dict = None, error: str = None) -> dict:
        args = {
            "run_id": run_id,
            "outcome": outcome
        }
        if gates is not None:
            args["gates"] = gates
        if artifacts is not None:
            args["artifacts"] = artifacts
        if error is not None:
            args["error"] = error[:4096]
        return self.call_tool("card_release", args)

    def decompose(self, parent_id: str, children: list) -> list:
        return self.call_tool("card_decompose", {"parent_id": parent_id, "children": children})

    def snapshot(self) -> dict:
        return self.call_tool("board_snapshot", {})


class LeaseHeartbeatWorker:
    """Background daemon thread maintaining the lease on an in-progress card."""
    def __init__(self, client: ControlPlaneClient, run_id: str, interval: float = CONTROL_PLANE_HEARTBEAT_INTERVAL):
        self.client = client
        self.run_id = run_id
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"heartbeat-{run_id[:8]}")
        self.abandoned = False

    def start(self):
        self.thread.start()

    def _run(self):
        while not self.stop_event.wait(self.interval):
            try:
                res = self.client.heartbeat(self.run_id)
                expires = res.get("lease", {}).get("expires_at")
                logger.debug(f"💓 Heartbeat extended for run {self.run_id} (expires at {expires})")
            except Exception as e:
                err_str = str(e).lower()
                logger.warning(f"⚠️ Heartbeat failed for run {self.run_id}: {e}")
                if "abandoned" in err_str or "not found" in err_str or "not held" in err_str:
                    logger.error(f"🛑 Run {self.run_id} invalidated on control plane.")
                    self.abandoned = True
                    break

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=3)

# =============================================================================
# MAIN AGENT CONTROLLER
# =============================================================================

class NightShiftAgent:
    def __init__(self, project_dir=".", control_plane_url: str = None, token: str = None):
        self.project_dir = Path(project_dir).resolve()
        if not self.project_dir.exists():
            raise ValueError(f"Project dir not found: {project_dir}")
        os.chdir(self.project_dir)

        self.control_plane = ControlPlaneClient(base_url=control_plane_url, token=token)
        self.current_run_id = None
        self.current_card = None

        self.build_state = BuildState()
        self.toolbox = Toolbox(self.build_state, project_dir=self.project_dir)
        self.toolbox.control_plane = self.control_plane
        self.llm = ProviderManager()
        self.bot_username = os.getenv("BOT_USERNAME", "agentnightshift")
        self.gh_token = resolve_gh_token()
        self._current_file_handlers = []
        self.stall_threshold = STALL_WITHOUT_WRITE_THRESHOLD
        self.iteration_delay = DEFAULT_ITERATION_DELAY
        self.last_stall_reason = None
    
        # Set up file logging in the project directory
        self._setup_logging()

    def _setup_logging(self, run_id: str = None):
        """Set up file handlers in the project directory. If run_id is supplied, keys log by run_id."""
        log_dir = self.project_dir / ".agent_logs"
        log_dir.mkdir(exist_ok=True)
        
        # Clean up prior file handlers if switching runs
        for h in self._current_file_handlers:
            logger.removeHandler(h)
            prompt_logger.removeHandler(h)
        self._current_file_handlers = []

        tag = run_id or f"session_{SESSION_TIMESTAMP}"
        log_file = log_dir / f"{tag}.log"
        prompt_log_file = log_dir / f"prompts_{tag}.log"
        
        # Add file handler to main logger
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S'))
        logger.addHandler(file_handler)
        self._current_file_handlers.append(file_handler)
        
        # Add file handler to prompt logger
        prompt_handler = logging.FileHandler(prompt_log_file)
        prompt_handler.setFormatter(logging.Formatter('%(asctime)s\n%(message)s\n'))
        prompt_logger.addHandler(prompt_handler)
        self._current_file_handlers.append(prompt_handler)
        
        logger.info(f"📁 Logging to: {log_dir} ({tag})")

    def detect_resources(self) -> list:
        """Detect physical resources available on this host (e.g. attached Android device)."""
        resources = []
        try:
            res = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=3)
            if res.returncode == 0:
                lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
                for line in lines[1:]:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] in ("device", "unauthorized"):
                        resources.append("android-device")
                        break
        except Exception as e:
            logger.debug(f"Resource detection (adb): {e}")
        return resources

    def run_cmd_quiet(self, cmd):
        env = os.environ.copy()
        
        # Ensure /opt/homebrew/bin is in PATH for Mac
        path = env.get("PATH", "")
        if "/opt/homebrew/bin" not in path:
             env["PATH"] = f"/opt/homebrew/bin:{path}"
             
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

    def commit_changes(self, task: str):
        """Create a local review commit without shell interpolation or diagnostic files."""
        args = {"cwd": self.project_dir, "capture_output": True, "text": True,
                "timeout": 30, "stdin": subprocess.DEVNULL}
        staged = subprocess.run(["git", "add", "--", ".", ":(exclude).agent_logs",
                                 ":(exclude).agent_records"], **args)
        if staged.returncode:
            return False
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], **args)
        if changed.returncode != 1:
            return False
        result = subprocess.run(["git", "commit", "-m", "Night Shift: " + task], **args)
        return result.returncode == 0

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
   NOTE: This runs the target repository verification.json gates.
         The task is NOT complete until this passes.

LOCAL EXECUTION: Do not push, create PRs, deploy, or alter credentials. Return local changes for review.

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
- Tests follow the target repository conventions; inspect existing tests first
- You MUST call verify_build before considering the task complete
- Preserve coverage thresholds declared by the target repository

CRITICAL - DO NOT HALLUCINATE:
- NEVER generate fake "USER:" or "TOOL OUTPUT:" text - you are NOT simulating a conversation
- STOP IMMEDIATELY after outputting a single <agent_action> tag - do not continue
- The REAL tool output will be provided by the system in the next message - do NOT imagine it
- If you find yourself writing "USER:" or "ASSISTANT:" in your response, STOP - that is wrong
- Each response should contain AT MOST one <agent_action> block, then STOP
"""
        task_intro = f"TASK: {task}\n\nBegin by reading the relevant files to understand the current implementation."
        
        # Initialize messages list with system prompt and task
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task_intro}
        ]
        consecutive_failures = 0
        replace_repeat_counts = {}

        # Card #17: Stall detection state
        self.last_stall_reason = None
        has_written = False
        has_verified = False
        tool_counts = Counter()
        card_kind = (self.current_card.get("kind") if self.current_card else "software") or "software"
        stall_detection_enabled = (card_kind == "software")

        last_provider_index = self.llm.current_index
        i = 0
        while i < MAX_ITERATIONS:
            logger.info(f"🔄 Iteration {i+1}/{MAX_ITERATIONS}")
            
            # Check for silent provider switch (e.g. QuotaExceeded inside ask())
            # and reset iteration count if it happened to give new model a chance
            if self.llm.current_index != last_provider_index:
                logger.info("Provider switched; preserving the total task iteration limit")
                last_provider_index = self.llm.current_index
                tool_counts.clear()
            
            # Context Pruning: preserve system (0) and task (1), prune middle pairs
            before_pruning = list(messages) if self.llm.context_capture else None
            total_chars = sum(len(m.get("content", "")) for m in messages)
            while len(messages) > 4 and total_chars > MAX_CONTEXT_CHARS:
                # Remove oldest user/assistant pair after the task (indices 2, 3)
                messages.pop(2)
                messages.pop(2)  # Was index 3, now 2 after first pop
                total_chars = sum(len(m.get("content", "")) for m in messages)
                logger.info(f"🧹 Pruned context: {len(messages)} messages, {total_chars} chars")

            if before_pruning is not None and len(before_pruning) != len(messages):
                capture_call(self.llm.context_capture, "transformed", before_pruning, messages, MAX_CONTEXT_CHARS)

            # LLM Call with messages list
            response = self.llm.ask(messages)
            if not response: return False
            
            # Hallucination Detection: Strip fake USER/TOOL OUTPUT content
            # Some models (especially Gemini) hallucinate entire conversations
            hallucination_patterns = [
                r'\nUSER:.*',
                r'\nTOOL OUTPUT.*',
                r'\nASSISTANT:.*',
                r'\n\u003cuser\u003e.*',
                r'\n\u003ctool_output\u003e.*',
            ]
            original_len = len(response)
            for pattern in hallucination_patterns:
                match = re.search(pattern, response, re.IGNORECASE | re.DOTALL)
                if match:
                    response = response[:match.start()]
                    break
            if len(response) < original_len:
                logger.warning(f"⚠️ Stripped {original_len - len(response)} chars of hallucinated content")
            
            messages.append({"role": "assistant", "content": response})
            
            # Extract Actions (only process FIRST action to prevent hallucination cascades)
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
            
            # Only process FIRST action to prevent hallucination cascades
            if len(actions) > 1:
                logger.warning(f"⚠️ Found {len(actions)} actions in response, only processing first one")
                actions = actions[:1]
            
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
                        tool_counts[tool] += 1
                        logger.info(f"🛠️ Tool: {tool}")
                        tool_start_t = time.time()
                        tool_err = None
                        try:
                            output = self.toolbox.dispatch(tool, args)
                        except Exception as e:
                            tool_err = str(e)
                            output = f"Tool execution error: {e}"
                        tool_duration_ms = int((time.time() - tool_start_t) * 1000)
                        capture_call(self.llm.context_capture, "tool_executed", tool, args, output, tool_err, tool_duration_ms)
                        if tool in ("verify_build", "verify", "run_tests", "test"):
                            capture_call(self.llm.context_capture, "verification_finished", self.toolbox.last_gate_results, self.build_state.build_passed)
                        messages.append({"role": "user", "content": f"TOOL OUTPUT ({tool}): {output}"})

                        # Card #17: Track writes and verification attempts
                        if tool == "write_file" and not (str(output).startswith("Error") or str(output).startswith("ERROR")):
                            has_written = True
                        elif tool == "replace" and "successfully replaced text" in str(output).lower():
                            has_written = True
                        elif tool in ("verify_build", "verify"):
                            has_verified = True

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
                capture_call(self.llm.context_capture, "verification_finished", self.toolbox.last_gate_results, self.build_state.build_passed)
                messages.append({"role": "user", "content": f"AUTO-VERIFICATION OUTPUT:\n{build_output}"})
                
            # Build Failure Logic - only increment on actual build attempts
            if tool_run and self.build_state.build_attempted:
                if not self.build_state.build_passed:
                    consecutive_failures += 1
                    if consecutive_failures >= 5:
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
                else:
                    consecutive_failures = 0
                self.build_state.build_attempted = False

            if len(self.build_state.files_changed_since_success) > 0:
                has_written = True
            if self.build_state.build_attempted:
                has_verified = True
            
            # Check Success (Build Passed + User Task satisfied implies we should commit)
            if self.build_state.build_passed and self.build_state.is_verified():
                logger.info("✅ Build Passed. Task Complete.")
                return True

            # Card #17: Abort silent explore-forever runs with no writes and no verification
            if (
                stall_detection_enabled
                and (i + 1) >= self.stall_threshold
                and not has_written
                and not has_verified
            ):
                if tool_counts:
                    breakdown_parts = [f"{count} {t}" for t, count in tool_counts.most_common()]
                    tool_summary = ", ".join(breakdown_parts)
                else:
                    tool_summary = "0 tools executed"

                stall_msg = f"Explored {i + 1} iterations without writing ({tool_summary})"
                logger.warning(f"🛑 Stall detected: {stall_msg}")
                self.last_stall_reason = stall_msg
                return False
            
            i += 1
            if self.iteration_delay > 0:
                time.sleep(self.iteration_delay)
        
        return False

    def execute_task_card(self, card, run_id, heartbeat):
        """Non-software work belongs to a coordinator, not this coding worker."""
        if heartbeat.abandoned:
            return "abandoned", [], None, "Card abandoned before execution"
        return "blocked", [], None, "Unsupported task kind: route non-software work to a coordinator"

    def check_preexisting_satisfaction(
        self, card: dict, target_dir: Optional[Path] = None
    ) -> tuple:
        """Check whether the card goal is already satisfied before starting a coding attempt (Card #16).

        Evaluates declared baseline gates first. If all gates pass, explicitly evaluates
        the card goal and acceptance criteria against current workspace behavior.

        Passing existing gates alone is insufficient. A no-change success requires
        goal-specific evidence plus all required gates passing, recorded with an explicit
        no-commit explanation. If the goal is not proved, continue normal execution.
        Gate errors never imply success.

        Returns:
            (is_satisfied: bool, gate_results: list, acceptance_eval: dict, explanation: Optional[str])
        """
        target_path = Path(target_dir).resolve() if target_dir else self.project_dir
        gate_ids = list(card.get("gate_ids") or [])
        self.toolbox.target_gates = list(gate_ids)

        # 1. Run declared gates as baseline
        logger.info(f"🔍 Running baseline verification gates for card {card.get('id', 'unknown')}...")
        self.toolbox.verify_build()
        gate_results = list(self.toolbox.last_gate_results)

        # If last_gate_results is empty, fall back to build_state
        if not gate_results:
            status = "passed" if self.build_state.build_passed else "failed"
            gate_results = [{"gate_id": "baseline_build", "status": status, "duration_ms": 0}]

        all_gates_passed = all(g.get("status") == "passed" for g in gate_results) if gate_results else False

        # Gate errors never imply success
        if not all_gates_passed:
            logger.info("🔴 Baseline gates failed or errored; cannot satisfy without changes (gate errors never imply success).")
            acceptance_eval = {
                "evaluated": False,
                "passed": False,
                "reason": "Baseline gates failed or errored (gate errors never imply success)"
            }
            return False, gate_results, acceptance_eval, None

        # 2. Baseline gates passed -> explicitly check card goal and acceptance criteria against current behavior
        criteria = card.get("acceptance_criteria") or []
        if not criteria:
            logger.info("ℹ️ Baseline gates passed, but no goal-specific acceptance criteria defined to prove preexisting satisfaction.")
            acceptance_eval = {
                "evaluated": False,
                "passed": False,
                "reason": "Passing existing gates alone is insufficient; no goal-specific acceptance criteria declared"
            }
            return False, gate_results, acceptance_eval, None

        from run_record import evaluate_criteria
        acceptance_eval = evaluate_criteria(criteria, target_path, patch_text="", gate_results=gate_results)

        if acceptance_eval.get("passed"):
            explanation = (
                "Goal already satisfied before coding attempt: all acceptance criteria "
                "verified against current behavior and all required gates passed. No commit required."
            )
            logger.info(f"✅ Goal already satisfied before coding attempt: {explanation}")
            return True, gate_results, acceptance_eval, explanation
        else:
            explanation = (
                "Baseline gates passed, but requested functionality absent / acceptance criteria "
                "not satisfied. Continuing normal execution."
            )
            logger.info(f"⚡ {explanation}")
            return False, gate_results, acceptance_eval, explanation

    def execute_card(self, card: dict, run_id: str, heartbeat: LeaseHeartbeatWorker) -> tuple:
        """Execute a single claimed card. Returns (outcome, gate_results, artifacts, error_msg)."""
        card_id = card.get("id", "unknown")
        kind = card.get("kind", "software")
        repo_name = card.get("repo")
        gate_ids = card.get("gate_ids") or []

        self.current_run_id = run_id
        self.current_card = card
        self.toolbox.current_card_id = card_id
        self.toolbox.target_gates = list(gate_ids)
        self.toolbox.last_gate_results = []

        # Set up logging keyed to this run_id (observability join key)
        self._setup_logging(run_id=run_id)

        if kind != "software":
            return self.execute_task_card(card, run_id, heartbeat)

        orig_cwd = Path.cwd()
        target_dir = self.project_dir

        if repo_name and Path(repo_name).name != self.project_dir.name:
            return "blocked", [], None, "Card repository does not match explicit --project-dir"
        if not gate_ids:
            return "blocked", [], None, "Software cards require explicit verification gates"

        os.chdir(target_dir)
        self.toolbox.project_dir = target_dir
        logger.info(f"📂 Switched to target directory: {target_dir}")

        branch = None
        artifacts = None

        try:
            if kind == "software" and (target_dir / ".git").exists():
                self.configure_git()
                branch = f"{BRANCH_PREFIX}/{card_id}"
                self.run_cmd_quiet(f"git checkout -b {shlex.quote(branch)} 2>/dev/null || git checkout {shlex.quote(branch)}")
                logger.info(f"🌿 Working on branch: {branch}")

            # Card #16: Check whether goal is already satisfied before starting coding attempt
            is_satisfied, gate_results, acceptance, explanation = self.check_preexisting_satisfaction(card, target_dir)
            if self.llm.context_capture:
                all_passed = all(g.get("status") == "passed" for g in gate_results) if gate_results else False
                capture_call(self.llm.context_capture, "verification_finished", gate_results, all_passed)

            if is_satisfied:
                self._last_preexisting_acceptance = acceptance
                logger.info(f"✅ Pre-attempt check satisfied: {explanation}")
                artifacts = {
                    "no_commit": True,
                    "no_commit_reason": explanation,
                    "branch": branch,
                }
                return "succeeded", gate_results, artifacts, explanation

            logger.info("⚡ Goal not satisfied by preexisting behavior; proceeding with normal execution.")

            task_intro = f"CARD [{card_id}] ({kind.upper()}): {card.get('title')}\nGOAL: {card.get('goal')}"

            arch_output = self.toolbox.run_shell("tree -L 2 -I 'build|.*' 2>/dev/null || find . -maxdepth 2 -type d ! -path '*/.*' ! -path './build*' 2>/dev/null | head -50")
            arch_doc = target_dir / "docs" / "ARCHITECTURE.md"
            if arch_doc.exists():
                try:
                    arch_output = f"=== Directory Structure ===\n{arch_output}\n\n=== ARCHITECTURE.md ===\n{arch_doc.read_text()}"
                except Exception as e:
                    logger.warning(f"Failed to read ARCHITECTURE.md: {e}")

            files = self.toolbox.list_files()
            self.last_stall_reason = None
            task_success = self.process_task(task_intro, arch_output, files)

            if heartbeat.abandoned:
                logger.warning(f"🛑 Run {run_id} was abandoned by admin.")
                return "abandoned", self.toolbox.last_gate_results, None, "Card abandoned by admin during execution"

            if task_success:
                commit_ok = self.commit_changes(f"[{card_id}] {card.get('title')}")
                commit_sha = self.run_cmd_quiet("git rev-parse HEAD").stdout.strip() if commit_ok else None

                if branch and commit_ok:
                    artifacts = {"branch": branch, "commit_sha": commit_sha}

                return "succeeded", self.toolbox.last_gate_results, artifacts, None
            elif self.last_stall_reason:
                logger.warning(f"🛑 Releasing card {card_id} as blocked due to stall: {self.last_stall_reason}")
                return "blocked", self.toolbox.last_gate_results, None, self.last_stall_reason
            else:
                return "failed", self.toolbox.last_gate_results, None, "Verification failed or max iterations reached"

        finally:
            os.chdir(orig_cwd)
            self.toolbox.project_dir = orig_cwd

    def run_control_plane(
        self,
        lane: str = None,
        max_runs: int = None,
        poll_interval_base: float = CONTROL_PLANE_POLL_INTERVAL_BASE,
        until_empty: bool = False,
        repos: list = None
    ):
        """Control-plane mode: poll controlPlaneMcp for ready cards, execute with heartbeats."""
        # An empty --repos is not "every repo": claiming unscoped would hand this
        # worker task cards and other repositories' cards, which it cannot execute.
        # An empty --repos is not "every repo": claiming unscoped would hand this
        # worker task cards and other repositories' cards, which it cannot execute.
        if not repos:
            repos = [self.project_dir.name]
            logger.info(f"Target repo: {repos}")
        else:
            logger.info(f"🎯 Caller-specified repos: {repos}")

        lane_str = f" (lane: {lane})" if lane else ""
        logger.info(f"🎛️ Night Shift starting in control-plane mode{lane_str}, until_empty={until_empty}...")
        runs_count = 0

        while True:
            if max_runs is not None and runs_count >= max_runs:
                logger.info(f"Reached max runs limit ({max_runs}). Exiting control-plane loop.")
                break

            # Add jitter between 30s and 60s
            jitter = random.uniform(-15.0, 15.0)
            delay = max(10.0, poll_interval_base + jitter)

            resources = self.detect_resources()
            try:
                claim_result = self.control_plane.claim(resources=resources, repos=repos, lane=lane)
            except Exception as e:
                logger.error(f"❌ Error polling control plane: {e}")
                time.sleep(delay)
                continue

            if not claim_result or not claim_result.get("card"):
                if until_empty:
                    logger.info("🏁 No matching ready cards (until_empty=True). Loop finished.")
                    break
                no_cards_str = f" in lane '{lane}'" if lane else ""
                logger.info(f"😴 No cards ready{no_cards_str}. Sleeping {delay:.1f}s...")
                time.sleep(delay)
                continue

            card = claim_result["card"]
            run_id = claim_result["run_id"]
            logger.info(f"🎯 Claimed card {card.get('id')}: '{card.get('title')}' (Run {run_id})")

            from run_record import RunRecord, EVIDENCE_CLASS_REAL_MODEL
            import uuid
            record_root = Path(os.getenv("NIGHT_SHIFT_RECORD_DIR", str(self.project_dir / ".agent_records")))
            record = RunRecord(record_root / str(uuid.uuid4()), card,
                               evidence_class=EVIDENCE_CLASS_REAL_MODEL,
                               mode=os.getenv("NIGHT_SHIFT_CAPTURE_MODE", "metadata"),
                               workspace_dir=self.project_dir)
            capture = record.capture
            self._last_patch = None
            self._last_preexisting_acceptance = None
            self.build_state = BuildState()
            self.toolbox.build_state = self.build_state
            self.llm.context_capture = capture
            heartbeat = LeaseHeartbeatWorker(self.control_plane, run_id)
            heartbeat.start()

            outcome = "failed"
            gate_results = []
            artifacts = None
            error_msg = None

            try:
                outcome, gate_results, artifacts, error_msg = self.execute_card(card, run_id, heartbeat)
            except Exception as e:
                logger.error(f"❌ Unhandled error executing card {card.get('id')}: {e}")
                error_msg = str(e)
                outcome = "failed"
            finally:
                heartbeat.stop()

            release_status = "unknown"
            try:
                logger.info(f"🏁 Releasing card {card.get('id')} (Run {run_id}) outcome={outcome}")
                self.control_plane.release(
                    run_id=run_id,
                    outcome=outcome,
                    gates=gate_results,
                    artifacts={k: v for k, v in (artifacts or {}).items()
                               if k in ("branch", "commit_sha", "pr_url") and v is not None} or None,
                    error=error_msg
                )
                release_status = "acknowledged"
            except Exception as e:
                logger.error(f"❌ Failed to release card {card.get('id')}: {e}")
            finally:
                no_commit_expl = artifacts.get("no_commit_reason") if isinstance(artifacts, dict) else None
                acceptance = getattr(self, "_last_preexisting_acceptance", None) if no_commit_expl else None
                try:
                    record.finalize(outcome, release_status=release_status,
                                    workspace_path=self.project_dir, gate_results=gate_results,
                                    patch_text=self._last_patch, providers=self.llm.providers,
                                    no_commit_explanation=no_commit_expl,
                                    stall_reason=self.last_stall_reason, error=error_msg)
                    logger.info(f"Run record: {record.output_dir}")
                finally:
                    self.llm.context_capture = None

            runs_count += 1

    def run(self, until_empty: bool = False, repos: list = None):
        """Default run entrypoint: takes work from the control plane."""
        self.run_control_plane(until_empty=until_empty, repos=repos)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Night Shift Agent - Control Plane Worker")
    parser.add_argument('--project-dir', default='.', help="Working project directory")
    parser.add_argument('--lane', default=None, help="[Deprecated] Legacy control plane lane")
    parser.add_argument('--max-runs', type=int, default=None, help="Max cards to process before exiting")
    parser.add_argument('--poll-interval', type=float, default=CONTROL_PLANE_POLL_INTERVAL_BASE, help="Base polling delay in seconds")
    parser.add_argument('--until-empty', action='store_true', default=False, help="Exit when no matching cards are claimable instead of polling indefinitely")
    parser.add_argument('--repos', nargs='*', default=None, help="Repos to claim (defaults to the explicitly selected project directory name)")
    args = parser.parse_args()
    
    agent = NightShiftAgent(args.project_dir)
    agent.run_control_plane(
        lane=args.lane, max_runs=args.max_runs,
        poll_interval_base=args.poll_interval,
        until_empty=args.until_empty, repos=args.repos)
