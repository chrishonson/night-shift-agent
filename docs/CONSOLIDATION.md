# Consolidation, 2026-09-16

Purpose: one local software worker, a usable board, and a queue for Study Coach.
This is consolidation, not a new training or evaluation project.

## Provenance

- Supported base: `47a050f5f4d6fb5afd938032ca6e85c904c94c9c`, containing
  local integration #53, recorder #50, goal-specific no-change #16 and stall #17.
- Prior implementation history is preserved in the verified standalone bundle:
  `/Users/nick/git/.priority-intake/2026-09-16-night-shift-consolidation/pre-consolidation.bundle`.
- Research source/tests/data removed from the supported tree are copied to the adjacent
  `experiments/` directory. Restore the bundle at `47a050f` for the original runnable
  environment, including task-drafting and replay tests. Original archives are unchanged.
- #55 evidence at `/Users/nick/git/.priority-intake/2026-09-13-card-55-real-model/`
  reports a successful real Ollama qwen3.8:27b task, independent acceptance exit 0,
  complete valid recording, human acceptance unknown. It ran the original `47a050f`,
  not the consolidation changes, so it cannot certify this new version. A separate
  smoke run on the consolidated branch supplies that evidence:
  `/Users/nick/git/.priority-intake/2026-09-16-night-shift-consolidation/smoke/`
  (REVIEW.md, run-summary.json, record/, validator exit 0).

## Deliberate decisions

- Keep bounded redacted ContextCapture as diagnostic infrastructure. Its old name and
  `s4-run-record.v1` schema preserve compatibility; neither implies a training program.
- Keep recorder regression fixtures and validator. Remove decision probes, comparison
  benchmarks and their datasets from runtime/test dependencies.
- Non-software work returns blocked for the coordinator. No planning-model fallback.
- Explicit repository selection and verification replace mobile auto-discovery and
  implicit Gradle verification in the supported run path.
- Board attempts now use the retained full run-record wrapper, with metadata capture
  by default and optional payload capture. Automatic GitHub publication/CI repair is removed.
- Lane removal (#49) shipped as `controlplanemcp-00023-nif` on September 16.
  Optional legacy lane inputs remain compatible with the supported worker.
- Board feedback is deployed. Live MCP exposes card_feedback_add/list, the served
  `/board` page carries the feedback UI, and writes persist on completed cards
  through both the MCP tool and the REST route the UI calls.
- An empty `--repos` used to claim unscoped, which resolves against the
  `night-shift-01` identity's `*` scope and would hand this worker task cards and
  other repositories' cards. It now falls back to the selected project directory,
  with a regression test that fails without the fix.

## Completion audit

- [x] Inventory branches and inspect #55 evidence.
- [x] Preserve original source/history and separate research files, with an index
      naming source commits, dependencies, commands, results and limitations.
- [x] Narrow supported scope and document launch contract.
- [x] Verify consolidated worker gates and boundary regressions: `worker_tests`
      141 passed, 73% coverage; `worker_syntax` exit 0.
- [x] Verify one real-model run on the final combined version: succeeded in 118.9s
      on Ollama qwen3.8:27b, declared gate passed, independent acceptance exit 0,
      record complete and valid.
- [x] Verify production feedback persistence: Nick confirmed the browser UI works;
      both admin test comments on #57 were independently read back on September 17.
      Current board revision is `controlplanemcp-00023-nif`.
- [x] Archive superseded board work, defer optional work, create Study Coach milestone.
- [x] Commit supported version and write final handoff with exact evidence.

## Known limitations

- Browser interaction was verified by Nick; this agent independently verified persisted data.
- Board claim, heartbeat and release were not exercised against production by the
  smoke run, which used a synthetic card through the same execution wiring.
- The `night-shift-01` identity is still scoped `repos: ["*"]` board-side. The
  worker-side guards (repo must match `--project-dir`, non-software returns blocked,
  empty `--repos` falls back) are what keep it in scope. Narrowing the identity
  requires an admin reseed, which mints a new token.
- No KMP or Gradle target was exercised on the consolidated branch.

The September 16 continuation deployed lane removal. The September 17 closeout
restores the existing production lease-reaper source to local Control Board main;
it does not deploy or push. Publication remains explicitly deferred under the
existing constraint. See HANDOFF.md for exact revisions and verification evidence.
