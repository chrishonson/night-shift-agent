# Worker Local Integration Manifest

- **Branch**: `codex/worker-local-integration`
- **Tip SHA**: `7b67a66fcb7b8b8e124de1a0500a3d74ccc8b9d2` (integration merge tip)
- **Timestamp**: Sun Sep 13 17:55:42 CDT 2026 (America/Chicago)
- **Evidence Directory**: `/tmp/ns-worker-local-integration-evidence/`

## Merged Commits

1. `9cfed12` feat(eval): add S4 board reconciliation benchmark fixtures, harness and tests (Integration root)
2. `ba73ab8` feat: auto-detect mobile repos and support until-empty in control-plane loop (PR #3)
3. `52cb0ac` Separate task-card drafts from software execution with persisted evidence (PR #4)
4. `6016e73` Resolve the bot PAT from Secret Manager so rotation actually takes effect
5. `7fcb66b` Authenticate git as the bot without a token in the remote URL

## Gate Results

### 1. `worker_syntax`
- **Command**: `.venv/bin/python -m py_compile agent_night_shift.py context_capture.py context_decision_eval.py board_reconciliation_eval.py scripts/context-eval.py`
- **Exit Code**: `0`
- **Raw Log**: `/tmp/ns-worker-local-integration-evidence/worker_syntax.log`

### 2. `worker_tests`
- **Command**: `.venv/bin/python -m pytest -q parts_tests --cov=agent_night_shift --cov=context_capture --cov=context_decision_eval --cov=board_reconciliation_eval --cov-fail-under=55`
- **Exit Code**: `0`
- **Key Coverage Line**: `Required test coverage of 55% reached. Total coverage: 63.21%`
- **Summary**: `150 passed in 14.82s`
- **Raw Log**: `/tmp/ns-worker-local-integration-evidence/worker_tests.log`
