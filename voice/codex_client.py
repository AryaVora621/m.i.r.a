"""Dev-route Codex turn: shells out to the local, OAuth-authenticated `codex` CLI.

This is the development route named in the canonical plan (the Mac Ollama registry is empty),
not a replacement for the exact P40 models. Response latency is a few seconds — the caller must
be in the THINKING voice-loop state while this runs.
"""

import subprocess
import tempfile
from pathlib import Path


class CodexError(Exception):
    pass


class CodexClient:
    def __init__(self, binary: str = "codex", timeout_s: float = 30.0) -> None:
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
