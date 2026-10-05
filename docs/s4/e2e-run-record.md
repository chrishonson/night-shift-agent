# S4 End-to-End Run Record and Offline Validation

This document describes the evaluable end-to-end night-shift run record format,
the opt-in local payload-capture mode, the offline validator, and the distinction
between scripted-provider fixtures and real-model evidence.

## Overview

Card #50 delivers an offline-evaluable end-to-end run record for Night Shift:
bounded coding tasks executed through the **actual tool loop**, producing a
self-contained sample that an offline validator or reviewer can inspect and
reconstruct **without parsing console logs**.

The record captures:
- Sanitized task specification and independent acceptance criteria
- Starting git commit SHA, dirty state, and branch
- Harness, model, and provider configuration
- Supplied context messages and model responses (payload mode)
- Provider attempts and failover attribution
- Tool invocations, arguments, outputs, error states, and execution timing
- Unified diff patch of workspace changes
- Verification gate results
- Terminal outcome and release status
- Independent acceptance evaluation (distinguishing green baseline tests from actual goal achievement)

## Opt-In Payload Capture Mode

Payload capture is **off by default**. It coexists alongside the existing
metadata-only mode (`S4_CONTEXT_CAPTURE_DIR`):

- **Metadata mode** (default): Emits hashed message identifiers, attempt boundaries,
  pruning transformation hashes, and provider completion statuses. Never retains
  prompt text, model responses, tool arguments, error text, or credentials.
- **Payload mode** (opt-in via `S4_PAYLOAD_CAPTURE=1` or `run_record.RunRecord`):
  Emits sanitized payload events (`attempt_opened`, `context_assembled`,
  `provider_call_finished`, `tool_call_finished`, `verification_finished`,
  `trace_finalized`).

### Security and Redaction Guarantees

- **No Lease Capabilities**: Lease tokens and capability strings are scrubbed
  from task cards and event data before recording.
- **Secret Redaction**: GitHub PATs, Bearer tokens, private keys, API credentials,
  and sensitive keys (`lease`, `token`, `secret`, `password`, `key`, `auth`) are
  automatically redacted with `[REDACTED_SECRET]`.
- **Explicit Truncation & Unavailable Markers**: Payloads exceeding limits are
  truncated with explicit byte/character counts. Offline or unobserved subsystems
  (such as `quota_reader` and real-model evidence) are recorded with explicit
  `{"status": "unavailable", "reason": "..."}` objects rather than fabricated data.
- **Independent Trace IDs**: Every capture attempt receives an isolated UUID trace ID.

## Evidence Classification Labeling

Every run record manifest explicitly identifies its evidence class:

| Label | Description |
|---|---|
| `evidence_class: scripted_provider_fixture` | A deterministic scripted provider fixture executed through the actual worker tool loop. Validates worker mechanics, tool dispatch, verification gates, and run-record finalization without non-deterministic model calls. |
| `evidence_class: real_model` | Live model runs (e.g. Gemini, Ollama, Claude). When absent, marked as `real_model_evidence: {"status": "unavailable", "reason": "no_real_model_run_for_card_50"}`. No real-model execution is required for this card. |

## Run Record Artifacts

A run record directory contains three self-contained artifacts:

1. `manifest.json`: Versioned schema (`s4-run-record.v1`) containing task definition,
   independent acceptance criteria, git environment, provider attempts, failover
   attribution, tool call index, verification summary, and terminal outcome.
2. `events.jsonl`: Monotonically sequenced, newline-delimited JSON events recording
   each boundary of the worker execution.
3. `patch.diff`: Unified diff representing the exact code changes made to the workspace.

## Offline Validation and Review

An offline reviewer or automated gate validates and reconstructs the run from
the sample alone without reading console logs:

### Validation Commands

Using `scripts/run-record.py`:
```sh
# Validate the committed sample (human-readable review + exit code)
.venv/bin/python scripts/run-record.py validate eval/s4/e2e-run-record-sample

# Output machine-readable validation JSON
.venv/bin/python scripts/run-record.py validate eval/s4/e2e-run-record-sample --json

# Detailed human-readable review report
.venv/bin/python scripts/run-record.py review eval/s4/e2e-run-record-sample
```

Using `scripts/context-eval.py`:
```sh
# Integrated validation command
.venv/bin/python scripts/context-eval.py validate eval/s4/e2e-run-record-sample

# Integrated review command
.venv/bin/python scripts/context-eval.py review eval/s4/e2e-run-record-sample
```

### Exit Codes

- `0`: Valid, complete capture, and terminal outcome succeeded with independent acceptance passed.
- `2`: Validation failure (task outcome failed, independent acceptance criteria failed, or malformed action).
- `3`: Incomplete capture (dropped events, missing finalization) or leaked secrets detected.

## Independent Acceptance vs. Green Baseline Tests

A critical design requirement is that **green existing tests alone ≠ task success**.
In a coding task:
- Baseline unit tests may pass simply because old functionality was not broken.
- If the requested feature was not implemented or returns incorrect values, the
  verification gate may be green while the task goal remains unfulfilled.
- The run record evaluates independent acceptance criteria against the final
  workspace state and patch, recording explicit per-criterion pass/fail status.
