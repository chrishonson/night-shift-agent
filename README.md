# Night Shift: local coding worker

Night Shift takes a bounded software task, edits an explicitly selected repository,
runs its verification contract, and returns local changes with diagnostic evidence.
The coordinator owns planning, task selection, human review, publication and deployment.
This worker is not a general planning assistant, model-training system or evaluation platform.

## Supported local version

Use `/Users/nick/git/night-shift-supported`, branch `codex/night-shift-supported`.
Older worktrees are evidence, not alternative launch locations. This branch starts
from card-17's combined worker at `47a050f` and preserves cards 50 and 16.
Lane removal is deliberately deferred until the deployed board interface is compatible.

## Install and verify

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/run-gate.py worker_tests
.venv/bin/python scripts/run-gate.py worker_syntax
```

## Run one board card

Use a clean, isolated target checkout whose directory name matches the card's repo.
It must contain `verification.json` and `scripts/run-gate.py`, with the card's declared
gates. Test/build commands, interpreter, SDK selection and coverage floors belong to
that repository. There are no implicit Gradle commands. KMP repositories declare
Gradle/Xcode gates; other repositories declare their own commands.

```sh
cd /Users/nick/git/night-shift-supported
FORCE_PROVIDER=ollama OLLAMA_MODEL='<already-installed-model>' \
NIGHT_SHIFT_RECORD_DIR='/Users/nick/git/.night-shift-records' \
.venv/bin/python agent_night_shift.py \
  --project-dir '/absolute/path/to/isolated/repository-name' \
  --repos repository-name --max-runs 1 --until-empty
```

Choose the model explicitly. `ollama` uses only local inference. Other existing
headless adapters may fall back to Ollama; consult `ProviderManager` before choosing
a chain. No quota scheduler or automatic model-tier promotion is supported.
Board credentials use the existing Secret Manager/identity configuration. Never
put credentials into URLs or task files. Scope the worker identity to software repos;
non-software cards received accidentally are returned blocked for a coordinator.

The runner does not automatically push, create a PR or monitor remote CI. Shell tools
are not a security sandbox: run only trusted bounded tasks in an appropriate local
execution environment. Publication and credential operations are outside this worker's
supported purpose.

## Records and completion

Each board attempt writes an independent record directory with manifest, ordered
redacted diagnostic events and patch. Default capture is metadata-only; payloads are
not retained. Use `NIGHT_SHIFT_CAPTURE_MODE=payload` only when full context capture is
appropriate for the target's data. Missing acceptance evidence is not a quality pass.

```sh
.venv/bin/python scripts/run-record.py validate /path/to/record
.venv/bin/python scripts/run-record.py review /path/to/record
```

A passing gate is build/test evidence. A no-change success additionally requires
explicit goal-specific acceptance criteria. Stalls return blocked rather than
repeatedly consuming attempts. Inspect the record and diff before accepting a result.
A lost release acknowledgement is recorded as unknown; reconcile the board before
retrying. Never overwrite prior records to make a failed attempt look successful.

## Archived experiments

Context decision probes, board-reconciliation benchmarks and frozen research datasets
are outside the supported checkout, in:
`/Users/nick/git/.priority-intake/2026-09-16-night-shift-consolidation/experiments/`.
The adjacent `pre-consolidation.bundle` preserves their full original source/history.
Existing run-record fixtures remain regression tests of recording and coding safeguards;
they are not a benchmark or a training dataset program.

See `docs/CONSOLIDATION.md` for provenance, evidence and outstanding integration decisions.
