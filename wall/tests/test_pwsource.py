"""PwSource against a fake pw-record (a Python script first on PATH that writes s16 PCM to stdout).

Parametrized over the frozen v1 and the current module. Chunk i is the constant i * 1000 / 32768, so a
chunk's identity survives the int16 to float32 conversion and ordering can be asserted exactly.
Gap: none for v1; the queue size is not a parameter, so the drop test swaps queue.Queue for the instant of
construction (the class is untouched).
"""
import importlib
import os
import queue
import stat
import sys
import time

import numpy as np
import pytest

MODULES = ["mira_listen_v1", "mira_listen"]
CHUNK = 1600


@pytest.fixture(params=MODULES)
def ml(request):
    return importlib.import_module(request.param)


def chunk_bytes(i):
    return np.full(CHUNK, i * 1000, dtype="<i2").tobytes()


def expect(i):
    return np.full(CHUNK, i * 1000, dtype=np.int16).astype(np.float32) / 32768.0


@pytest.fixture
def fake_pw(tmp_path, monkeypatch):
    """Install a fake `pw-record` first on PATH. `body` is Python source that runs with `out`, `chunk_bytes`, `go`."""
    def install(body):
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        script = bindir / "pw-record"
        script.write_text(f"""#!{sys.executable}
import os, sys, time
import numpy as np
out = os.fdopen(1, "wb", buffering=0)
go = {str(tmp_path / 'go')!r}
def chunk_bytes(i):
    return np.full({CHUNK}, i * 1000, dtype="<i2").tobytes()
{body}
""")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        return tmp_path / "go"
    return install


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def drain(src):
    out = []
    while True:
        c = src.read()
        if c is None:
            return out
        out.append(c)


def test_exact_chunks_odd_writes_and_eof(ml, fake_pw):
    # Writes of 700 bytes straddle chunk borders; the 1000-sample tail is never a full chunk and is dropped.
    fake_pw("""
data = b"".join(chunk_bytes(i) for i in (1, 2, 3)) + chunk_bytes(4)[:2000]
for i in range(0, len(data), 700):
    out.write(data[i:i + 700])
""")
    src = ml.PwSource()
    try:
        got = drain(src)
    finally:
        src.close()
    assert src.live is True
    assert len(got) == 3 and all(c.dtype == np.float32 and len(c) == CHUNK for c in got)
    for i, c in enumerate(got, 1):
        assert np.array_equal(c, expect(i))


def test_eof_returns_none_immediately(ml, fake_pw):
    fake_pw("pass")
    src = ml.PwSource()
    try:
        assert src.read() is None
    finally:
        src.close()


def test_full_queue_drops_oldest_slot(ml, fake_pw):
    """With room for 3 chunks and 8 written before anything is read: v1 frees the oldest slot on a full queue
    but loses the chunk that was being added (the new chunk is not put back): 1 to 3 fill the queue, 4 is lost
    and frees 1, 5 fits, 6 is lost and frees 2, 7 fits, 8 is lost and frees 3, so 5 and 7 survive.
    Recorded as it is, because the refactor must keep it."""
    fake_pw("""
for i in range(1, 9):
    out.write(chunk_bytes(i))
""")
    real = queue.Queue
    queue.Queue = lambda maxsize=0: real(maxsize=3)
    try:
        src = ml.PwSource()
    finally:
        queue.Queue = real
    try:
        assert wait_for(lambda: src.proc.poll() is not None)
        time.sleep(0.5)
        got = drain(src)
    finally:
        src.close()
    heights = [int(round(float(c[0]) * 32768 / 1000)) for c in got]
    assert heights == [5, 7], heights


def test_flush_empties_the_queue(ml, fake_pw):
    go = fake_pw("""
out.write(chunk_bytes(1)); out.write(chunk_bytes(2))
while not os.path.exists(go):
    time.sleep(0.01)
out.write(chunk_bytes(9))
""")
    src = ml.PwSource()
    try:
        assert wait_for(lambda: src.q.qsize() == 2)
        src.flush()
        assert src.q.qsize() == 0
        src.flush()  # flushing an empty queue is fine
        go.write_text("x")
        c = src.read()
        assert np.array_equal(c, expect(9))
        assert src.read() is None
    finally:
        src.close()


def test_close_twice_is_safe_and_stops_the_process(ml, fake_pw):
    fake_pw("""
out.write(chunk_bytes(1))
time.sleep(60)
""")
    src = ml.PwSource()
    assert np.array_equal(src.read(), expect(1))
    src.close()
    src.close()
    assert wait_for(lambda: src.proc.poll() is not None)
    assert src.stop is True


def test_clock_is_monotonic_time(ml, fake_pw):
    fake_pw("pass")
    src = ml.PwSource()
    try:
        a = src.clock()
        b = src.clock()
        assert b >= a and abs(a - time.monotonic()) < 5
    finally:
        src.close()
