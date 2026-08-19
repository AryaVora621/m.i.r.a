# M.I.R.A. Task Queue

## Open

- [ ] Define and execute the voice loop subplan (Implementation Sequence step 3 of the canonical plan): local "Mira" wake word, local STT/TTS, Codex OAuth answers. Natural next step — depends on nothing the gateway/guardrail plan doesn't already produce.
- [ ] Follow-up-plan tickets deferred from the gateway/guardrail whole-branch review (see `PROJECT_STATUS.md`'s "Deferred From the Gateway/Guardrail Plan" section for the full list): sender_node identity spoofing on the broadcast path, non-canonical/Rust-only-verifiable receipt signing, Rust protocol mirror laxity (no deny_unknown_fields, unvalidated protocol_version), CapabilityReceipt missing an authorized-vs-executed distinction, guardrail accept()-panics-the-process, unbounded capability-dispatch mpsc, signing-key file permissions/path, dropped trace_id on receipt/error envelopes, unverified MSRV 1.75 claim. Fold into whichever plan covers LAN/production hardening (P40/mTLS plan) rather than a standalone cleanup pass, since several depend on that plan's trust-boundary decisions anyway.
- [ ] Define focused subplans for memory, UI, Agent OS, P40 deployment, and hardware nodes.
- [ ] Audit Ubuntu/P40/Ollama before any removal or migration.

## In-Progress

(none)

## Done

- [x] Task 1: Shared IPC schemas and typed contracts (`aa8bc51`).
- [x] Re-verified Task 1 on 2026-08-19: 8 unit tests pass.
- [x] Audited the repository and prepared the Phase 1 resume audit.
- [x] Saved the custom voice and Agent OS canonical plan.
- [x] Saved the context-clear start prompt.
- [x] Rebaselined against the canonical plan and wrote the executable protocol-v2/guardrail TDD plan (`docs/superpowers/plans/2026-08-19-mira-protocol-v2-gateway-guardrail-plan.md`, Tasks 2-6).
- [x] Executed Tasks 2-6 via Subagent-Driven Development: protocol v2 contracts (JSON Schema/Pydantic/TS/Rust), Rust workspace (`mira-protocol`/`daemon`/`guardrail` crates), WebSocket gateway + node registry, deny-by-default guardrail policy engine + Ed25519 dev signing, end-to-end capability dispatch over a Unix Domain Socket. Whole-branch review passed (1 fix round + 1 closure fix, both re-reviewed clean). Final commit `28d21e5`. 25/25 Rust tests + 12/12 Python tests + `tsc --noEmit` clean.
