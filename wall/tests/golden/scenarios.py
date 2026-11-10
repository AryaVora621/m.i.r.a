"""S1 to S17 of spec 6.3 as data. Audio is composed in code from the two fixture WAVs; replies are scripted.

A scenario is a dict:
  name, desc
  audio(ml)      -> float32 array at 16 kHz (`ml` is the module under test: its read_wav_float loads the fixtures)
  stt            -> scripted /stt replies by call order (see fakes.FakeSpeech); "down" instead means connection refused
  tts_errors     -> {tts call index: HTTP status}
  hermes         -> scripted chat/stream replies by call order (see fakes.FakeHermes)
  volumes        -> music-api status volumes by call order
  extra          -> extra argv appended after the common argv

Timing rule for WAV mode: the follow-up window runs on audio time, and handling a clip takes no audio time, so a
second wake word must start more than `followup_s` (8 s) of audio after a spoken reply, or it is a follow-up.
The wake fixtures contain "Mira", so the spotter fires on them. NOWAKE is the fixture's speech without the word,
used where the scenario needs speech the spotter must not turn into a wake.
"""
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"
RATE = 16000

# None: the spotter fires on both fixtures at the code default (0.15), checked on 2026-11-10, so no override is passed.
# Set to "0.05" (the live Pi value) if a fixture ever stops firing.
KWS_THRESHOLD = None

CAL_TEXT = "Mira, what's on my calendar today?"
CAL_REPLY = "You have a physics review at three and nothing else today."


def silence(seconds, noise=0.003, seed=1):
    """A quiet room: low broadband noise rather than digital zero, which a real mic never delivers."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    return np.convolve(rng.normal(0, noise, n), np.ones(4) / 4, mode="same").astype(np.float32)


def zeros(seconds):
    return np.zeros(int(seconds * RATE), dtype=np.float32)


def compose(*parts):
    return np.concatenate([p if isinstance(p, np.ndarray) else silence(p, seed=i + 1)
                           for i, p in enumerate(parts)]).astype(np.float32)


def cal(ml):
    return ml.read_wav_float(FIXTURES / "mira_what_s_on_my_calendar_today.wav")


def pause(ml):
    return ml.read_wav_float(FIXTURES / "mira_pause_the_music.wav")


def nowake(ml):
    """'pause the music' without the word 'Mira' (the first 0.6 s), so the spotter stays quiet."""
    return pause(ml)[9600:]


LONG_REPLY = " ".join(
    f"Sentence number {i} is here to make the reply long enough that it must be split into several spoken parts today."
    for i in range(1, 9))

SCENARIOS = [
    {"name": "s01_calendar_roundtrip", "desc": "wake, Hermes, one-chunk reply, window expires unused",
     "audio": lambda ml: compose(2.0, cal(ml), 10.0), "stt": [CAL_TEXT], "hermes": [CAL_REPLY]},
    {"name": "s02_music_local", "desc": "pause, volume up 50 to 60, up 95 to 100, down 5 to 0, all local",
     "audio": lambda ml: compose(2.0, pause(ml), 1.5, nowake(ml), 1.5, nowake(ml), 1.5, nowake(ml), 10.0),
     "stt": ["Mira, pause the music.", "Volume up.", "Turn it up.", "Volume down."], "volumes": [50, 95, 5]},
    {"name": "s03_followup_same_session", "desc": "follow-up without the wake word reuses the Hermes session",
     "audio": lambda ml: compose(2.0, cal(ml), 1.5, nowake(ml), 10.0),
     "stt": [CAL_TEXT, "And what about tomorrow?"], "hermes": [CAL_REPLY, "Tomorrow is free."]},
    {"name": "s04_stt_offline", "desc": "STT connection refused: chime and the error trail",
     "audio": lambda ml: compose(2.0, cal(ml), 4.0), "stt": "down"},
    {"name": "s05_stt_http500", "desc": "STT HTTP 500 counts as offline",
     "audio": lambda ml: compose(2.0, cal(ml), 4.0), "stt": [("status", 500)]},
    {"name": "s06_hermes_failure", "desc": "Hermes HTTP error: error posted, then the fail line is spoken after the hold",
     "audio": lambda ml: compose(2.0, cal(ml), 10.0), "stt": [CAL_TEXT], "hermes": [("http", 500)],
     "extra": ["--error-hold", "1.0"]},
    {"name": "s07_hermes_sse_error", "desc": "SSE error event gives the same fail line",
     "audio": lambda ml: compose(2.0, cal(ml), 10.0), "stt": [CAL_TEXT], "hermes": [("error", "boom")]},
    {"name": "s08_long_reply_capped", "desc": "multi-chunk reply is capped at 3 TTS chunks of at most 280 chars",
     "audio": lambda ml: compose(2.0, cal(ml), 10.0), "stt": [CAL_TEXT], "hermes": [LONG_REPLY]},
    {"name": "s09_session_ttl", "desc": "second wake after the TTL starts a new Hermes session",
     "audio": lambda ml: compose(2.0, cal(ml), 13.0, cal(ml), 10.0), "stt": [CAL_TEXT, CAL_TEXT],
     "hermes": [CAL_REPLY, CAL_REPLY], "extra": ["--session-ttl", "2"]},
    {"name": "s10_empty_transcript", "desc": "empty transcript is a reject and opens no follow-up window",
     "audio": lambda ml: compose(2.0, cal(ml), 2.0, nowake(ml), 10.0), "stt": ["", "SHOULD NOT BE ASKED"]},
    {"name": "s11_lookalike_rejected", "desc": "'mirror on the wall' is rejected and opens no window",
     "audio": lambda ml: compose(2.0, cal(ml), 2.0, nowake(ml), 10.0),
     "stt": ["Mirror on the wall", "SHOULD NOT BE ASKED"]},
    {"name": "s12_bare_wake_opens_window", "desc": "a bare 'Mira' opens the follow-up window",
     "audio": lambda ml: compose(2.0, cal(ml), 2.0, nowake(ml), 10.0), "stt": ["Mira", "What time is it?"],
     "hermes": ["It is three o'clock."]},
    {"name": "s13_silence_only", "desc": "silence only: nothing posted beyond idle",
     "audio": lambda ml: compose(15.0), "stt": []},
    {"name": "s14_mic_mute", "desc": "digital zeros for 12 s then signal: mic muted, mic live",
     "audio": lambda ml: compose(zeros(12.0), 2.0, cal(ml), 10.0), "stt": [CAL_TEXT], "hermes": [CAL_REPLY]},
    {"name": "s15_no_agc", "desc": "--no-agc level path",
     "audio": lambda ml: compose(2.0, cal(ml), 4.0), "stt": [CAL_TEXT], "hermes": [CAL_REPLY],
     "extra": ["--no-agc"]},
    {"name": "s16_stt_invalid_json", "desc": "invalid JSON from /stt ends run() with the ValueError",
     "audio": lambda ml: compose(2.0, cal(ml), 4.0), "stt": [("raw", b"this is not json")]},
    {"name": "s17_tts_fails_chunk2", "desc": "TTS fails on chunk 2: playback ends silently after chunk 1",
     "audio": lambda ml: compose(2.0, cal(ml), 10.0), "stt": [CAL_TEXT], "hermes": [LONG_REPLY],
     "tts_errors": {1: 500}},
]

BY_NAME = {s["name"]: s for s in SCENARIOS}
NAMES = [s["name"] for s in SCENARIOS]
