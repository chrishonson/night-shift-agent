#!/usr/bin/env bash
set -euo pipefail
AGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /absolute/path/to/isolated/repository [worker options]" >&2
  exit 2
fi
TARGET_DIR="$1"
shift
: "${FORCE_PROVIDER:?Set FORCE_PROVIDER explicitly (for example ollama)}"
exec "$AGENT_ROOT/.venv/bin/python" "$AGENT_ROOT/agent_night_shift.py" \
  --project-dir "$TARGET_DIR" --max-runs 1 --until-empty "$@"
