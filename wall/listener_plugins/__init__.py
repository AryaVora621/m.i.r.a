"""Listener plugins (spec 4.4): Mira's Pi listener, one small plugin per part. `specs()` is the explicit list the
core and `mira check` read; the order here is the tie-break order of the plugins' `order` fields."""

from mirakit.plugin import PluginSpec


def specs() -> list[PluginSpec]:
    from . import (agc_mic, brain_hermes, control_http, gate_wake, intent_music, loop_turns, mute_zero, persona_file,
                   playback_miraaudio, reporter_agentos, source_pipewire, source_wav, speech_link, stt_remote,
                   tts_remote, vad_silero, wake_kws)
    return [m.SPEC for m in (
        loop_turns, source_pipewire, source_wav, mute_zero, agc_mic, wake_kws, vad_silero, speech_link, stt_remote,
        tts_remote, gate_wake, intent_music, brain_hermes, persona_file, playback_miraaudio, reporter_agentos,
        control_http)]
