# M.I.R.A. Context-Clear Start Prompt

Paste this after a context clear:

```text
We are continuing work in /Users/aryavora/Desktop/Personal Projects/mira.

Read AGENTS.md, then run ls. Read TASK_QUEUE.md, CHECKPOINT_LAST.md, and any BLOCKED.md. The canonical approved plan is:
docs/superpowers/plans/2026-08-19-mira-custom-voice-agent-os-plan.md

Important decisions are already locked:
- Build M.I.R.A. fully custom. Hermes and OpenClaw are reference-only.
- Preserve shared-schema commit aa8bc51 and branch feat/macbook-offline-core-sim.
- MacBook first: local “Mira” wake word, local STT/TTS, Codex OAuth answers, Tauri menu-bar UI, permanent memory, and a safe Mac capability.
- Agent OS: LangGraph plus PostgreSQL checkpoints, Docker-isolated workers, local planner, Codex or Claude implementer, independent cross-model verifier, signed evidence, score >=90, staged branch promotion, canary deployment, and automatic rollback.
- Untouchable guardrail blocks irreversible or unknown actions and cannot be edited by agents.
- Exact P40 model targets: Qwen3.8-27B and NVIDIA Nemotron 3.5 Lightning 30B-A3B. Do not substitute other models.
- Audit and reuse Ubuntu/Ollama first. Do not wipe without evidence and explicit approval.

Start with a read-only current-state inventory and reconcile it with the canonical plan. Do not implement until the next concrete task is identified. When implementation begins, use TDD, preserve unrelated changes, update PROJECT_STATUS.md, TASK_QUEUE.md, and CHECKPOINT_LAST.md after each meaningful unit, and verify before claiming completion.
```
