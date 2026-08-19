"""Runnable entrypoint: `voice/.venv/bin/python -m voice.main` (the real ML adapters only import
cleanly in voice/.venv — see voice/requirements.txt — not the repo's system Python). Wires the
real adapters, DaemonClient, and CodexClient into VoiceLoop and runs it forever. Requires
mira-guardrail and mira-daemon already running (see this plan's Task 15 manual verification
checklist)."""

import sys

from voice.adapters.audio_io import MicStream
from voice.adapters.stt import FasterWhisperStt
from voice.adapters.tts import KokoroTts
from voice.adapters.vad import SileroVad
from voice.adapters.wake import SherpaWakeEngine
from voice.codex_client import CodexClient
from voice.daemon_client import DaemonClient
from voice.identity import load_identity
from voice.loop import VoiceLoop
from voice.memory import ConversationBuffer


def main() -> None:
    identity_prompt = load_identity()
    mic = MicStream()
    daemon = DaemonClient(node_id="mac-host-voice")
    daemon.connect()

    loop = VoiceLoop(
        wake=SherpaWakeEngine(mic),
        vad=SileroVad(mic),
        stt=FasterWhisperStt(),
        tts=KokoroTts(),
        turns=CodexClient(),
        capabilities=daemon,
        events=daemon,
        identity_prompt=identity_prompt,
        memory=ConversationBuffer(),
    )

    print("Mira voice loop running. Say \"Mira\" to begin.", file=sys.stderr)
    while True:
        loop.run_once()


if __name__ == "__main__":
    main()
