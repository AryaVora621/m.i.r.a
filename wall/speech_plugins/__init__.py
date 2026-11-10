"""Speech machine plugins of Mira (the Mac mini speech server), one module per plugin.

Nothing is imported at module level on purpose: speech/server.py imports single backend modules
while other code is still being written, and a legacy fallback must work even when one plugin
module is broken. specs() imports them when the core asks.
"""
from pathlib import Path

MODULES = ("server_http", "control_http", "stt_engine", "stt_faster_whisper", "stt_sherpa_whisper",
           "stt_sherpa_moonshine", "stt_sherpa_moonshine_v2", "stt_sherpa_transducer", "tts_engine",
           "tts_piper", "tts_kokoro", "voices_catalog")


def specs():
    """Every PluginSpec of the speech machine, in a stable order."""
    import importlib
    return tuple(importlib.import_module(f"{__name__}.{m}").SPEC for m in MODULES)


def expand_path(value, paths):
    """Expand ~, ${HOME} and ${HERE} through the context's Paths (never the process environment)."""
    text = str(value).replace("${HERE}", str(paths.here)).replace("${HOME}", str(paths.home))
    if text == "~" or text.startswith("~/"):
        text = str(paths.home) + text[1:]
    return Path(text)
