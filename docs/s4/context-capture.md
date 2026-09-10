# S4 worker capture and early regression evidence

This slice observes the worker's existing message pruning and each call into a
provider adapter. Enable capture explicitly with `S4_CONTEXT_CAPTURE_DIR` set to
an absolute, private local directory outside any repository. It is off by default
and applies to control-plane runs. It does not change prompts, pruning, provider
selection, verification, or lease semantics. No deployed worker is enabled by
this change.

Each attempt gets a random trace ID and an exclusive mode-0600 JSONL file. Events
record hashed card/task revisions, worker/capture source hashes, ordered message value hashes and roles,
character lengths, before/after pruning, provider configuration hashes, provider
call results (status only), and local finalization. No new capture event stores
prompt text, response text, error text, tokens, leases, or tool arguments. Existing
worker logs are separate and retain their existing behavior; this is not a logging
redaction project. Hashes remain private metadata, not anonymized public data.

The first boundary is **before the provider adapter**: an adapter may reformat
messages or a hosted CLI may add/remove context. Delivery to the underlying model
and provider-internal transformations remain unknown. A provider return establishes
a returned result, not correct context use. Failover records each attempted
adapter separately. Repeated identical message values share a hash; omission
reports preserve their multiplicity. Hashes cover JSON `[role, content]` encoded
as UTF-8 with sorted keys, no separator spaces and unescaped Unicode.

This is an additive local subset of the proposed S4 contract, not its distributed
finalization service. No authenticated actor, canonical memory-source mapping,
retrieval ranking, raw source replay payload, output judgment, or learning export
is fabricated. Card association is hashed. Terminal records distinguish the
worker's outcome from release acknowledgement; a failed release is **unknown**,
not a remotely confirmed failure. Complete capture means locally readable events
with no recorded gaps, not successful work or durable server ingestion.

Capture is capped at 1 MiB and 4,096 events per attempt, reserving 2 KiB for a
terminal gap count. Overflow drops telemetry, not work. I/O failures fail open;
an absent/partial trace must be treated as missing evidence. A killed process can
leave a trace without finalization. The report rejects corrupt traces and marks
missing finalization or reported overflow incomplete. There is no cleanup daemon;
retention and deletion remain operator-managed. Do not upload traces or use them
for training automatically. All learning eligibility is false.

## Run the synthetic regression replay

From the repository, with dependencies installed in `.venv`:

```sh
.venv/bin/python scripts/context-eval.py replay --output-dir /private/tmp/s4-worker-replay
.venv/bin/python scripts/context-eval.py report /absolute/path/to/trace.jsonl
```

The replay uses the actual worker loop, message pruning, read-file tool, and
provider manager, with a scripted provider and synthetic files. It makes no model,
control-plane, or network calls and does not commit or push. One fixture fetches
`No credential rotation.`, then fetches a large irrelevant file. At a 9,000-character
budget the third call loses that fetched constraint; at 1,000,000 characters it
retains it. Both budgets are **test variants**, not recommended production settings.
The scripted provider then returns no answer. Neither run is an agent task success.

The report emits review candidates with observed omitted message hashes and
`event_observation` provenance. Relevance remains unknown in arbitrary traces.
Only the synthetic fixture declares a required source with `synthetic_definition`
provenance. Related variants share one incident group and development split; they
must not be divided between training and held-out evaluation.

This command is a worker-boundary probe and candidate generator. MetaCortex's
existing evaluator remains the retrieval comparison runner. An early dashboard
can track missing traces, pruning omissions, and failover boundaries; a decrease
in omissions alone does not establish better task outcomes. The next quality step
is reviewed source mapping plus repeated real-agent runs on frozen task cases,
with output/goal judgments and the same corpus available to MetaCortex and
self-hosted open-source Onyx. No Unblocked or paid SaaS is required.

## Verification

```sh
.venv/bin/python scripts/run-gate.py worker_tests
.venv/bin/python scripts/run-gate.py worker_syntax
```

The repository gate declares all worker tests, including the actual-loop pruning
replay, fallback observations, opt-in behavior, release ambiguity, overflow,
crashed traces, duplicate content, and capture-failure isolation. Coverage starts
at 50%, rounded down from the observed total (54%); this is not a claim that the
legacy worker is comprehensively tested. This branch starts on current main and
does not incorporate the separate unmerged task-evidence implementation.
