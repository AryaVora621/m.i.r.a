# M.I.R.A. Phase 1 Resume Audit

> This audit is preserved for history. The approved canonical implementation plan is [2026-08-19-mira-custom-voice-agent-os-plan.md](docs/superpowers/plans/2026-08-19-mira-custom-voice-agent-os-plan.md). The older narrow Task 2 resume sequence is superseded by that plan.

**Status:** Audit complete, awaiting one architecture decision  
**Active branch:** `feat/macbook-offline-core-sim`  
**Roadmap source:** `docs/superpowers/plans/2026-08-17-mira-macbook-offline-development-plan.md`

## Outcome

Resume from the verified shared-contract foundation without building the Rust daemon against underspecified wire protocols.

## Current Reality

- Task 1 is implemented and verified with 8 passing tests.
- No `daemon/`, `web/`, `vision/`, or `nodes/simulator/` implementation exists yet.
- The active 16-task document is a useful roadmap, but its tasks are implementation outlines rather than executable TDD briefs.
- Task 2 assumes event registration and routing contracts that Task 1 did not define.
- The networking spec requires authenticated nodes, while the MacBook-only plan does not say whether localhost development is exempt.

## Resume Sequence

1. **Lock the Phase 1 trust boundary**
   - Recommended: bind only to `127.0.0.1` and permit unauthenticated local clients.
   - Require token authentication before any LAN binding, mDNS advertisement, or physical node pairing.

2. **Harden the shared WebSocket contract**
   - Add a JSON Schema for `MiraEventEnvelope` matching the TypeScript and Python models.
   - Define client registration, registration acknowledgement, protocol error, and node-disconnected messages.
   - Define required node identity, role, protocol version, topic subscriptions, and connection lifecycle.
   - Add cross-language fixture tests so JSON Schema, TypeScript shapes, and Pydantic models stay aligned.

3. **Rewrite Task 2 as an executable TDD plan**
   - Specify exact Rust public types and module responsibilities.
   - Define routing behavior: sender echo, topic filtering, controller route behavior, lag handling, malformed JSON, duplicate node IDs, and disconnect cleanup.
   - Test the Axum router on an ephemeral port rather than assuming port `8080` is free.
   - Pin compatible dependency versions and document the minimum supported Rust version.

4. **Implement one thin vertical slice**
   - Start daemon.
   - Register two simulated local clients.
   - Publish one validated event.
   - Verify the subscribed peer receives it and the registry removes disconnected clients.

5. **Gate the remaining roadmap**
   - Re-plan Tasks 3 through 16 immediately before execution, one subsystem at a time.
   - Keep the current document as the roadmap, not as a copy-paste implementation contract.
   - Update `PROJECT_STATUS.md`, `.superpowers/sdd/progress.md`, and this checkpoint after each accepted task.

## Audit Findings

### Must Fix Before Task 2

- There is no JSON Schema for the universal WebSocket envelope.
- There is no registration or protocol-error schema.
- Node Registry fields and connect/disconnect behavior are unspecified.
- Broker delivery semantics and validation failures are unspecified.
- The auth requirement conflicts with the offline plan unless a localhost exemption is explicit.

### Fix During Phase 1

- Active docs alternate between `Jarvis` and `M.I.R.A.` naming.
- The roadmap names dependencies but does not pin versions or record support floors.
- Later tasks depend on APIs that are not yet produced by earlier tasks.
- The `.superpowers/` work ledger is local-only and not part of the committed handoff.

## Done Criteria For The Next Planning Unit

- The trust boundary is explicitly approved.
- All broker wire messages have schemas and typed representations.
- Task 2 contains exact interfaces, red-green tests, expected failures, and verification commands.
- The user reviews the revised protocol design before implementation starts.
