"""Integration test against the real Rust daemon+guardrail, mirroring
daemon/tests/capability_dispatch_test.rs from the Python side. Requires `cargo build` to have
already produced target/debug/mira-daemon and target/debug/mira-guardrail."""

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from voice.daemon_client import DaemonClient

REPO_ROOT = Path(__file__).resolve().parents[2]
DAEMON_BIN = REPO_ROOT / "target" / "debug" / "mira-daemon"
GUARDRAIL_BIN = REPO_ROOT / "target" / "debug" / "mira-guardrail"


def _port_open(host, port, timeout=0.2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def daemon_stack():
    if not DAEMON_BIN.exists() or not GUARDRAIL_BIN.exists():
        pytest.skip("mira-daemon/mira-guardrail not built — run `cargo build` first")

    with tempfile.TemporaryDirectory() as tmp_dir:
        sock_path = Path(tmp_dir) / "guardrail.sock"
        key_path = Path(tmp_dir) / "guardrail.key"
        env = dict(os.environ, MIRA_GUARDRAIL_SOCK=str(sock_path), MIRA_GUARDRAIL_KEY=str(key_path))

        guardrail_proc = subprocess.Popen([str(GUARDRAIL_BIN)], env=env)
        daemon_proc = subprocess.Popen([str(DAEMON_BIN)], env=env)
        try:
            for _ in range(50):
                if _port_open("127.0.0.1", 8080):
                    break
                time.sleep(0.1)
            else:
                pytest.fail("mira-daemon did not open port 8080 in time")
            yield
        finally:
            daemon_proc.terminate()
            guardrail_proc.terminate()
            daemon_proc.wait(timeout=5)
            guardrail_proc.wait(timeout=5)


def test_register_and_volume_capability_round_trip(daemon_stack):
    client = DaemonClient(node_id="voice-test-node")
    client.connect()
    assert client.is_connected()

    client.publish_state("IDLE")  # must not raise

    receipt = client.request_capability("mac.volume.set", {"level": 33})
    assert receipt["result_status"] in ("SUCCESS", "FAILURE")  # FAILURE is valid on a non-macOS CI runner
    assert receipt["capability"] == "mac.volume.set"

    client.close()
