import time

from voice.loop import VoiceLoop
from voice.memory import ConversationBuffer


class FakeWake:
    def __init__(self):
        self.calls = 0
    def listen_for_wake(self):
        self.calls += 1


class FakeVad:
    def __init__(self, utterance=b"fake-pcm", speech_frames=None):
        self.utterance = utterance
        self._speech_frames = speech_frames or []
        self._frame_index = 0
    def capture_utterance(self):
        return self.utterance
    def read_frame(self):
        self._frame_index += 1
        return b"frame-%d" % self._frame_index
    def has_speech(self, frame):
        return self._frame_index <= len(self._speech_frames) and self._speech_frames[self._frame_index - 1]


class FakeStt:
    def __init__(self, text="set the volume to 40"):
        self.text = text
        self.call_count = 0
    def transcribe(self, pcm16_audio):
        self.call_count += 1
        return self.text


class FakeTts:
    def __init__(self):
        self.spoken = []
        self.was_cancelled = []
    def speak(self, text, should_cancel):
        self.spoken.append(text)
        # Poll should_cancel with a real sleep between checks (not a tight loop) so the
        # background barge-in monitor thread (a real OS thread in VoiceLoop) actually gets
        # scheduled to run and flip the flag — a 0-sleep loop can finish before the GIL ever
        # hands control to the monitor thread, making the interruption assertion flaky.
        # Non-interrupted tests pay this as a bounded ~50ms cost, not an open-ended wait.
        for _ in range(10):
            if should_cancel():
                self.was_cancelled.append(text)
                return
            time.sleep(0.005)


class FakeTurns:
    def __init__(self, reply="volume is now forty", raise_error=False):
        self.reply = reply
        self.raise_error = raise_error
        self.calls = []
    def generate(self, system_prompt, history, user_text):
        self.calls.append((system_prompt, list(history), user_text))
        if self.raise_error:
            raise RuntimeError("simulated Codex failure")
        return self.reply


class FakeCapabilities:
    def __init__(self, connected=True):
        self.connected = connected
        self.requests = []
    def request_capability(self, capability, arguments):
        self.requests.append((capability, arguments))
        return {"result_status": "SUCCESS"}
    def is_connected(self):
        return self.connected


class FakeEvents:
    def __init__(self):
        self.published = []
    def publish_state(self, state, transcript=None, is_final=None):
        self.published.append((state, transcript, is_final))


def make_loop(**overrides):
    defaults = dict(
        wake=FakeWake(),
        vad=FakeVad(),
        stt=FakeStt(),
        tts=FakeTts(),
        turns=FakeTurns(),
        capabilities=FakeCapabilities(),
        events=FakeEvents(),
        identity_prompt="You are Mira.",
        memory=ConversationBuffer(),
    )
    defaults.update(overrides)
    return VoiceLoop(**defaults)


def test_happy_path_visits_every_state_in_order_and_returns_to_idle():
    loop = make_loop()
    loop.run_once()

    states = [state for state, _, _ in loop.events.published]
    assert states == [
        "WAKE_DETECTED", "LISTENING", "TRANSCRIBING", "THINKING", "SPEAKING", "IDLE",
    ]
    assert loop.state == "IDLE"


def test_happy_path_speaks_the_codex_reply_and_updates_memory():
    loop = make_loop()
    loop.run_once()

    assert loop.tts.spoken == ["volume is now forty"]
    history = loop.memory.to_history()
    assert history[-2] == {"role": "user", "content": "set the volume to 40"}
    assert history[-1] == {"role": "assistant", "content": "volume is now forty"}


def test_muted_ignores_wake_and_never_transcribes():
    loop = make_loop(muted=True)
    loop.run_once()

    assert loop.state == "MUTED"
    assert loop.stt.call_count == 0
    assert loop.events.published == [("MUTED", None, None)]


def test_codex_failure_speaks_an_apology_and_returns_to_idle_without_crashing():
    loop = make_loop(turns=FakeTurns(raise_error=True))
    loop.run_once()  # must not raise

    assert loop.state == "IDLE"
    assert len(loop.tts.spoken) == 1
    assert "trouble" in loop.tts.spoken[0].lower() or "sorry" in loop.tts.spoken[0].lower()


def test_offline_when_daemon_unreachable_skips_the_turn():
    loop = make_loop(capabilities=FakeCapabilities(connected=False))
    loop.run_once()

    assert loop.state == "OFFLINE"
    assert loop.turns.calls == []


def test_barge_in_interrupts_speaking_and_returns_to_listening():
    # speech_frames=[False, True] means the 2nd frame read during SPEAKING looks like speech
    vad = FakeVad(speech_frames=[False, True])
    loop = make_loop(vad=vad)
    loop.run_once()

    assert "volume is now forty" in loop.tts.was_cancelled
    states = [state for state, _, _ in loop.events.published]
    assert "INTERRUPTED" in states
    # after an interruption the loop re-enters LISTENING rather than settling at IDLE
    assert states.index("INTERRUPTED") > states.index("SPEAKING")


def test_only_mac_volume_set_capability_is_ever_requested():
    loop = make_loop()
    loop.run_once()

    assert loop.capabilities.requests, "the '40' in the transcript should have triggered a capability request"
    for capability, _arguments in loop.capabilities.requests:
        assert capability == "mac.volume.set"
