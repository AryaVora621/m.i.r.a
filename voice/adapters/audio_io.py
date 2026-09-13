"""Real microphone capture and speaker playback via sounddevice. The pre-wake ring buffer this
module reads from is in-memory only (a bounded numpy buffer) and is never written to disk, per
this plan's Global Constraints."""

import queue

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
# Silero VAD (v5+) requires exactly 512-sample chunks at 16kHz — anything shorter raises
# ValueError: "Input audio chunk is too short". 512 samples = 32ms, not the more common 30ms.
_FRAME_SAMPLES = 512


class _RingQueue:
    """Bounded queue that discards the oldest item to make room for a new one instead of
    raising when full, so a producer (the audio callback, on sounddevice's own thread) never
    blocks or throws — it just keeps the most recent items. A plain queue.Queue's put_nowait()
    raises queue.Full whenever the consumer falls behind for longer than the buffer holds
    (e.g. blocked on input() during enrollment, or on an LLM/STT call mid-conversation), and
    that exception surfaces from inside sounddevice's cffi callback."""

    def __init__(self, maxsize: int):
        self._queue: "queue.Queue[bytes]" = queue.Queue(maxsize=maxsize)

    def put(self, item: bytes) -> None:
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass

    def get(self) -> bytes:
        return self._queue.get()


class MicStream:
    """Blocking, frame-at-a-time mic reader. One InputStream is opened for the process lifetime;
    read_frame() blocks on an internal queue fed by the audio callback."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, frame_samples: int = _FRAME_SAMPLES):
        self._queue = _RingQueue(maxsize=50)
        self._stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
            blocksize=frame_samples,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status):
        self._queue.put(bytes(indata))

    def read_frame(self) -> bytes:
        return self._queue.get()

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


def play_pcm(audio, sample_rate: int) -> None:
    if audio is None:
        return
    # Kokoro's Result.audio is a torch.FloatTensor (possibly on an MPS/CUDA device); sounddevice
    # needs a real CPU numpy array to play it.
    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().numpy()
    else:
        audio = np.asarray(audio)
    sd.play(audio, samplerate=sample_rate, blocking=False)


def stop_playback() -> None:
    sd.stop()
