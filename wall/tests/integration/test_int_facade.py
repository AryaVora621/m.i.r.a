"""Integration: the facade (mira_listen) built on the real core and the real plugins, driven by a WAV, with the
golden fakes for every network peer and a recording mira-audio. This file also holds the rig the other integration
files import (`from test_int_facade import Rig`): test basenames are unique and pytest puts this folder on sys.path.

Nothing here records a microphone or plays sound; every port is an ephemeral one on 127.0.0.1; HOME is a temp dir.
"""
import json
import os
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

WALL = Path(__file__).resolve().parents[2]
REPO = WALL.parent
for p in (str(WALL), str(WALL / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import listener_plugins  # noqa: E402
import mira_listen as ml  # noqa: E402
from golden import fakes, scenarios  # noqa: E402
from golden.record import argv_for  # noqa: E402
from mirakit.core import RESET_STREAM, Clock, Core  # noqa: E402
from mirakit.plugin import Edit  # noqa: E402

CAL_TEXT, CAL_REPLY = scenarios.CAL_TEXT, scenarios.CAL_REPLY


class AudioClock(Clock):
    """Core time driven by the WAV's audio position (plus a manual bias), so rebuild_quiet_s and trials are
    measured in audio seconds and a test never sleeps."""

    def __init__(self):
        self.src, self.bias = None, 0.0

    def mono(self):
        return (self.src.clock() if self.src is not None else 0.0) + self.bias

    def wall(self):
        return 1_800_000_000.0 + self.mono()


class HookHermes(fakes.FakeHermes):
    """FakeHermes that calls `on_chat(index)` on its HTTP thread while a chat request is open: the loop thread is
    blocked in Brain.ask at that moment, which is exactly 'a change submitted from another thread mid-turn'."""

    def __init__(self, replies=()):
        self.on_chat = None
        super().__init__(replies)

    def respond(self, method, path, body, headers, index):
        if method == "POST" and path.endswith("/chat/stream") and self.on_chat is not None:
            self.on_chat(index)
        return super().respond(method, path, body, headers, index)


class Rig:
    """One listener over a WAV. mode "args": Listener builds its own in-memory core (the legacy path) or takes
    `core_kw` (clock, ...); mode "file": a production-style core from a config file under HOME."""

    def __init__(self, tmp, monkeypatch, audio, *, stt=(), hermes=(), tts_errors=None, volumes=(50,), extra=(),
                 mode="args", file_plugins=None, file_core=None, mirror=True, hermes_cls=HookHermes, clock=True,
                 plugin_specs=None):
        self.tmp = Path(tmp)
        self.home = self.tmp / "home"
        self.home.mkdir(exist_ok=True)
        for k in [k for k in os.environ if k.startswith("MIRA_")]:
            monkeypatch.delenv(k)
        monkeypatch.setenv("HOME", str(self.home))
        wav = self.tmp / "in.wav"
        wav.write_bytes(ml.pcm16_wav_bytes(audio))
        self.tokens = fakes.write_tokens(self.tmp)
        self.audio = fakes.FakeAudio(self.tmp)
        self.speech = fakes.FakeSpeech(stt, tts_errors)
        self.hermes = hermes_cls(hermes)
        self.hook = fakes.FakeHook()
        self.music = fakes.FakeMusic(volumes)
        urls = {"speech": self.speech.url, "hermes": self.hermes.url, "voice": self.hook.url, "music": self.music.url}
        argv = argv_for({"extra": list(extra)}, self.tmp, urls, self.tokens, self.audio.bin)
        self.specs = plugin_specs or listener_plugins.specs()
        self.clock = AudioClock() if clock else None
        kw = {"clock": self.clock} if self.clock else {}
        if mode == "args":
            self.argv = argv
            self.args = ml.build_parser().parse_args(argv)
            self.src = ml.WavSource(str(wav))
            if self.clock:
                self.clock.src = self.src
            self.core = Core.from_args(self.args, specs=self.specs, source=self.src, **kw)
            self.core.boot()
        else:
            self.cfg_path = self.home / ".config" / "mira" / "mira.json"
            self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
            self.argv = ["--config", str(self.cfg_path), "--wav", str(wav)]
            self.cfg_path.write_text(json.dumps(self.file_doc(argv, file_plugins, file_core)))
            self.args = ml.build_parser().parse_args(self.argv)
            self.core = Core.from_file(str(self.cfg_path), argv=self.argv, machine="listener", specs=self.specs,
                                       parser=ml.build_parser(), mirror=mirror, changelog=True, **kw)
            self.core.boot()
            self.src = self.core.service("source")
            if self.clock:
                self.clock.src = self.src
        self.lis = ml.Listener(self.args, self.src, self.core)

    def file_doc(self, argv, file_plugins, file_core):
        """A minimal mira.json: only what differs from the schema defaults (the fakes' URLs and token files)."""
        d = dict(zip(argv[::2], argv[1::2]))
        plugins = {
            "wake.kws": {"enabled": True, "config": {"kws_dir": d["--kws-dir"]}},
            "vad.silero": {"enabled": True, "config": {"model": d["--vad-model"]}},
            "speech.link": {"enabled": True, "config": {"url": d["--speech-url"], "token_file": d["--speech-token-file"]}},
            "brain.hermes": {"enabled": True, "config": {"url": d["--hermes-url"], "key_file": d["--hermes-key-file"]}},
            "persona.file": {"enabled": True, "config": {"path": d["--identity"]}},
            "reporter.agentos": {"enabled": True, "config": {"url": d["--voice-url"], "token_file": d["--voice-token-file"],
                                                              "error_hold": 0.3}},
            "intent.music": {"enabled": True, "config": {"url": d["--music-url"], "auth_file": d["--music-auth-file"]}},
            "playback.miraaudio": {"enabled": True, "config": {"bin": d["--mira-audio"], "chime": d["--chime"]}},
        }
        for k, v in (file_plugins or {}).items():
            plugins.setdefault(k, {"enabled": True, "config": {}})
            plugins[k] = {"enabled": v.get("enabled", True), "config": {**plugins[k]["config"], **v.get("config", {})}}
        return {"format": 1, "machine": "listener", "revision": 1,
                "core": {"engine": "plugins", **(file_core or {})}, "plugins": plugins}

    def run(self):
        try:
            self.lis.run()
        finally:
            self.close()

    def close(self):
        if getattr(self, "closed", False):
            return
        self.closed = True
        self.core.shutdown()
        for s in (self.speech, self.hermes, self.hook, self.music):
            s.stop()

    def marks(self):
        return [k for k, _ in self.lis.events]


@pytest.fixture
def rig_factory(tmp_path, monkeypatch):
    made = []

    def make(audio, **kw):
        d = tmp_path / f"r{len(made)}"
        d.mkdir()
        r = Rig(d, monkeypatch, audio, **kw)
        made.append(r)
        return r
    yield make
    for r in made:
        try:
            r.close()
        except Exception:
            pass


def cal_audio(tail=10.0):
    return scenarios.compose(2.0, scenarios.cal(ml), tail)


# ----------------------------------------------------------------------------------------------------------------
# names and construction
# ----------------------------------------------------------------------------------------------------------------

REEXPORTS = ("RATE CHUNK HERE build_parser Listener WavSource PwSource build_kws Speech SpeechOffline read_wav_float "
             "read_wav_float_bytes pcm16_wav_bytes wav_envelope MicAGC MuteDetector VoiceReporter Hermes Music "
             "music_intent strip_wake has_wake clean_for_speech speech_chunks resample HALLUCINATIONS WAKE_WORD "
             "WAKE_OK WAKE_PREFIX DEFAULTS log read_secret http").split()


def test_every_v1_name_stays_importable():
    import mira_listen_v1 as v1
    missing = [n for n in REEXPORTS if not hasattr(ml, n)]
    assert not missing
    assert ml.DEFAULTS == v1.DEFAULTS and ml.HERE == v1.HERE
    assert (ml.RATE, ml.CHUNK, ml.WAKE_WORD, ml.HALLUCINATIONS) == (v1.RATE, v1.CHUNK, v1.WAKE_WORD, v1.HALLUCINATIONS)


def test_parser_is_v1s_plus_config():
    import mira_listen_v1 as v1
    old = {a.dest: a.default for a in v1.build_parser()._actions}
    new = {a.dest: a.default for a in ml.build_parser()._actions}
    assert new.pop("config") is None
    assert new == old


def test_listener_attributes_and_services(rig_factory):
    r = rig_factory(cal_audio(1.0))
    lis = r.lis
    for name in ("a", "src", "kws", "vad", "_vc", "speech", "hermes", "music", "rep", "agc", "mute", "followup_until",
                 "wakes", "events", "_heard", "_reply", "core", "stt", "tts", "gate", "intents", "playback", "vadf"):
        assert hasattr(lis, name), name
    assert lis.core is r.core and lis.src is r.src and lis.a is r.args
    assert lis.kws is r.core.service("wake") and lis.speech is r.core.service("speech")
    assert isinstance(lis.events, list)            # events_max 0 is the legacy unbounded list
    assert lis.music is lis.intents[0].music and lis.agc is not None
    assert lis.followup_until == 0.0 and lis.wakes == 0 and lis._heard == "" and lis._reply == ""
    # bind_args added what v1's namespace never had
    for attr, want in (("preroll_chunks", 15), ("wake_seed_chunks", 3), ("followup_seed_chunks", 5),
                       ("followup_wait_s", 1.0), ("offline_text", "Speech is offline"), ("level_scale", 8.0)):
        assert getattr(r.args, attr) == want, attr


def test_events_is_a_bounded_deque_from_the_file(rig_factory):
    r = rig_factory(cal_audio(1.0), mode="file", file_plugins={"loop.turns": {"config": {"events_max": 7}}})
    import collections
    assert isinstance(r.lis.events, collections.deque) and r.lis.events.maxlen == 7


def test_room_eval_subclass_still_imports_and_can_wrap_the_spotter(rig_factory):
    sys.path.insert(0, str(WALL / "tests"))
    import room_eval
    assert issubclass(room_eval.EvalListener, ml.Listener)
    r = rig_factory(cal_audio(1.0))
    real = r.lis.kws
    r.lis.kws = room_eval.KeepStream(real)
    assert r.lis.kws is not real and r.lis.kws.create_stream is not None


# ----------------------------------------------------------------------------------------------------------------
# a whole turn: events, the brain bracket, log_text
# ----------------------------------------------------------------------------------------------------------------

def test_calendar_turn_emits_the_events_and_brackets_the_brain(rig_factory):
    r = rig_factory(cal_audio(), stt=[CAL_TEXT], hermes=[CAL_REPLY])
    seen, phases = [], []
    for ev in ("wake.hit", "wake.accepted", "turn.heard", "turn.replied", "turn.spoken", "mic.changed"):
        r.core.on(ev, lambda p: seen.append(p))
    r.hermes.on_chat = lambda i: phases.append(r.core.turn.phase if r.core.turn else None)
    r.run()
    assert [e["event"] for e in seen] == ["mic.changed", "wake.hit", "wake.accepted", "turn.heard", "turn.replied",
                                          "turn.spoken"]
    by = {e["event"]: e for e in seen}
    assert by["mic.changed"]["mic"] == "live"
    assert by["turn.heard"]["words"] == 5          # "what's on my calendar today?" once the wake word is stripped
    assert by["turn.replied"]["source"] == "brain" and by["turn.spoken"]["chunks"] == 1
    assert CAL_REPLY not in json.dumps(seen) and "calendar" not in json.dumps(seen)   # never the reply or request text
    assert phases == ["brain"] and r.core.turn is None
    assert r.audio.calls()[0] == "duck" and r.audio.calls()[-1] == "unduck"


def test_music_reply_source_is_intent_and_offline_event(rig_factory):
    r = rig_factory(scenarios.compose(2.0, scenarios.pause(ml), 10.0), stt=["Mira, pause the music."])
    seen = []
    r.core.on("turn.replied", lambda p: seen.append(p["source"]))
    r.run()
    assert seen == ["intent"] and "music" in r.marks()
    r2 = rig_factory(cal_audio(4.0), stt=[("status", 500)])
    off = []
    r2.core.on("turn.offline", lambda p: off.append(p["stage"]))
    r2.run()
    assert off == ["stt"] and "offline" in r2.marks()


def test_log_text_false_drops_detail_from_the_journal_not_from_events(rig_factory, capsys):
    r = rig_factory(cal_audio(), stt=[CAL_TEXT], hermes=[CAL_REPLY])
    r.core.submit([Edit("loop.turns.log_text", False)], actor="owner", scope="owner")
    r.core.safe_point(True)
    r.run()
    err = capsys.readouterr().err
    assert "calendar" not in err and CAL_REPLY not in err
    assert any(l.endswith(" wake_accepted") for l in err.splitlines()) and any(l.endswith(" heard") for l in err.splitlines())
    assert [d for k, d in r.lis.events if k == "reply"] == [CAL_REPLY]        # the in-memory trail keeps the detail


def test_persona_is_refreshed_each_turn_when_reread_is_turn(rig_factory, tmp_path):
    ident = tmp_path / "ident.md"
    ident.write_text("You are Mira. First.")
    r = rig_factory(cal_audio(), stt=[CAL_TEXT], hermes=[CAL_REPLY], extra=["--identity", str(ident)])
    r.core.submit([Edit("persona.file.reread", "turn")], actor="owner", scope="owner")
    r.core.safe_point(True)
    ident.write_text("You are Mira. Second.")
    r.run()
    bodies = r.hermes.json_bodies(next(c["path"] for c in r.hermes.calls if c["path"].endswith("/chat/stream")))
    assert any("Second." in json.dumps(b) for b in bodies)


# ----------------------------------------------------------------------------------------------------------------
# main(): the production entry
# ----------------------------------------------------------------------------------------------------------------

def test_main_with_config_logs_the_v1_startup_line_with_file_values(tmp_path, monkeypatch, capsys):
    r = Rig(tmp_path, monkeypatch, cal_audio(1.0), mode="file",
            file_plugins={"wake.kws": {"config": {"threshold": 0.05}}})
    r.core.shutdown()           # main builds its own core from the same file
    rc = ml.main(r.argv)
    err = capsys.readouterr().err
    assert rc == 0
    assert "mira-listen up (wav), kws threshold 0.05 score 1.0, agc target 0.05 max 10.0x rest 3.0x gate 1.8" in err
    assert (r.home / ".config" / "mira" / "listen.env").exists() and (r.home / ".config" / "mira" / "legacy.args").exists()
    r.close()


def test_main_without_config_is_the_v1_path(tmp_path, monkeypatch, capsys):
    r = Rig(tmp_path, monkeypatch, cal_audio(1.0))
    r.core.shutdown()
    wav = tmp_path / "in.wav"
    rc = ml.main(r.argv + ["--wav", str(wav)])
    err = capsys.readouterr().err
    assert rc == 0 and "mira-listen up (wav), kws threshold 0.15 score 1.0, agc target" in err
    assert not (r.home / ".config" / "mira").exists()          # from_args writes nothing under HOME
    r.close()
