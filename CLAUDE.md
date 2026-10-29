# Night Shift contributor instructions

Read README.md for the supported scope, launch command and verification gates.
Implement bounded software work only. Planning, board grooming, publication and
credential changes belong to the coordinator. Do not reintroduce research modules
as runtime dependencies or hardcoded target-repository build commands.

Keep diagnostic recording, goal-specific no-change checks, and stall handling.
Do not weaken gates or silently turn missing evidence into success. Preserve the
historical experiment bundle and prior run artifacts. Work locally; no push or deployment.

## Definition of done

A change is done only when all three hold:

1. **Merged to main.** Nothing for it is left on another branch or checked out
   locally.
2. **Deployed and running where it is used**, with the deployed revision
   recorded.
3. **Tested.** This repository's gates pass on main, and a check against the
   deployed system exercises the change.

Merged but not deployed, or passing only on a branch, is not done.

There is no deploy step. The worker runs from the main checkout at
`/Users/nick/git/night-shift-agent`, so a change reaches it when that checkout
pulls main and the next run starts. The coordinator does the merge and the pull;
the worker itself never pushes.

Commands in this repository:

| Step | Command |
|---|---|
| Merge | `gh pr merge <number> --merge`, then `git -C /Users/nick/git/night-shift-agent pull --ff-only` on main |
| Gates | `.venv/bin/python scripts/run-gate.py worker_tests`, `.venv/bin/python scripts/run-gate.py worker_syntax` |
| Deploy | None. The pull above is the deploy. Stop a running worker with `scripts/stop_agent.sh` before pulling, and start the next run with `scripts/start_agent.sh /absolute/path/to/isolated/repository` |
| Deployed revision | `git -C /Users/nick/git/night-shift-agent rev-parse HEAD` |
| Post-deploy check | Run one board card from that checkout (see "Run one board card" in README.md), then `.venv/bin/python scripts/run-record.py review /path/to/record` on its record. `scripts/check_agent.sh` shows whether a worker is running. |

Record the merge commit, the revision and the check on the card before closing it.
