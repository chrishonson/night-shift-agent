# Night Shift consolidation handoff — updated 2026-09-17

Production release update (20:13 UTC): Nick authorized deployment of Control Board
source `649e1b2`. Both functions updated successfully and are ACTIVE:
`controlplanemcp-00024-feq`, `controlplaneleasereaper-00004-rox`.
Post-release board and feedback HTTP/MCP checks passed. #57 is ready with no
blockers, no lease and zero attempts; claimed count is zero. This supersedes the
historical deployment-deferral statements below. Git publication is still deferred.

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

- Deployed revision: `controlplanemcp-00023-nif`, released 2026-09-16T21:25:19Z
  from local main `543f70d` (lane removal). Remote main was independently checked
  on September 17 and remains `636fd89`. Publication is explicitly deferred under
  the existing no-push constraint; this is local closure, not a claim of remote parity.
- `controlPlaneLeaseReaper` runs in production on a five-minute schedule, deployed
  2026-09-08. Its scheduler export, ClaimService method, and REAP_LIMIT configuration
  were restored from checkpoint `1d84401` to local main `649e1b2` during September 17 closeout,
  preserving lane removal. Production already runs this function; no redeploy is
  needed for source recovery. Build, typecheck, 306 unit tests and 373 full-suite
  emulator tests pass, with 90.3% branch coverage. See
  `/Users/nick/git/control-plane/docs/CONSOLIDATION-CLOSEOUT.md`.
- Card feedback works in production on completed cards, verified by writing and
  reading back through the MCP tool and through `POST/GET /board/cards/:id/feedback`,
  the route the board UI calls.
- Historical branches are explicitly dispositioned:
  `agent/card-49-remove-lane` (now merged, see below), `agent/reconciliation-primitive-and-s4`
  at `0f52d34` (optional `card_reconcile` primitive, deferred; not required for Study Coach),
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

## Other work and dispositions

- **Lane removal (card 49) shipped 2026-09-16**, after this handoff was first
  written. Merged to control-plane main and deployed as `controlplanemcp-00023-nif`.
  The deployed board no longer shows lanes anywhere, and `board_snapshot` has no
  `lanes` block. `card_claim` still accepts an optional lane and `card_create` still
  accepts an optional placement, so the supported worker needed no change and a
  lane-sending claim was accepted after the release.
- **Model-tier escalation (card 18).** Backlog. Its blockers (50, 17) are done, so
  it is merely unstarted, not stuck.
- **Board observability routine (card 21)** and **flashy-card contact session (card 20)**.
  Card 20's dead blocker (abandoned card 14) was cleared; the JDK prerequisite behind
  it is still unmet.
- **Two pending escalations**, both requiring Nick: `esc_8ktki1nF7u9IxPciJBlZ`
  (card 42, PAT rotation, irreversible, and step one cannot be delegated) and
  `esc_KfYOlsEzU85OR0KOlKmS` (card 27, portfolio refactor, budget exhausted).

## Known limitations

- Nick confirmed the feedback UI works. Independent readback on September 17 found
  both admin comments on #57: `2vfXKkz90wwR6NqcUXan` and `OaFDijGo9yfWdxGDxAqr`.
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

The September 16 lane-removal release was deployed by the intervening work.
September 17 closeout adds local source recovery and documentation only, with no
push, deployment, credential rotation or production worker attempt. Publication of
the consolidated worker and Control Board commits remains explicitly deferred.
The live board readback has 56 cards: 4 backlog, 1 ready (#57), 0 claimed, 2 blocked,
35 done and 14 abandoned. No further infrastructure work is required to begin #57.
