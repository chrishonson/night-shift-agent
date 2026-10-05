# S4 Stall-Without-Write Detection and Mid-Loop Abort (Card #17)

This document describes the worker behavior, empirical threshold justification, offline validation, and evidence artifacts for aborting silent explore-forever runs without writes.

## Overview and Goal

In autonomous coding runs, agents occasionally enter silent "explore-forever" loops: issuing endless read or shell commands (`read_file`, `find_by_name`, `grep_search`, `run_shell`) without ever attempting code modifications (`write_file`, `replace`) or gate verification (`verify_build`). Without early termination, these dead runs burn the maximum iteration budget (up to 80 iterations per attempt) without making progress.

Card #17 introduces a data-justified mid-loop stall detector that halts explore-forever runs early and releases the card as **blocked** (not `failed`) to park it for operator intervention.

## Empirical Threshold Justification

The stall threshold of **25 iterations** is derived directly from empirical run telemetry rather than arbitrary preference:

1. **Successful Completions**:
   - In production runs, successful card completions occurred at **30, 66, 29, and 29 iterations**.
   - In all successful runs, the agent began modifying code (`write_file` or `replace`) **well before iteration 25** (typically within the first 5 to 15 iterations).
2. **Dead Exploration Runs**:
   - In contrast, unrecoverable exploration loops burned all **80 iterations** with **zero writes** and **zero verification attempts**.
   - Two dead runs alone accounted for 160 of 405 total iterations (~40% of the entire run budget).
3. **Margin and Safety**:
   - A threshold of **25 iterations** (`DEFAULT_STALL_WITHOUT_WRITE_THRESHOLD = 25`, overridable via `STALL_WITHOUT_WRITE_THRESHOLD`) provides ample headroom (>15–20 exploratory tool invocations) for legitimate read-heavy understanding and discovery phases.
   - Any software implementation run reaching iteration 25 without a single write or verification attempt has stalled and will not recover on its own.
   - Aborting at iteration 25 immediately halts the loop, saving up to 55 iterations of wasted budget per failed attempt.

## Core Detection Behavior and Requirements

### 1. Healthy Exploration vs. Dead Stalls
- **Lots of reads followed by a write is healthy**: Once a write (`write_file`, successful `replace`, or workspace file change) or verification attempt (`verify_build`) occurs, the stall condition is permanently cleared for that attempt.
- **Only the complete absence of any write or verification** at the threshold iteration triggers stall detection.

### 2. Release as Blocked, Not Failed
- When a stall is detected, the card is released with `outcome: "blocked"` and `release_status: "acknowledged"`.
- Releasing as `failed` would return the card to `ready` status, causing another worker to claim the card and burn another 80 iterations on the exact same dead pattern.
- Releasing as `blocked` parks the card for an operator, surfacing the stall reason directly.
- The stall message explicitly itemizes tool usage:
  `Explored {N} iterations without writing ({tool_summary})` (e.g. `Explored 25 iterations without writing (25 read_file)`).

### 3. Interplay with Card #16 (Pre-Attempt Satisfaction)
- Card #16 pre-attempt satisfaction evaluation runs **before** the coding loop begins.
- If a card's goal is already satisfied and baseline gates pass, the worker finalizes a `justified_no_change_completion` with outcome `succeeded` and an explicit no-commit explanation.
- Legitimate no-change cards never enter the iterative tool loop and are therefore never falsely blocked by stall detection.

### 4. Software vs. Task Scoping
- Write-based stall detection is strictly scoped to software coding cards (`card.kind == "software"`).
- Non-software cards (e.g. planning, task drafts) operate under the task evidence contract and are not blocked for omitting code writes.

## Artifact Locations

Scripted provider fixture samples are committed under `eval/s4/stall-without-write-samples/`:

| Sample | Case Description | Outcome | Release Status | Reconstructed Tools |
|---|---|---|---|---|
| `eval/s4/stall-without-write-samples/case-stall-blocked` | Explored 25 iterations without writing | `blocked` | `acknowledged` | 25 `read_file`, 0 writes |
| `eval/s4/stall-without-write-samples/case-justified-no-change-succeeds` | Card #16 path: goal satisfied before coding | `succeeded` | `acknowledged` | 0 tool calls (loop avoided) |
| `eval/s4/stall-without-write-samples/case-healthy-read-then-write` | 20 reads, then `write_file` and `verify_build` | `succeeded` | `acknowledged` | 20 `read_file`, 1 write, 1 verify |

All three samples carry:
- `evidence_class: "scripted_provider_fixture"`
- `real_model_evidence: {"status": "unavailable", "reason": "no_real_model_run_for_card_17"}`

## Offline Validation Commands

Run records are validated offline using `scripts/run-record.py`:

```sh
# 1. Validate stall-blocked run record
.venv/bin/python scripts/run-record.py validate eval/s4/stall-without-write-samples/case-stall-blocked

# 2. Validate justified no-change success run record (#16 interoperability)
.venv/bin/python scripts/run-record.py validate eval/s4/stall-without-write-samples/case-justified-no-change-succeeds

# 3. Validate healthy read-then-write run record
.venv/bin/python scripts/run-record.py validate eval/s4/stall-without-write-samples/case-healthy-read-then-write

# Output machine-readable JSON:
.venv/bin/python scripts/run-record.py validate eval/s4/stall-without-write-samples/case-stall-blocked --json

# Generate human-readable review reports:
.venv/bin/python scripts/run-record.py review eval/s4/stall-without-write-samples/case-stall-blocked
.venv/bin/python scripts/run-record.py review eval/s4/stall-without-write-samples/case-justified-no-change-succeeds
.venv/bin/python scripts/run-record.py review eval/s4/stall-without-write-samples/case-healthy-read-then-write
```

### Validator Exit Codes

- `0`: Valid run record. Succeeded with verified acceptance, continued with legitimate criteria failure, or blocked with justified stall rationale.
- `2`: Validation failure:
  - Blocked outcome without required stall rationale ("without writing").
  - Succeeded outcome with failing acceptance criteria or missing no-commit explanation.
- `3`: Incomplete capture (dropped events, missing finalization) or leaked secrets detected.
