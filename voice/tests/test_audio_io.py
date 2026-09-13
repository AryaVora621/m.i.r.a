"""_RingQueue is pure logic, unlike the rest of audio_io.py which wraps real sounddevice
hardware (see test_adapters_smoke.py) — testable directly without a mic."""

from voice.adapters.audio_io import _RingQueue


def test_ring_queue_discards_oldest_when_full_instead_of_raising():
    q = _RingQueue(maxsize=2)
    q.put(b"a")
    q.put(b"b")
    q.put(b"c")  # would raise queue.Full on a plain bounded Queue
    assert q.get() == b"b"
    assert q.get() == b"c"


def test_ring_queue_preserves_fifo_order_under_normal_load():
    q = _RingQueue(maxsize=5)
    q.put(b"x")
    q.put(b"y")
    assert q.get() == b"x"
    assert q.get() == b"y"
