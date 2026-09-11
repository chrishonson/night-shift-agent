# Card #49 Rollout Note: Decouple Lanes from Dispatch & Client

## 1. Summary of Changes
- **Core Abstraction Removal**: Removed the `cloud-linux` / `cloud-macos` / `local` runner lane concept from `control-plane` dispatch and the `night-shift-agent` client. Lane placement was identified as unnecessary GitHub Actions conceptual baggage.
- **Card Schema & Creation**:
  - `placement` field on cards (`Card`, `CreateCardInput`, `DecomposeChildInput`) is now optional.
  - Card creation (`card_create`) and decomposition (`card_decompose`) no longer require or validate `placement`.
  - Empty or absent placements are permitted.
- **Claim & Dispatch**:
  - `ClaimRequest.lane`, `Lease.lane`, `RunEvent.lane` are now optional.
  - Removed lane-based identity permission checks (`identity.lanes.includes(lane)`).
  - Removed lane filtering (`placement.includes(lane)`) in claim eligibility evaluation (`claimPolicy.ts`). Work is dispatched purely on physical resource availability (`requires`), repository scoping (`repos`), dependencies (`blocked_by`), lease state, priority, and attempt budgets.
  - Removed lane parameters and queries in `workRepository.ts` (`readCandidates` queries candidates by state and priority/updated_at without placement array-contains filtering).
- **Verification Gates**:
  - Gates in `verification.json` (`controlplane_typecheck`, `controlplane_test`, `controlplane_emulator`) had their obsolete `"placement"` arrays removed while preserving all verification commands and `requires`.
  - Gate runner (`run-gate.py` and `test_run_gate.py`) now executes and tests gate commands independently of dispatch lane.
- **Board UI & Observer**:
  - Removed lane ribbon counters from board snapshot (`BoardSnapshot.lanes`).
  - Removed lane dropdown filter (`#filter-lane`) and placement input fields from the mobile live board HTML and renderer (`src/boardPage.ts`, `scripts/render-board.mjs`).
  - Identity seed script (`scripts/seed-identity.mjs`) no longer requires `--lanes`.
- **Night Shift Agent Client**:
  - `ControlPlaneClient.claim(resources, repos, lane)`: `args` no longer includes `lane` by default unless explicitly supplied.
  - `NightShiftAgent.run_control_plane()`: operates without lane constraints, claiming ready work matching local hardware resources.
  - `DEFAULT_LANE` and CLI `--lane` flag are deprecated.

## 2. Backward Compatibility & Migration
- **Existing Cards**: Cards created prior to Card #49 with legacy `placement` arrays (`["cloud-linux"]`, `["local"]`, etc.) remain fully valid and claimable by any worker whose resources and repo scope permit.
- **Existing Identities**: Identities with legacy `lanes` arrays (e.g. `["cloud-linux"]`) continue to authenticate and claim work normally without being restricted to those lanes.
- **Active Leases & Historical Runs**: Leases and run events recorded with `lane` values can still be released, audited, and reviewed without error.

## 3. What Must NOT Be Done Yet
- **DO NOT drop legacy Firestore indexes**: Existing composite indexes in `firestore.indexes.json` that include `placement (ARRAY)` must NOT be dropped in this change to avoid disrupting running environments or older client versions.
- **DO NOT deploy Firebase functions or rules**: Production deployment is out of scope for this card and must only be executed through authorized release channels.
- **DO NOT rotate credentials or identity tokens**.
- **DO NOT push to origin or open pull requests**.

## 4. Verification After Future Authorized Deployment
Once authorized to deploy:
1. Verify Firebase deployment:
   ```bash
   firebase deploy --only functions,firestore:indexes
   ```
2. Check MCP Server endpoint via curl or MCP client:
   - Call `tools/list` and verify schema descriptions for `card_create` and `card_claim` reflect optional placement / no lane requirements.
   - Call `card_create` without `placement`: verify HTTP 200 and ready card returned.
   - Call `card_claim` without `lane`: verify card is claimed with valid `lease` and `run_id`.
   - Call `card_release`: verify release succeeds and card transitions to `done`.
3. Verify Mobile Live Board:
   - Navigate to `/board`: verify UI renders cleanly without lane filter dropdown and displays cards across states.
