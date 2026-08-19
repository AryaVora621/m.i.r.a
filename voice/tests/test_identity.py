import pytest

from voice.identity import load_identity


def test_load_identity_returns_file_contents(tmp_path):
    identity_file = tmp_path / "MIRA_IDENTITY.md"
    identity_file.write_text("# Test Identity\n\nYou are a test persona.\n")

    result = load_identity(identity_file)

    assert result == "# Test Identity\n\nYou are a test persona.\n"


def test_load_identity_missing_file_raises_clear_error(tmp_path):
    missing = tmp_path / "does_not_exist.md"

    with pytest.raises(FileNotFoundError, match="MIRA identity file not found"):
        load_identity(missing)
