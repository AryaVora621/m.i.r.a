# M.I.R.A. Context-Clear Start Prompt

Paste this after a context clear:

```text
We are continuing work in /Users/aryavora/Desktop/Personal Projects/mira.

Read AGENTS.md, then run ls. Read TASK_QUEUE.md and CHECKPOINT_LAST.md (and BLOCKED.md if
present). The canonical approved plan is:
docs/superpowers/plans/2026-08-19-mira-custom-voice-agent-os-plan.md

Current state (as of 2026-08-19):
- Implementation Sequence steps 1-2 are done and merged to main: protocol v2 contracts
  (MiraEnvelopeV2, CapabilityRequest, ActionManifest, CapabilityReceipt) across JSON
  Schema/Pydantic/TS/Rust, the Rust daemon (WebSocket gateway + node registry, loopback-only
  trust enforced), and the guardrail (deny-by-default PolicyTable + Ed25519 dev signing), wired
  end to end over a Unix Domain Socket. 25/25 Rust tests + 12/12 Python tests + tsc --noEmit
  clean. main is ahead of origin/main and not pushed (push only if explicitly asked).
- Implementation Sequence step 3 (the Mac voice loop) has a written, self-reviewed plan ready to
  execute, not yet built:
  docs/superpowers/plans/2026-08-19-mira-voice-loop-plan.md (Tasks 7-15)
  It was spot-verified against a real build before being finalized (guardrail volume handler:
  built and tested clean in a scratch worktree; VoiceLoop FSM: ran all 7 tests including 20
  clean runs of the barge-in test) — three real bugs were found and fixed during that
  verification, so the plan as written should execute cleanly.
- Nothing in voice/ or MIRA_IDENTITY.md exists in the working tree yet — that's this plan's job.

Important decisions already locked:
- Build M.I.R.A. fully custom. Hermes and OpenClaw are reference-only.
- MacBook first: local "Mira" wake word, local STT/TTS, Codex OAuth (dev route, via the local
  `codex exec` CLI) answers, a real (not simulated) Mac volume capability, permanent identity,
  in-session memory. Persistent cross-restart memory, a Tauri menu-bar UI, and the full Agent OS
  pipeline are later Implementation Sequence steps, not this one.
- The guardrail binary, signing keys, and irreversible-action policy are untouchable by agents.
  Only reversible actions with a tested rollback may run autonomously.
- Exact P40 model targets: Qwen3.8-27B and NVIDIA Nemotron 3.5 Lightning 30B-A3B. Do not
  substitute other models. Audit and reuse the existing Ubuntu/Ollama box first — do not wipe
  without evidence and explicit approval.

Next concrete action: execute docs/superpowers/plans/2026-08-19-mira-voice-loop-plan.md,
Tasks 7-15, via superpowers:subagent-driven-development (fresh implementer per task,
task-scoped review) — same pattern used for the completed gateway/guardrail plan. Create branch
feat/mira-voice-loop first, per that plan's Global Constraints. The plan's final manual
end-to-end verification step (Task 15) needs a live mic/speaker and the real, OAuth-authenticated
codex CLI — it can't be done by a subagent or in CI, it needs the user physically at the machine.

Start with a read-only current-state inventory (git status, git log -3) to confirm nothing has
drifted since this prompt was written, then begin Task 7. Use TDD, update PROJECT_STATUS.md,
TASK_QUEUE.md, and CHECKPOINT_LAST.md after each meaningful unit, and verify before claiming
completion.
```
