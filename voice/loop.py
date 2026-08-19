"""The voice loop's 9-state FSM: IDLE -> WAKE_DETECTED -> LISTENING -> TRANSCRIBING -> THINKING
-> SPEAKING -> IDLE, with INTERRUPTED (barge-in), MUTED, and OFFLINE as the non-linear states.

Deliberately holds no real audio/network/subprocess code — everything it touches is injected via
the Protocols in voice/interfaces.py, so this file's tests never need a microphone, speaker, or
network connection."""

import re
import threading

from voice.memory import ConversationBuffer

_VOLUME_PATTERN = re.compile(r"volume.{0,20}?(\d{1,3})", re.IGNORECASE)


class VoiceLoop:
    def __init__(self, wake, vad, stt, tts, turns, capabilities, events, identity_prompt, memory: ConversationBuffer, muted: bool = False):
        self.wake = wake
        self.vad = vad
        self.stt = stt
        self.tts = tts
        self.turns = turns
        self.capabilities = capabilities
        self.events = events
        self.identity_prompt = identity_prompt
        self.memory = memory
        self.muted = muted
        self.state = "IDLE"

    def run_once(self) -> None:
        self.wake.listen_for_wake()

        if not self.capabilities.is_connected():
            self._set_state("OFFLINE")
            return

        if self.muted:
            self._set_state("MUTED")
            return

        self._set_state("WAKE_DETECTED")
        self._listen_and_respond()

    def _listen_and_respond(self) -> None:
        self._set_state("LISTENING")
        audio = self.vad.capture_utterance()

        # Set state directly (not via _set_state) to avoid a duplicate TRANSCRIBING publish —
        # the explicit publish_state call two lines down is the one and only TRANSCRIBING event,
        # carrying the transcript that a bare _set_state("TRANSCRIBING") wouldn't have yet.
        self.state = "TRANSCRIBING"
        transcript = self.stt.transcribe(audio)
        self.events.publish_state("TRANSCRIBING", transcript=transcript, is_final=True)
        self.memory.add("user", transcript)

        self._set_state("THINKING")
        try:
            reply = self.turns.generate(self.identity_prompt, self.memory.to_history(), transcript)
        except Exception:
            reply = "Sorry, I ran into trouble thinking that through."
        self.memory.add("assistant", reply)

        self._maybe_dispatch_capability(transcript)

        interrupted = self._speak(reply)
        if interrupted:
            self._listen_and_respond()
        else:
            self._set_state("IDLE")

    def _speak(self, text: str) -> bool:
        self._set_state("SPEAKING")
        interrupted_flag = threading.Event()
        stop_monitor = threading.Event()

        def monitor():
            while not stop_monitor.is_set():
                frame = self.vad.read_frame()
                if self.vad.has_speech(frame):
                    interrupted_flag.set()
                    return

        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()
        try:
            self.tts.speak(text, should_cancel=interrupted_flag.is_set)
        finally:
            stop_monitor.set()
            monitor_thread.join(timeout=1.0)

        if interrupted_flag.is_set():
            self._set_state("INTERRUPTED")
            return True
        return False

    def _maybe_dispatch_capability(self, transcript: str) -> None:
        match = _VOLUME_PATTERN.search(transcript)
        if match is None:
            return
        level = min(int(match.group(1)), 100)
        self.capabilities.request_capability("mac.volume.set", {"level": level})

    def _set_state(self, state: str) -> None:
        self.state = state
        transcript = None
        is_final = None
        self.events.publish_state(state, transcript=transcript, is_final=is_final)
