"""source.pipewire: the mic through pw-record. The class is v1's PwSource (copied), constructed with every value
from the config.

FIXED: the pw-record arguments `--rate 16000 --channels 1 --format s16 -`, and one chunk = 1600 samples (100 ms).
"""

import queue
import subprocess
import threading
import time

import numpy as np

from mirakit.plugin import Knob, PluginBase, PluginSpec

from .common import CHUNK, RATE

LEGACY_CLASSES = ("PwSource",)


class PwSource:
    """pw-record on stdout. A reader thread keeps the pipe drained so nothing blocks the PipeWire graph."""

    live = True

    def __init__(self, cmd=None, record_bin="pw-record", queue_chunks=600):
        cmd = cmd or [record_bin, "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.q = queue.Queue(maxsize=queue_chunks)  # 60 s of audio; beyond that the oldest is dropped
        self.stop = False
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        want = CHUNK * 2
        buf = b""
        while not self.stop:
            data = self.proc.stdout.read(want - len(buf))
            if not data:
                self.q.put(None)
                return
            buf += data
            if len(buf) == want:
                try:
                    self.q.put_nowait(np.frombuffer(buf, dtype="<i2").astype(np.float32) / 32768.0)
                except queue.Full:
                    try:
                        self.q.get_nowait()
                    except queue.Empty:
                        pass
                buf = b""

    def read(self):
        return self.q.get()

    def clock(self):
        return time.monotonic()

    def flush(self):
        """Drop audio queued while we were thinking or speaking, so Mira never hears herself."""
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def close(self):
        self.stop = True
        self.proc.terminate()


class SourcePipewire(PluginBase):
    def start(self):
        src = PwSource(record_bin=self.cfg.record_bin, queue_chunks=self.cfg.queue_chunks)
        self.src = src
        self.ctx.effect(src.close, "pw-record")
        self.ctx.provide("source", src)

    def status(self):
        src = getattr(self, "src", None)
        return {"queued": src.q.qsize() if src is not None else 0}


SPEC = PluginSpec(
    name="source.pipewire", kind="source", version="1", machine="listener", provides=("source",),
    summary="The microphone through pw-record on the Pi.",
    create=lambda ctx, cfg: SourcePipewire(ctx, cfg), order=20,
    knobs=(
        Knob("record_bin", "str", "pw-record", "Capture command; rate, channels and format are fixed.", min=1, max=200,
             unit="chars", applies="restart"),
        Knob("queue_chunks", "int", 600, "Audio buffered before the oldest chunk is dropped.", unit="chunks(100ms)",
             min=50, max=3000, applies="restart"),
    ),
    tests=("wall/tests/plugins/test_lp_source_pipewire.py",),
)
