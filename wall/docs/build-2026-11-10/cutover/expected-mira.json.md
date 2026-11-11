# Expected `mira.json` (Pi) after `mira adopt --write` and the cutover `mira set`

Produced by running the real adopt code on the Pi's live `listen.env` and `mira-listen.service` (copied read-only, no secrets in either: they hold URLs, numbers and token file paths) with the test interpreter, in a throwaway folder that was deleted afterwards. Home on the Pi is `/home/mira`, so `%h` in the unit expands to it. Every key not listed here takes the plugin's schema default (the same default the adopt output carries).

File header: `format` 1, `machine` "listener", `revision` 1 (adopt) then 2 (after the cutover `mira set`), `core.engine` "legacy" until `mira set core.engine=plugins --offline`, `core.legacy_env` "ignore".

## A. What adopt writes (values that differ from the schema default, or come from the live environment)

| key | value | where it comes from |
|---|---|---|
| speech.link.url | `http://127.0.0.1:8766` | `listen.env` MIRA_SPEECH_URL (the splitter on the Pi, added 18:02 today); the loopback address passes the plugin's host rule |
| speech.link.token_file | `/home/mira/.config/mira/speech.token` | unit flag `--speech-token-file` |
| wake.kws.threshold | 0.05 | `listen.env` MIRA_KWS_THRESHOLD (tuned 2026-11-09 from room recordings; schema default 0.15, min 0.01) |
| wake.kws.score | 1.0 | `listen.env` MIRA_KWS_SCORE (equals the schema default) |
| wake.kws.kws_dir | `/home/mira/mira-listen/kws` | unit flag `--kws-dir` (schema default `${HERE}/../voice/models/kws`) |
| wake.kws.keywords, threads | `keywords.txt`, 1 | defaults |
| vad.silero.model | `/home/mira/mira-listen/models/silero_vad.onnx` | unit flag `--vad-model` |
| agc.mic.on | true | `listen.env` MIRA_AGC=1 |
| agc.mic.target | 0.05 | `listen.env` MIRA_AGC_TARGET (tuned 2026-11-09; equals the schema default) |
| agc.mic.max_gain | 10.0 | `listen.env` MIRA_AGC_MAX_GAIN (tuned; equals the default) |
| agc.mic.rest | 3.0 | `listen.env` MIRA_AGC_REST (tuned; equals the default) |
| agc.mic.gate | 1.8 | `listen.env` MIRA_AGC_GATE (tuned; equals the default) |
| intent.music.url | `http://100.77.98.89:4534` | `listen.env` MIRA_MUSIC_URL (schema default is loopback 4534) |
| intent.music.auth_file | `/home/mira/.config/mira/music_auth.json` | unit flag `--music-auth-file` |
| brain.hermes.url | `http://127.0.0.1:8642` | `listen.env` MIRA_HERMES_URL (equals the default) |
| brain.hermes.key_file | `/home/mira/.config/mira/hermes_api_key` | unit flag `--hermes-key-file` |
| reporter.agentos.url | `http://127.0.0.1:3456/api/hooks/voice` | `listen.env` MIRA_VOICE_URL (equals the default) |
| reporter.agentos.token_file | `/home/mira/.config/mira/voice_token` | unit flag `--voice-token-file` |
| persona.file.path | `/home/mira/mira-listen/MIRA_WALL_IDENTITY.md` | frozen from the file beside the live install (source `install`; repair pass for drift.md B1). Before the repair adopt wrote `${HERE}/MIRA_WALL_IDENTITY.md`, which points into the release; the old section B step is then a no-op |
| playback.miraaudio.chime | `/home/mira/mira-listen/assets/offline.wav` | same: frozen from the install, source `install` |
| playback.miraaudio.bin | `mira-audio` | default (no MIRA_AUDIO_BIN live); the patched helper that applies the 12 percent level |
| playback.miraaudio.night_window | "" (off) | nothing in the legacy world has it; adopt cannot produce 23:00-06:00 |
| playback.miraaudio.night_gain | 0.5 | schema default |
| tts.remote.voice, speed, peak | "", 0.0, 0.0 | defaults (no MIRA_VOICE live); the live voice (Kokoro am_eric) is chosen by the aiserver server's default |
| control.http | enabled false | spec default until M8 |
| source.wav | enabled false, path "" | default |
| core.unit_memory_max | "200M" | unit `MemoryMax=` |
| core.unit_cpu_quota | "40%" | unit `CPUQuota=` |
| core.unit_nice | 5 | unit `Nice=` |
| core.unit_restart | "always" | unit `Restart=` |
| core.unit_restart_sec | 3.0 | unit `RestartSec=` |
| core.crash_window_s, crash_guard_after, crash_legacy_after, crash_guard_revert_s | 600.0, 3, 4, 1800.0 | defaults |

All 17 listener plugins are present; enabled everywhere except `source.wav` and `control.http`. The dry run lists only values with source env or flag, or that differ from the default, so the AGC values that equal their default are visible only with `mira adopt --all`; the persona path and chime now show in the plain dry run (source `install`) (AGC shows because its source is the environment).

## B. Set after adopt, before the engine switch (runbook, drift.md deltas)

Command: `mira set persona.file.path=/home/mira/mira-listen/MIRA_WALL_IDENTITY.md playback.miraaudio.chime=/home/mira/mira-listen/assets/offline.wav playback.miraaudio.night_window=23:00-06:00 persona.file.reread=turn --offline --reason cutover` (revision becomes 2; proven in the temp run for the first three, rc 7 means pending restart, which is expected with the listener still the old process).

| key | value | why |
|---|---|---|
| persona.file.path | `/home/mira/mira-listen/MIRA_WALL_IDENTITY.md` | the shared live persona, not a release folder |
| playback.miraaudio.chime | `/home/mira/mira-listen/assets/offline.wav` | the shared asset; releases hold code only |
| playback.miraaudio.night_window | `23:00-06:00` | owner decision (addendum); local time on the Pi (America/New_York) |
| playback.miraaudio.night_gain | 0.5 (unchanged) | owner may prefer 0.7: with the helper's 12 percent the night level is 0.12 x 0.5, about 6 percent |
| persona.file.reread | `turn` | persona edits take effect on the next turn with no listener restart |
| core.engine | `plugins` | step 4 of the cutover, last |

## C. What the regenerated mirror files must say afterwards (check them)

`~/.config/mira/listen.env` (the old file is kept as `listen.env.bak-adopt-<date>`) must contain: `MIRA_SPEECH_URL=http://127.0.0.1:8766`, `MIRA_SPEECH_TOKEN_FILE=/home/mira/.config/mira/speech.token`, `MIRA_KWS_THRESHOLD=0.05`, `MIRA_KWS_SCORE=1`, `MIRA_AGC=1`, `MIRA_AGC_TARGET=0.05`, `MIRA_AGC_MAX_GAIN=10`, `MIRA_AGC_REST=3`, `MIRA_AGC_GATE=1.8`, `MIRA_HERMES_URL=http://127.0.0.1:8642`, `MIRA_VOICE_URL=http://127.0.0.1:3456/api/hooks/voice`, `MIRA_MUSIC_URL=http://100.77.98.89:4534`, `MIRA_IDENTITY_FILE=/home/mira/mira-listen/MIRA_WALL_IDENTITY.md`, `MIRA_CHIME=/home/mira/mira-listen/assets/offline.wav`, `MIRA_AUDIO_BIN=mira-audio` (and the other env knobs at their defaults). It must NOT contain any path under `releases/`. `legacy.args` must end with `--identity /home/mira/mira-listen/MIRA_WALL_IDENTITY.md --chime /home/mira/mira-listen/assets/offline.wav`. The night window and gain have no legacy form, so `mira rollback` reports them as dropped; the helper's 12 percent stays either way.
