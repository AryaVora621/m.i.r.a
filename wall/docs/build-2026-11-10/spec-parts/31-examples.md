
#### 2.4.3 Full Pi file at cutover (`deploy/mira.example.json`; values equal the live Pi on 2026-11-10)
Values that differ from the schema default are marked in 2.4.5. Paths are the Pi's. The real file has no `$comment` keys except the header.

```json
{
 "$comment": "Written by mirakit. Change with `mira set`; see wall/AGENTS.md. Bounds live in the plugin specs.",
 "format": 1, "machine": "listener", "revision": 1,
 "core": {"engine": "plugins", "legacy_env": "ignore",
  "unit_memory_max": "200M", "unit_cpu_quota": "40%", "unit_nice": 5, "unit_restart": "always", "unit_restart_sec": 3.0,
  "crash_window_s": 600, "crash_guard_after": 3, "crash_legacy_after": 4, "crash_guard_revert_s": 1800},
 "plugins": {
  "loop.turns": {"enabled": true, "config": {"wait_speech": 4.0, "max_s": 10.0, "followup_s": 8.0, "followup_wait_s": 1.0,
    "followup_seed_chunks": 5, "wake_seed_chunks": 3, "preroll_chunks": 15, "offline_text": "Speech is offline",
    "events_max": 2000, "log_text": true, "sigterm_idle": false}},
  "source.pipewire": {"enabled": true, "config": {"record_bin": "pw-record", "queue_chunks": 600}},
  "source.wav": {"enabled": false, "config": {"path": "", "tail_s": 1.0}},
  "mute.zero": {"enabled": true, "config": {"mute_s": 10.0, "mute_share": 0.99, "live_share": 0.90, "block_s": 1.0, "repeat_s": 60.0}},
  "agc.mic": {"enabled": true, "config": {"on": true, "target": 0.05, "max_gain": 10.0, "rest": 3.0, "gate": 1.8,
    "release": 0.25, "decay": 0.03, "min_rms": 0.001, "ceiling": 0.9, "floor_init": 0.003,
    "floor_rate_speech": 0.0005, "floor_rate_low": 0.2, "floor_rate_mid": 0.05}},
  "wake.kws": {"enabled": true, "config": {"kws_dir": "/home/mira/mira-listen/kws", "keywords": "keywords.txt",
    "threshold": 0.05, "score": 1.0, "threads": 1}},
  "vad.silero": {"enabled": true, "config": {"model": "/home/mira/mira-listen/models/silero_vad.onnx", "threshold": 0.5,
    "end_silence": 0.7, "min_speech": 0.15, "max_speech": 30.0, "threads": 1, "buffer_s": 30.0}},
  "speech.link": {"enabled": true, "config": {"url": "http://100.77.98.89:8765",
    "token_file": "/home/mira/.config/mira/speech.token", "stt_timeout_s": 30.0, "tts_timeout_s": 45.0}},
  "stt.remote": {"enabled": true, "config": {}},
  "tts.remote": {"enabled": true, "config": {"voice": "", "speed": 0.0, "peak": 0.0, "chunk_limit": 280, "max_chunks": 3,
    "pin_voice": true}},
  "gate.wake": {"enabled": true, "config": {"wake_word": "[a-z]{0,2}(?:i|ee|ea|e|y|ie|iu)r+(?:a|ah|ar|uh)h?",
    "wake_prefixes": ["hey", "hi", "ok", "okay"],
    "hallucinations": ["you", "thank you", "thanks", "thanks for watching", "bye", "okay", "uh", "um", "hmm", "the"]}},
  "intent.music": {"enabled": true, "config": {"priority": 100, "url": "http://100.77.98.89:4534",
    "auth_file": "/home/mira/.config/mira/music_auth.json", "timeout_s": 8.0, "volume_step": 10,
    "patterns": {
     "status": "(what'?s|what is|what song is) (playing|on)( right now| now)?|what am i listening to|what song is this",
     "volume_up": "(turn |make it |volume )?(the )?(volume )?(up|louder)( a (little|bit))?|volume up|turn (it|the music|the volume) up|louder",
     "volume_down": "volume down|turn (it|the music|the volume) down|(turn |make it )?(the )?(volume )?(down|quieter|softer)( a (little|bit))?|quieter",
     "pause": "(pause[ds]?|stop|mute)( the)?( music| song| playback)?|pause it",
     "next": "(next|skip(ped)?)( the)?( song| track)?|next song|skip it",
     "play": "(play(s|ed)?|resume|start)( the)?( music| song| playback)?|play it"},
    "replies": {"play": "Playing.", "pause": "Paused.", "next": "Next song.", "volume": "Volume {n}.",
     "echo_down": "The speaker is not connected.", "playing": "Music is playing.", "paused": "Music is paused.",
     "nothing": "Nothing is playing.", "done": "Done.", "unreachable": "I could not reach the music player."}}},
  "brain.hermes": {"enabled": true, "config": {"url": "http://127.0.0.1:8642", "key_file": "/home/mira/.config/mira/hermes_api_key",
    "session_ttl": 180.0, "session_timeout_s": 15.0, "stream_timeout_s": 150.0, "title_prefix": "Mira wall",
    "fail_reply": "I cannot reach Hermes right now.", "empty_reply": "I have nothing to add."}},
  "persona.file": {"enabled": true, "config": {"path": "/home/mira/mira-listen/MIRA_WALL_IDENTITY.md",
    "fallback": "You are Mira.", "reread": "start"}},
  "playback.miraaudio": {"enabled": true, "config": {"bin": "mira-audio", "call_timeout_s": 120.0, "level_lag": 0.15,
    "chime": "/home/mira/mira-listen/assets/offline.wav", "envelope_step_s": 0.1, "follow_join_s": 1.0}},
  "reporter.agentos": {"enabled": true, "config": {"url": "http://127.0.0.1:3456/api/hooks/voice",
    "token_file": "/home/mira/.config/mira/voice_token", "error_hold": 4.0, "min_gap": 0.5, "level_gap": 0.2,
    "level_step": 0.03, "post_timeout_s": 3.0, "close_wait_s": 20.0, "level_scale": 8.0}},
  "control.http": {"enabled": false, "config": {"bind": "127.0.0.1", "port": 8650,
    "owner_token_file": "/home/mira/.config/mira/control.owner.token", "agent_token_file": "/etc/mira/control.agent.token",
    "speech_url": "http://100.77.98.89:8765", "speech_token_file": "/home/mira/.config/mira/speech.control.token",
    "agents_locked": false, "agent_requires_voice_turn": true, "agent_writes_per_10min": 12, "agent_writes_per_knob_10min": 3,
    "agent_undo_window_s": 86400, "trial_default_s": 600, "trial_max_s": 1800, "session_max_s": 900,
    "session_max_changes": 12, "session_followup_s": 15.0, "session_idle_s": 120, "rebuild_quiet_s": 10.0,
    "previews_per_10min": 6, "preview_max_chars": 200, "preview_quiet": "23:00-06:00",
    "preview_text": "This is {alias}. This is how I would sound.", "changelog_max_bytes": 5000000,
    "history_keep": 50, "wait_s_default": 5.0}}
 }
}
```
`control.http` stays disabled at cutover and is enabled in migration step M8 (a file edit and one restart).

#### 2.4.4 Full mini file (`deploy/speech.example.json`; values equal the live LaunchAgent)
```json
{
 "$comment": "Written by mirakit. Change with `mira set` on the mini; see wall/AGENTS.md.",
 "format": 1, "machine": "speech", "revision": 1,
 "core": {"engine": "plugins", "legacy_env": "ignore", "unit_nice": 5, "unit_throttle_s": 15, "unit_keep_alive": true,
  "crash_window_s": 600, "crash_guard_after": 3, "crash_legacy_after": 4, "crash_guard_revert_s": 1800},
 "plugins": {
  "server.http": {"enabled": true, "config": {"bind": ["127.0.0.1", "100.77.98.89"], "port": 8765,
    "token_file": "/Users/aryavora/.config/mira/speech.token",
    "base_dir": "/Users/aryavora/Library/Application Support/mira-speech", "max_stt_s": 30.0, "max_tts_chars": 600,
    "tts_body_max": 4096}},
  "control.http": {"enabled": true, "config": {"owner_token_file": "/Users/aryavora/.config/mira/control.owner.token",
    "pi_token_file": "/Users/aryavora/.config/mira/control.pi.token", "trial_default_s": 600, "trial_max_s": 1800,
    "agent_writes_per_10min": 12, "busy_defer_s": 10.0, "history_keep": 50, "changelog_max_bytes": 5000000}},
  "stt.engine": {"enabled": true, "config": {"kind": "faster-whisper:tiny.en", "threads": 4, "warm": true}},
  "stt.faster_whisper": {"enabled": true, "config": {"compute": "int8", "beam": 1,
    "prompt": "Vocabulary: Mira, Hermes, 3D printers, Sovol SV08, Onshape, OrcaSlicer, slicer, vault, timer, calendar, YouTube Music.",
    "hotwords": "", "language": "en"}},
  "stt.sherpa_whisper": {"enabled": true, "config": {}},
  "stt.sherpa_moonshine": {"enabled": true, "config": {"retry_min_s": 1.0, "retry_rms": 0.01, "retry_offset_s": 0.2}},
  "stt.sherpa_moonshine_v2": {"enabled": true, "config": {}},
  "stt.sherpa_transducer": {"enabled": true, "config": {}},
  "tts.engine": {"enabled": true, "config": {"voice": "vits-piper-en_US-amy-medium#0", "speed": 1.0, "peak": 0.89,
    "threads": 4, "max_loaded": 2, "warm": true}},
  "tts.piper": {"enabled": true, "config": {"length_scale": 1.0}},
  "tts.kokoro": {"enabled": true, "config": {}},
  "voices.catalog": {"enabled": true, "config": {"rescan_s": 300, "featured": ["amy", "jenny", "lessac", "kristin", "hfc_female"],
    "agent_max_rtf": 0.5, "rtf_file": "/Users/aryavora/.config/mira/voices.rtf.json"}}
 }
}
```
Backend plugins are all enabled because they only register a factory (no model loaded); `stt.engine.kind` picks one.

#### 2.4.5 Deployment values that differ from the schema default (written by `mira adopt`, never by the schema)
The example-versus-defaults tests compare values after expanding `~` and `${HOME}` (Pi `/home/mira`, mini `/Users/aryavora`); `${HERE}` defaults never equal a deployment path, which is why those keys are listed here. Pi: `core.engine` plugins, `core.legacy_env` ignore, `wake.kws.kws_dir`, `wake.kws.threshold` 0.05 (code 0.15), `vad.silero.model`, `speech.link.token_file`, `intent.music.url` (mini, code loopback), `intent.music.auth_file`, `brain.hermes.key_file`, `persona.file.path`, `playback.miraaudio.chime`, `reporter.agentos.token_file`, `loop.turns.events_max` 2000 (owner-approved at cutover; code unbounded). Mini: `core.engine`, `core.legacy_env`, `stt.engine.kind` faster-whisper:tiny.en, `server.http.token_file`, `base_dir`, `control.http.enabled`.

#### 2.4.6 Legacy mapping table
`mira docs` generates `docs/KNOBS.md`: one row per knob with plugin, key, type, default, bounds, unit, applies, agent policy, legacy env, legacy flag, and the v1 source line for literals. `tests/test_knob_coverage.py` builds the same table from an AST scan of `mira_listen_v1.py` (every `build_parser` flag, every `os.environ.get("MIRA_...")`, and the literal list of section 2.4.2 marked "(literal)") and of `speech/server_v1.py` (every `os.environ.get`), and fails if any item has no knob or a knob default differs from the v1 value under an empty environment. The table answers "where does this value live" for every knob, including unit limits, plist keys and token paths.
