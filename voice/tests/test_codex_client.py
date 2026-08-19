import stat
import textwrap

import pytest

from voice.codex_client import CodexClient, CodexError


def _write_fake_codex(tmp_path, script: str) -> str:
    fake = tmp_path / "codex"
    fake.write_text(script)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return str(fake)


def test_generate_returns_last_message_file_contents(tmp_path):
    # A real `codex exec ... -o <file> ...` writes the reply to whatever path follows -o;
    # this fake mimics that contract without touching the network or real Codex auth.
    fake_codex = _write_fake_codex(tmp_path, textwrap.dedent("""\
        #!/bin/sh
        for i in "$@"; do
          prev="$cur"
          cur="$i"
          if [ "$prev" = "-o" ]; then
            echo "pipeline works" > "$cur"
          fi
        done
        exit 0
        """))

    client = CodexClient(binary=fake_codex)
    result = client.generate(system_prompt="You are Mira.", history=[], user_text="say the test phrase")

    assert result == "pipeline works"


def test_generate_raises_codex_error_on_nonzero_exit(tmp_path):
    fake_codex = _write_fake_codex(tmp_path, "#!/bin/sh\necho 'boom' >&2\nexit 1\n")

    client = CodexClient(binary=fake_codex)
    with pytest.raises(CodexError, match="exited with"):
        client.generate(system_prompt="You are Mira.", history=[], user_text="hello")


def test_generate_raises_codex_error_on_timeout(tmp_path):
    fake_codex = _write_fake_codex(tmp_path, "#!/bin/sh\nsleep 5\n")

    client = CodexClient(binary=fake_codex, timeout_s=0.2)
    with pytest.raises(CodexError, match="timed out"):
        client.generate(system_prompt="You are Mira.", history=[], user_text="hello")
