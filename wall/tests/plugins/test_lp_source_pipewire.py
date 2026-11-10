"""source.pipewire: PwSource copied from v1, fed by a fake pw-record on PATH."""
import time

import numpy as np
import pytest

import test_lp_common as H
from listener_plugins import source_pipewire as sp

SPEC = sp.SPEC


def _pcm(n_chunks):
    x = (np.arange(n_chunks * 1600) % 2000 - 1000).astype("<i2")
    return x.tobytes(), x.astype(np.float32) / 32768.0


def _drain(src):
    out = []
    while True:
        c = src.read()
        if c is None:
            return out
        out.append(c)


def test_stage_equivalence_with_v1(tmp_path, monkeypatch):
    raw, want = _pcm(7)
    H.fake_pw_record(tmp_path, monkeypatch, raw)
    legacy = H.v1.PwSource()
    mine = sp.PwSource()
    a, b = _drain(legacy), _drain(mine)
    assert len(a) == len(b) == 7
    assert all(np.array_equal(x, y) for x, y in zip(a, b))
    assert np.array_equal(np.concatenate(b), want)
    for s in (legacy, mine):
        s.close()
        s.close()  # idempotent


def test_plugin_provides_source_and_passes_knobs(tmp_path, monkeypatch):
    raw, want = _pcm(3)
    log = H.fake_pw_record(tmp_path, monkeypatch, raw, name="my-record")
    plugin, ctx, _ = H.started("source.pipewire", record_bin="my-record", queue_chunks=77)
    src = ctx.get("source")
    assert src is plugin.src and src.live is True
    assert src.q.maxsize == 77
    chunks = _drain(src)
    assert np.array_equal(np.concatenate(chunks), want)
    deadline = time.time() + 2
    while not log.exists() and time.time() < deadline:
        time.sleep(0.01)
    assert log.read_text().split() == ["--rate", "16000", "--channels", "1", "--format", "s16", "-"]
    plugin.stop()
    ctx.release_all()  # runs src.close
    assert src.stop is True


def test_defaults_equal_v1_literals(tmp_path, monkeypatch):
    H.fake_pw_record(tmp_path, monkeypatch, b"")
    plugin, ctx, cfg = H.started("source.pipewire")
    assert cfg.record_bin == "pw-record" and cfg.queue_chunks == 600
    assert plugin.src.q.maxsize == 600 == H.v1.PwSource().q.maxsize
    ctx.release_all()


def test_flush_drops_queued_audio(tmp_path, monkeypatch):
    raw, _ = _pcm(4)
    H.fake_pw_record(tmp_path, monkeypatch, raw, tail="sleep 5")
    src = sp.PwSource()
    deadline = time.time() + 3
    while src.q.qsize() < 4 and time.time() < deadline:
        time.sleep(0.01)
    assert src.q.qsize() == 4
    src.flush()
    assert src.q.qsize() == 0
    src.close()


def test_spec_shape():
    assert SPEC.provides == ("source",) and SPEC.default_enabled and sp.LEGACY_CLASSES == ("PwSource",)
    assert {k.name: k.applies for k in SPEC.knobs} == {"record_bin": "restart", "queue_chunks": "restart"}
