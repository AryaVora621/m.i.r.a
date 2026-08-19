"""Loads M.I.R.A.'s versioned identity document as the voice loop's system prompt."""

from pathlib import Path


def load_identity(path: str | Path = "MIRA_IDENTITY.md") -> str:
    identity_path = Path(path)
    if not identity_path.is_file():
        raise FileNotFoundError(f"MIRA identity file not found at {identity_path}")
    return identity_path.read_text(encoding="utf-8")
