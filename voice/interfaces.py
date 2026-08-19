"""Structural interfaces VoiceLoop depends on. Real implementations (Task 14) wrap sherpa-onnx,
silero-vad, faster-whisper, kokoro, and the daemon WebSocket client; tests use plain fakes that
satisfy these Protocols without any real audio, network, or subprocess I/O."""

from typing import Callable, Protocol


class WakeWordEngine(Protocol):
    def listen_for_wake(self) -> None:
        """Blocks until the wake word is detected, then returns."""


class VoiceActivityDetector(Protocol):
    def capture_utterance(self) -> bytes:
        """Blocks, buffering mic audio, until a trailing silence boundary. Returns 16kHz mono PCM16 bytes."""

    def read_frame(self) -> bytes:
        """Blocks until one short (~30ms) mic frame is available. Used to watch for barge-in during SPEAKING."""

    def has_speech(self, frame: bytes) -> bool:
        """Non-blocking. True if the given frame contains speech."""


class SpeechToText(Protocol):
    def transcribe(self, pcm16_audio: bytes) -> str: ...


class TextToSpeech(Protocol):
    def speak(self, text: str, should_cancel: Callable[[], bool]) -> None:
        """Synthesizes and plays audio for text, checking should_cancel() between chunks and
        stopping playback immediately once it returns True."""


class TurnGenerator(Protocol):
    def generate(self, system_prompt: str, history: list[dict[str, str]], user_text: str) -> str: ...


class CapabilityClient(Protocol):
    def request_capability(self, capability: str, arguments: dict) -> dict:
        """Sends a CapabilityRequest and blocks for the CapabilityReceipt payload (as a dict).
        Raises if the daemon is unreachable."""

    def is_connected(self) -> bool: ...


class EventPublisher(Protocol):
    def publish_state(self, state: str, transcript: str | None = None, is_final: bool | None = None) -> None: ...
