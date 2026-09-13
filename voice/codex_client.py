"""Dev-route Codex turn: shells out to the local, OAuth-authenticated `codex` CLI.

This is the development route named in the canonical plan (the Mac Ollama registry is empty),
not a replacement for the exact P40 models. Response latency is a few seconds — the caller must
be in the THINKING voice-loop state while this runs.

Runs with --ignore-user-config and cwd set to a throwaway temp dir. Without both: `codex exec`
picks up ~/.codex/config.toml's `model_reasoning_effort = "high"` plus its full MCP server list
(several of which fail auth and retry), and running from this repo's own directory makes it read
AGENTS.md and treat a one-line voice reply as a "resume coding work" task — together these turned
"set volume to 32" into a 90+ second autonomous agent session that blew through a 30s timeout
instead of a quick spoken acknowledgment. With both flags, measured latency is ~10-25s, still
well above true chat-completion speed: every call re-reads a machine-wide "i-have-adhd" skill
file (a real subprocess+model round trip each time) that isn't gated by --ignore-user-config and
has no CLI flag to suppress — `--disable plugins` looked promising but broke codex's network
transport entirely (see this plan's Task 15 verification notes), so it's not a usable workaround.
The timeout below has margin for this variance; it will shrink to true API latency once the P40
route replaces this dev-route CLI shim.
"""

import subprocess
import tempfile
from pathlib import Path


class CodexError(Exception):
    pass


class CodexClient:
    def __init__(self, binary: str = "codex", timeout_s: float = 45.0) -> None:
        self._binary = binary
        self._timeout_s = timeout_s

    def generate(self, system_prompt: str, history: list[dict[str, str]], user_text: str) -> str:
        prompt = self._build_prompt(system_prompt, history, user_text)
        with tempfile.TemporaryDirectory() as tmp_dir:
            reply_path = Path(tmp_dir) / "reply.txt"
            try:
                result = subprocess.run(
                    [
                        self._binary,
                        "exec",
                        "--ignore-user-config",
                        "--skip-git-repo-check",
                        "--sandbox",
                        "read-only",
                        "--ephemeral",
                        "-o",
                        str(reply_path),
                        prompt,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_s,
                    cwd=tmp_dir,
                )
            except subprocess.TimeoutExpired as exc:
                raise CodexError(f"codex exec timed out after {self._timeout_s}s") from exc

            if result.returncode != 0:
                raise CodexError(f"codex exec exited with {result.returncode}: {result.stderr.strip()}")

            if not reply_path.is_file():
                raise CodexError("codex exec produced no reply file")

            return reply_path.read_text(encoding="utf-8").strip()

    @staticmethod
    def _build_prompt(system_prompt: str, history: list[dict[str, str]], user_text: str) -> str:
        lines = [system_prompt, ""]
        for turn in history:
            lines.append(f"{turn['role']}: {turn['content']}")
        lines.append(f"user: {user_text}")
        return "\n".join(lines)
