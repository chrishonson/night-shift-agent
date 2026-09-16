# Night Shift consolidation handoff — 2026-09-16

Consolidation is closed. What follows is everything needed to run the worker, find
the evidence, and start the next piece of work without reconstructing branch history.

## Supported worker

- Checkout: `/Users/nick/git/night-shift-supported` (worktree of `night-shift-agent`)
- Branch: `codex/night-shift-supported`
- Base: `47a050f` (local integration #53, recorder #50, no-change #16, stall #17)
- Purpose: execute one bounded software task in an explicitly selected local
  repository, run that repository's declared verification gates, and return local
  changes plus a diagnostic record. Nothing else is supported.

Launch:

```sh
cd /Users/nick/git/night-shift-supported
FORCE_PROVIDER=ollama OLLAMA_MODEL='<already-installed-model>' \
NIGHT_SHIFT_RECORD_DIR='/Users/nick/git/.night-shift-records' \
.venv/bin/python agent_night_shift.py \
  --project-dir '/absolute/path/to/isolated/repository-name' \
  --repos repository-name --max-runs 1 --until-empty
```

The target repository must contain `verification.json` and `scripts/run-gate.py`
declaring the card's gates. There are no implicit build commands; a target without
a contract gets an error, not a guessed Gradle invocation. Operational detail
(limits, logs, recovery, unsupported task types) is in `README.md`.

## Board

- Deployed revision: `controlplanemcp-00022-fog`, updated 2026-09-14T14:42:23Z,
  which postdates main `636fd89`. Local `control-plane` checkout is clean and level
  with `origin/main`.
- Card feedback works in production on completed cards, verified by writing and
  reading back through the MCP tool and through `POST/GET /board/cards/:id/feedback`,
  the route the board UI calls.
- Three local-only branches remain unmerged and are each explicitly dispositioned:
  `agent/card-49-remove-lane` (deferred, see below), `agent/reconciliation-primitive-and-s4`
  at `0f52d34` (a `card_reconcile` MCP primitive with tests, never integrated),
  and `checkpoint/mcp-query-token-wip` at `1d84401` (a working-tree checkpoint kept
  during the card-52 reconcile, not a feature).

## Experiment archive

`/Users/nick/git/.priority-intake/2026-09-16-night-shift-consolidation/`

- `pre-consolidation.bundle` — verified standalone bundle of the worker repository
  as it stood before consolidation. This is the restore point.
- `experiments/` — decision probes, board-reconciliation benchmarks, their frozen
  datasets, the retired replay and task-drafting tests, with `README.md` giving
  source commits, dependencies, commands, recorded results and limitations.
- `smoke/` — the real-model verification of the consolidated branch.
- Cards 45, 46 and 47 carry board feedback pointing at these locations.

## Verification evidence

| Check | Result |
|---|---|
| `worker_tests` gate | 141 passed, 72.8% coverage (floor 55%) |
| `worker_syntax` gate | exit 0 |
| Real-model coding task on this branch | succeeded, 118.9s, Ollama `qwen3.8:27b` |
| Declared gate in the target | `unit_tests` passed |
| Independent acceptance, outside the workspace | exit 0 |
| Run record | complete, `run-record.py validate` exit 0 |

Task success and evidence capture are recorded separately in
`smoke/meta/run-summary.json`. See `smoke/REVIEW.md` for what that run does and
does not certify.

## Deliberately deferred

- **Lane removal (card 49).** Implemented at `fcfbf09` on `agent/card-49-remove-lane`,
  not merged and not deployed, so lanes are still visible in production. Merged onto
  current main for verification at `bdf1801` on `integration/card-49-on-main`
  (`/Users/nick/git/control-plane-card49`): clean merge, build and typecheck clean,
  301 tests pass. The branch keeps `lane` optional on `card_claim` and `placement`
  optional on cards, so a board deploy is backward compatible with the current
  worker and does not require a simultaneous worker release. Two gaps before it
  ships: the card detail dialog still renders a "Placement Lanes" section fed from
  `card.placement`, so that branch alone does not remove lanes from view, and the
  branch carries a stray `ROLLHOUT-card-49.md` beside `ROLLOUT-card-49.md`.
- **Model-tier escalation (card 18).** Backlog. Its blockers (50, 17) are done, so
  it is merely unstarted, not stuck.
- **Board observability routine (card 21)** and **flashy-card contact session (card 20)**.
  Card 20's dead blocker (abandoned card 14) was cleared; the JDK prerequisite behind
  it is still unmet.
- **Two pending escalations**, both requiring Nick: `esc_8ktki1nF7u9IxPciJBlZ`
  (card 42, PAT rotation, irreversible, and step one cannot be delegated) and
  `esc_KfYOlsEzU85OR0KOlKmS` (card 27, portfolio refactor, budget exhausted).

## Known limitations

- The feedback UI is verified at the API and served-markup level. No browser session
  drove the form itself; Chrome computer-use permission was not granted.
- The smoke run used a synthetic card through the real execution wiring, so
  production claim, heartbeat and release were not re-exercised on this branch.
- `night-shift-01` is still scoped `repos: ["*"]` on the board. Scope is enforced
  worker-side instead: the card's repo must match `--project-dir`, non-software
  cards return blocked for a coordinator, and an empty `--repos` falls back to the
  selected repository rather than claiming the whole board. Narrowing the identity
  needs an admin reseed, which mints a new token.
- No KMP or Gradle target was exercised on the consolidated branch.

## Next work

Card 57, "Study Coach: one useful mobile voice session with saved progress", is on
the board in `ready`. It is a coordinator-owned milestone: establish the product
repository and its verification gates before decomposing software cards. Night Shift
is optional for it, and the board can dispatch other coding agents just as well.

The no-push, no-deployment constraint is still in force. Everything above was
prepared and verified locally. Board data written during verification is limited to
card feedback and one cleared dead blocker reference.
