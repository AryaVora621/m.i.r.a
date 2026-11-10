"""The parity proof: the same scenario through mira_listen_v1 and mira_listen must give equal canonical dicts.

The live comparison (both modules, same process, same CPU) is the gate. The stored JSON recorded from v1 is a
second witness, compared only when this machine matches the one that recorded it (float bits can differ
across CPUs and numpy builds, and the AGC digests are float32 bytes).
"""
import json
import platform

import numpy as np
import pytest

from golden import record, scenarios


@pytest.mark.parametrize("name", scenarios.NAMES)
def test_live_parity(name):
    v1 = record.canonical(record.cached_run("mira_listen_v1", name))
    cur = record.canonical(record.cached_run("mira_listen", name))
    for key in v1:
        assert cur[key] == v1[key], f"{name}: {key} differs"
    assert cur == v1


@pytest.mark.parametrize("name", scenarios.NAMES)
def test_v1_matches_stored_recording(name):
    path = record.stored_path(name)
    assert path.exists(), f"missing recording {path.name}; run tests/golden/record.py --record"
    doc = json.loads(path.read_text())
    if doc["machine"] != platform.machine() or doc["numpy"] != np.__version__:
        pytest.skip(f"recorded on {doc['machine']} numpy {doc['numpy']}, running on {platform.machine()} numpy {np.__version__}")
    assert record.canonical(record.cached_run("mira_listen_v1", name)) == doc["result"]
