# S4 Unchanged-Card Evidence and Verification Framing (Card #16)

This document describes the worker behavior, run-record schema, offline validation, and evidence artifacts for distinguishing already-satisfied goals from false short-circuits.

## Overview and Verification Framing

Card #16 prevents wasting full coding attempts on tasks where the goal is already satisfied in the repository, while strictly enforcing that **passing existing gates alone is never proof of task success**.

When an agent claims a task card, it evaluates the baseline before or at the start of a coding attempt:
1. **Case A: Goal already satisfied + passing gates → justified no-change completion**
   - Declared gates are run as a baseline and pass.
   - The card goal and acceptance criteria are explicitly evaluated against current behavior and pass.
   - The worker avoids spending a full coding attempt and makes no git commit.
   - An evaluable run record is finalized with `terminal_outcome: "succeeded"`, `pre_attempt_decision: "justified_no_change_completion"`, and an explicit `no_commit_explanation`.
2. **Case B: Passing gates + requested functionality absent → continue working**
   - Declared gates pass as baseline (existing tests remain green).
   - BUT the card goal and acceptance criteria fail against current behavior (requested functionality is absent).
   - The worker **must NOT short-circuit to success**.
   - Normal execution proceeds into the coding loop.
   - The continue-working run record captures `terminal_outcome: "continued"`, `pre_attempt_decision: "continue_working"`, and failed acceptance criteria.
3. **Gate Errors Never Imply Success**
   - If any baseline gate fails or errors, the goal cannot be declared satisfied.
   - Even if files exist or partial criteria match, gate failures require continued work or terminal failure.

## Artifact Locations

The scripted provider fixture samples are committed under `eval/s4/unchanged-card-samples/`:

| Sample | Case | Outcome | Diff Stat | Decision |
|---|---|---|---|---|
| `eval/s4/unchanged-card-samples/case-a-goal-satisfied-no-change` | Case A | `succeeded` | Clean (0 files) | `justified_no_change_completion` |
| `eval/s4/unchanged-card-samples/case-b-green-gates-missing-goal-continue` | Case B | `continued` | Clean (0 files) | `continue_working` |

Both samples carry:
- `evidence_class: "scripted_provider_fixture"`
- `real_model_evidence: {"status": "unavailable", "reason": "no_real_model_run_for_card_16"}`

## Offline Validation Commands

Using `scripts/run-record.py`:

```sh
# Validate Case A (justified no-change completion)
.venv/bin/python scripts/run-record.py validate eval/s4/unchanged-card-samples/case-a-goal-satisfied-no-change

# Output machine-readable validation JSON for Case A
.venv/bin/python scripts/run-record.py validate eval/s4/unchanged-card-samples/case-a-goal-satisfied-no-change --json

# Validate Case B (continue-working record; not short-circuited)
.venv/bin/python scripts/run-record.py validate eval/s4/unchanged-card-samples/case-b-green-gates-missing-goal-continue

# Output machine-readable validation JSON for Case B
.venv/bin/python scripts/run-record.py validate eval/s4/unchanged-card-samples/case-b-green-gates-missing-goal-continue --json

# Review reports for offline inspection
.venv/bin/python scripts/run-record.py review eval/s4/unchanged-card-samples/case-a-goal-satisfied-no-change
.venv/bin/python scripts/run-record.py review eval/s4/unchanged-card-samples/case-b-green-gates-missing-goal-continue
```

### Validator Exit Codes

- `0`: Valid run record. Outcome is `succeeded` with independent acceptance verified, or valid `continued` execution record without false short-circuit.
- `2`: Validation failure:
  - Falsely declared `succeeded` when acceptance criteria failed (short-circuit bug).
  - No-change success declared with failing gates (gate errors never imply success).
  - No-change success declared without an explicit no-commit explanation.
- `3`: Incomplete capture (dropped events, missing finalization) or leaked secrets detected.

## Offline Review Distinction

An offline reviewer can immediately distinguish between the two cases without parsing console logs:

### Case A Review Summary:
- **Terminal Outcome**: `succeeded (Release: acknowledged)`
- **Pre-Attempt Decision**: `justified_no_change_completion`
- **No-Commit Explanation**: `Goal already satisfied before coding attempt: all acceptance criteria verified against current behavior and all required gates passed. No commit required.`
- **Verification Results**: Baseline gates passed.
- **Independent Acceptance**: `Overall Passed: True` (goal-specific assertions proved).
- **Actions**: `(No tool calls: coding attempt avoided via justified no-change completion)`
- **Patch Diff**: Clean diff (0 files changed).

### Case B Review Summary:
- **Terminal Outcome**: `continued (Release: working)`
- **Pre-Attempt Decision**: `continue_working`
- **Verification Results**: Baseline gates passed (`unit_tests` passed).
- **Independent Acceptance**: `Overall Passed: False` (`greet('NightShift')` returned legacy value).
- **Actions**: Tool calls executed as coding attempt began (`read_file`).
- **Proof**: Demonstrates that green baseline gates alone did not trigger a false success.
