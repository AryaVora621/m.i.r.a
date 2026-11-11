
### 2.4 Config file format

#### 2.4.1 File, layers and editing rule
- One file per machine. Pi: `/home/mira/.config/mira/mira.json` (user `mira`, mode 0600). Mini: `/Users/aryavora/.config/mira/speech.json` (mode 0600).
- Siblings live in the folder of the config file actually in use (`cfgdir`), never in a hard-coded `~/.config/mira`: `<file>.lastgood` (last file that booted cleanly), `<file>.lock` (flock), `history/<machine>.<revision>.json` (previous files, last `control.http.history_keep`), `changes.jsonl` (change log, 0600), `state/starts` (launcher start times). Pi only, and only when `Core.from_file(..., mirror=True)`: `listen.env` and `legacy.args` (the legacy mirror, 2.5.6). The shadow runner and every test use `mirror=False` and a temp `HOME`; a test must never write under the real `~/.config/mira`.
- Shape:
```json
{"format": 1, "machine": "listener", "revision": 12,
 "core": {"<key>": <value>, ...},
 "plugins": {"<plugin name>": {"enabled": true, "config": {"<key>": <value>, ...}}}}
```
- A sparse file is valid: a missing key takes the next layer. Layers, highest first: flags explicitly present on argv (detected by a second parse with `argparse.SUPPRESS` defaults), the file, legacy `MIRA_*` env (only while `core.legacy_env == "fallback"`), schema default. `mira adopt` writes every key and sets `legacy_env` to `"ignore"`.
- Keys starting with `$` (for example `"$comment"`) are ignored. Unknown plugin or key: boot fails loud naming it and the closest known name (difflib); a live edit naming it is refused.
- The core is the only writer while the process runs. It stores the sha256 of the file it last wrote; a write that finds a different hash refuses with `ConflictError("file_changed")` and the hint "run mira config reload". `mira config reload` (owner) validates the file on disk and applies the difference as one batch with `actor="file"`. `mira config edit` (owner) copies the file to a temp path, opens `$EDITOR`, validates, and submits the difference as one batch.
- Boot with an invalid file: the core loads `<file>.lastgood`, logs `config_error` loudly, and `status.config_error` says why. If there is no valid last-good file, boot exits 78.
- Value syntax: `path` values may use `~`, `${HOME}`, `${HERE}`. `map[str,str]` values keep insertion order (JSON object order is preserved by `json.load`).
- Migrations: `mirakit/store.py: MIGRATIONS = {(format, "old.key"): "new.key"}`; empty in format 1.

#### 2.4.2 Every knob, with bounds (normative; the schema in code must match this table)
Legend: `L` live, `R` rebuild, `S` restart. Agent column: `deny`, or `min..max step s` for `bounded` (always a trial). Legacy: the v1 env var and/or argparse dest (`--flag`). Defaults are the v1 code defaults; the Pi and mini values that differ are in 2.4.3 and 2.4.4.

**core (listener)** (pseudo-plugin, address `core.<key>`)
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| engine | enum legacy,plugins | "legacy" | | S | deny | MIRA_ENGINE (bootstrap only) | which listener the launcher runs |
| legacy_env | enum fallback,ignore | "fallback" | | S | deny | | whether MIRA_* env fills keys the file lacks |
| unit_memory_max | str `^\d+[KMG]$` | "200M" | | S | deny | unit MemoryMax | rendered into the systemd drop-in |
| unit_cpu_quota | str `^\d{1,3}%$` | "40%" | | S | deny | unit CPUQuota | |
| unit_nice | int | 5 | -10..19 | S | deny | unit Nice | |
| unit_restart | enum always,on-failure | "always" | | S | deny | unit Restart | |
| unit_restart_sec | float | 3.0 | 1..60 s | S | deny | unit RestartSec | |
| crash_window_s | float | 600 | 60..3600 s | S | deny | | window for counting unplanned starts |
| crash_guard_after | int | 3 | 2..10 | S | deny | | unplanned starts in the window that trigger the boot guard |
| crash_legacy_after | int | 4 | 3..20 | S | deny | | unplanned starts that switch the launcher to legacy (must exceed guard_after) |
| crash_guard_revert_s | float | 1800 | 300..86400 s | S | deny | | how far back the boot guard reverts agent changes |

**loop.turns** (core slot, `core=True`)
| key | type | default | bounds | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| wait_speech | float | 4.0 | 1..15 s | L | 2..8 step 1 | --wait-speech | how long she waits for you to start talking after her name |
| max_s | float | 10.0 | 3..30 s | L | 6..15 step 2 | --max-s | longest request she records |
| followup_s | float | 8.0 | 0..30 s | L | 4..15 step 2 | --followup-s | how long she keeps listening without her name after answering |
| followup_wait_s | float | 1.0 | 0.5..3 s | L | deny | (literal L875) | wait for speech once the follow-up VAD fires |
| followup_seed_chunks | int | 5 | 1..15 chunks(100ms) | S | deny | (literal) | audio kept ahead of a follow-up |
| wake_seed_chunks | int | 3 | 1..10 chunks(100ms) | S | deny | (literal) | VAD seed after a wake; the rest of pre-roll goes only to STT |
| preroll_chunks | int | 15 | 5..30 chunks(100ms) | S | deny | (literal L858) | pre-roll length; must exceed wake_seed_chunks and be at least followup_seed_chunks |
| offline_text | str | "Speech is offline" | 1..80 chars | L | deny | (literal) | wall text when speech is offline |
| events_max | int | 0 | 0..100000 | S | deny | | in-memory event trail length, 0 = unbounded (legacy) |
| log_text | bool | true | | L | deny | | heard text and replies in the journal |
| sigterm_idle | bool | false | | S | deny | | SIGTERM posts a final idle and exits through the finally |

**source.pipewire**
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| record_bin | str | "pw-record" | 1..200 chars | S | deny | (literal) | capture command (rate, channels, format fixed) |
| queue_chunks | int | 600 | 50..3000 chunks(100ms) | S | deny | (literal L139) | audio buffered before the oldest is dropped |

**source.wav** (`default_enabled=False`)
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| path | path | "" | | S | deny | --wav | WAV file replacing the mic |
| tail_s | float | 1.0 | 0..5 s | S | deny | (literal) | zeros appended so the spotter flushes at EOF |

**mute.zero**
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| mute_s | float | 10.0 | 2..60 s | L | deny | (literal) | zero-sample time before the mic counts as muted |
| mute_share | float | 0.99 | 0.5..1.0 share | L | deny | (literal) | share of exact zeros that makes a block muted |
| live_share | float | 0.90 | 0.5..1.0 share | L | deny | (literal) | a block under this share of zeros is live; must not exceed mute_share |
| block_s | float | 1.0 | 0.5..5 s | S | deny | (literal) | verdict block length |
| repeat_s | float | 60.0 | 10..600 s | L | deny | (literal) | re-post of muted so a restarted Agent OS learns it |

**agc.mic**
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| on | bool | true | | S | deny | MIRA_AGC, --agc/--no-agc | software gain stage on the mic |
| target | float | 0.05 | 0.005..0.3 rms | L | 0.03..0.10 step 0.01 | MIRA_AGC_TARGET, --agc-target | speech level the gain aims for |
| max_gain | float | 10.0 | 1..40 x | L | 4..14 step 2 | MIRA_AGC_MAX_GAIN, --agc-max-gain | most the gain may rise |
| rest | float | 3.0 | 1..40 x | L | deny | MIRA_AGC_REST, --agc-rest | gain in a quiet room; re-clamped to 1..max_gain on apply |
| gate | float | 1.8 | 1..6 x floor | L | deny | MIRA_AGC_GATE, --agc-gate | how far above the noise floor counts as speech |
| release | float | 0.25 | 0.01..1 per chunk | L | deny | MIRA_AGC_RELEASE | how fast gain rises |
| decay | float | 0.03 | 0.001..0.5 per chunk | L | deny | MIRA_AGC_DECAY | how fast gain relaxes to rest in silence |
| min_rms | float | 0.001 | 0..0.05 rms | L | deny | MIRA_AGC_MIN_RMS | absolute floor of the speech test |
| ceiling | float | 0.9 | 0.1..1.0 peak | L | deny | MIRA_AGC_CEILING | no-clip limit |
| floor_init | float | 0.003 | 0..0.1 rms | S | deny | MIRA_AGC_FLOOR | starting noise floor (the JV603 hiss) |
| floor_rate_speech | float | 0.0005 | 0..0.1 | L | deny | (literal L225) | noise floor tracking rate during speech |
| floor_rate_low | float | 0.2 | 0..1 | L | deny | (literal) | rate when the chunk is below the floor |
| floor_rate_mid | float | 0.05 | 0..1 | L | deny | (literal) | rate otherwise |
Cross rule: `rest <= max_gain` (apply re-clamps exactly as `MicAGC.__init__`). `reads=("reporter.agentos.level_scale",)`.

**wake.kws** (every knob is owner only; all four are S, restart class: P0.5 failed the gate of 2.3.3)
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| kws_dir | path | "${HERE}/../voice/models/kws" | | S | deny | MIRA_KWS_DIR, --kws-dir | spotter model folder |
| keywords | str | "keywords.txt" | 1..120 chars | S | deny | --keywords | keyword file inside kws_dir |
| threshold | float | 0.15 | 0.01..0.5 score | S | deny | MIRA_KWS_THRESHOLD, --kws-threshold | spotter sensitivity, lower fires easier (the transcript gate decides) |
| score | float | 1.0 | 0.5..3.0 | S | deny | MIRA_KWS_SCORE, --kws-score | keyword boost (raising it hurt recall) |
| threads | int | 1 | 1..2 threads | S | deny | MIRA_KWS_THREADS, --kws-threads | spotter CPU threads |

**vad.silero** (live knobs take effect at the next capture, F5)
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| model | path | "${HERE}/models/silero_vad.onnx" | | S | deny | MIRA_VAD_MODEL, --vad-model | Silero model |
| threshold | float | 0.5 | 0.1..0.9 prob | L | deny | --vad-threshold | speech detection sensitivity |
| end_silence | float | 0.7 | 0.2..3.0 s | L | 0.5..1.2 step 0.1 | --end-silence | silence that ends a request; also adds that much latency |
| min_speech | float | 0.15 | 0.05..1.0 s | L | deny | (literal) | shortest sound that counts as speech |
| max_speech | float | 30.0 | 5..60 s | L | deny | (literal) | the VAD's own segment cap |
| threads | int | 1 | 1..2 threads | L | deny | (literal) | VAD CPU threads |
| buffer_s | float | 30.0 | 10..120 s | L | deny | (literal) | VAD buffer |
FIXED: `window_size=512`, `sample_rate=16000`.

**speech.link** (url host must be loopback or 100.64.0.0/10)
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| url | url | "http://100.77.98.89:8765" | | L | deny | MIRA_SPEECH_URL, --speech-url | speech server (Tailscale on purpose: bearer over HTTP) |
| token_file | path secret | "" | | L | deny | MIRA_SPEECH_TOKEN_FILE, --speech-token-file | bearer file, read on every call |
| stt_timeout_s | float | 30.0 | 5..120 s | L | deny | (literal L439) | |
| tts_timeout_s | float | 45.0 | 5..120 s | L | deny | (literal L450) | |

**stt.remote**: no knobs (`needs=("speech",)`, provides `stt`).

**tts.remote** (`needs=("speech",)`)
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| voice | str | "" | 0..200 chars | L | deny | MIRA_VOICE, --voice | voice sent per request; "" = the mini's current voice |
| speed | float | 0.0 | 0 or 0.5..1.6 | L | deny | | per-request speed override, 0 = server setting |
| peak | float | 0.0 | 0 or 0.3..0.98 | L | deny | | per-request loudness override, 0 = server setting |
| chunk_limit | int | 280 | 80..600 chars | L | deny | (literal) | longest spoken chunk |
| max_chunks | int | 3 | 1..10 | L | deny | (literal) | chunks spoken per reply (rest dropped) |
| pin_voice | bool | true | | L | deny | | later chunks of one reply use the voice id the first chunk got |

**gate.wake** (owner only; validator: the compiled gate must accept `Mira, Meera, Beerah, Miura, Myra, Viera, Vera` and reject `Mirror, Maria, Mara, Miranda, miracle, Mia, Siri`, the `test_unit.py` tables, copied into `gate_wake.py` as `GATE_CASES`)
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| wake_word | regex | `[a-z]{0,2}(?:i\|ee\|ea\|e\|y\|ie\|iu)r+(?:a\|ah\|ar\|uh)h?` | | L | deny | (literal L578) | the spellings of her name the transcript gate accepts |
| wake_prefixes | list[str] | ["hey","hi","ok","okay"] | 0..10 items | L | deny | (literal) | words allowed before her name |
| hallucinations | list[str] | the 10 v1 phrases | 0..50 items | L | deny | (literal L55) | transcripts treated as empty |
FIXED (freeze list): verify on wake clips, empty is a reject, a reject opens no window. Compiled patterns must equal v1 byte for byte: `WAKE_OK = ^(?:(?:<prefixes joined by |>)\W+)?(?:<wake_word>)\b` and `WAKE_PREFIX = ^\s*(?:(?:<prefixes>)\W+)?(?:<wake_word>)\b[\s,.!?:;-]*`, both `re.I`.

**intent.music** (`toggle="live"`; patterns validated by compiling and by the `test_unit.py` music table copied as `MUSIC_CASES`)
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| priority | int | 100 | 0..1000 | L | deny | | higher runs first among intents |
| url | url | "http://127.0.0.1:4534" | | L | deny | MIRA_MUSIC_URL, --music-url | music-api |
| auth_file | path secret | "" | | L | deny | MIRA_MUSIC_AUTH_FILE, --music-auth-file | JSON {u,t,s}, read per call |
| timeout_s | float | 8.0 | 1..30 s | L | deny | (literal L531) | |
| volume_step | int | 10 | 1..50 percent | L | deny | (literal L553) | volume up or down step |
| patterns | map[str,str] | the six v1 regexes, keys in order status, volume_up, volume_down, pause, next, play | exactly those keys | L | deny | (literal L562) | tried in order, fullmatch inside `(please )?(...)( please)?` |
| replies | map[str,str] | play "Playing.", pause "Paused.", next "Next song.", volume "Volume {n}.", echo_down "The speaker is not connected.", playing "Music is playing.", paused "Music is paused.", nothing "Nothing is playing.", done "Done.", unreachable "I could not reach the music player." | exactly those keys | L | deny | (literals) | spoken confirmations |

**brain.hermes** (url host loopback only)
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| url | url | "http://127.0.0.1:8642" | | L | deny | MIRA_HERMES_URL, --hermes-url | Hermes api_server |
| key_file | path secret | "" | | L | deny | MIRA_HERMES_KEY_FILE, --hermes-key-file | bearer file, read per call |
| session_ttl | float | 180.0 | 30..1800 s | L | 60..600 step 60 | --session-ttl | idle time before she starts a fresh conversation |
| session_timeout_s | float | 15.0 | 3..60 s | L | deny | (literal L473) | |
| stream_timeout_s | float | 150.0 | 10..600 s | L | deny | (literal L491) | per socket read |
| title_prefix | str | "Mira wall" | 1..60 chars | L | deny | (literal L469) | |
| fail_reply | str | "I cannot reach Hermes right now." | 1..200 chars | L | deny | (literal L844) | |
| empty_reply | str | "I have nothing to add." | 1..200 chars | L | deny | (literal L841) | |

**persona.file**
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| path | path | "${HERE}/MIRA_WALL_IDENTITY.md" | | S | deny | MIRA_IDENTITY_FILE, --identity | persona sent as per-turn instructions |
| fallback | str | "You are Mira." | 1..200 chars | L | deny | (literal L660) | used when the file is missing |
| reread | enum start,turn | "start" | | L | deny | | start: read once (v1); turn: mtime-cached reread before each ask |

**playback.miraaudio**
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| bin | str | "mira-audio" | 1..200 chars | L | deny | MIRA_AUDIO_BIN, --mira-audio | playback and duck command |
| call_timeout_s | float | 120.0 | 5..600 s | L | deny | (literal L685) | |
| level_lag | float | 0.15 | 0..1 s | L | deny | MIRA_LEVEL_LAG, --level-lag | delay from say start to sound, for the ring |
| chime | path | "${HERE}/assets/offline.wav" | | L | deny | MIRA_CHIME, --chime | sound when speech is offline |
| envelope_step_s | float | 0.1 | 0.05..0.5 s | L | deny | (literal L633) | reply level step |
| follow_join_s | float | 1.0 | 0.1..5 s | L | deny | (literal L791) | |
`reads=("reporter.agentos.level_scale",)`.

**reporter.agentos** (url host loopback only, or "" to disable posts)
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| url | url | "http://127.0.0.1:3456/api/hooks/voice" | | L | deny | MIRA_VOICE_URL, --voice-url | Agent OS voice hook |
| token_file | path secret | "" | | L | deny | MIRA_VOICE_TOKEN_FILE, --voice-token-file | bearer file, read per post |
| error_hold | float | 4.0 | 0..30 s | L | deny | --error-hold | how long error stays on the wall |
| min_gap | float | 0.5 | 0.1..5 s | L | deny | MIN_GAP | state post spacing |
| level_gap | float | 0.2 | 0.05..2 s | L | deny | LEVEL_GAP | level post spacing |
| level_step | float | 0.03 | 0.005..0.5 | L | deny | LEVEL_STEP | smallest level move worth a post |
| post_timeout_s | float | 3.0 | 0.5..30 s | L | deny | (literal L410) | |
| close_wait_s | float | 20.0 | 1..120 s | S | deny | (literal L419) | |
| level_scale | float | 8.0 | 1..32 x | L | deny | (literal x3: L241, L704, L641) | ring level = rms times this, capped at 1; the one copy |

**control.http** (`required=False`, `default_enabled=False`, `toggle="restart"`; every knob owner only)
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| bind | str | "127.0.0.1" | loopback only | S | deny | | |
| port | int | 8650 | 1024..65535 | S | deny | | |
| owner_token_file | path secret | "~/.config/mira/control.owner.token" | | L | deny | | owner scope |
| agent_token_file | path secret | "/etc/mira/control.agent.token" | | L | deny | | agent scope (Hermes) |
| speech_url | url | "http://100.77.98.89:8765" | loopback or tailnet | L | deny | | mini /ctl/v1 base |
| speech_token_file | path secret | "~/.config/mira/speech.control.token" | | L | deny | | the mini's Pi token |
| agents_locked | bool | false | | L | deny | | kill switch: every agent write and preview refused |
| agent_requires_voice_turn | bool | true | | L | deny | | agent writes and previews only during the brain phase of a voice turn |
| agent_writes_per_10min | int | 12 | 0..60 | L | deny | | undo and confirm are not counted |
| agent_writes_per_knob_10min | int | 3 | 1..20 | L | deny | | |
| agent_undo_window_s | float | 86400 | 600..604800 s | L | deny | | agents undo only agent changes this recent |
| trial_default_s | float | 600 | 60..3600 s | L | deny | | |
| trial_max_s | float | 1800 | 60..7200 s | L | deny | | must be at least trial_default_s |
| session_max_s | float | 900 | 60..3600 s | L | deny | | hard cap on a voice tuning session |
| session_max_changes | int | 12 | 1..50 | L | deny | | |
| session_followup_s | float | 15.0 | 8..30 s | L | deny | | follow-up window while a session is open |
| session_idle_s | float | 120 | 30..900 s | L | deny | | no voice turn and no agent call this long ends the session |
| rebuild_quiet_s | float | 10.0 | 0..120 s | L | deny | | no rebuild this soon after a wake or a turn |
| previews_per_10min | int | 6 | 0..30 | L | deny | | agent previews |
| preview_max_chars | int | 200 | 20..600 chars | L | deny | | |
| preview_quiet | time_window | "23:00-06:00" | | L | deny | | agent previews refused in this local window |
| preview_text | str | "This is {alias}. This is how I would sound." | 1..200 chars | L | deny | | default sample sentence |
| changelog_max_bytes | int | 5000000 | 100000..50000000 bytes | L | deny | | rotate changes.jsonl to .1 above this |
| history_keep | int | 50 | 5..500 | L | deny | | |
| wait_s_default | float | 5.0 | 0..30 s | L | deny | | how long a write waits for its apply |

**core (speech)**: `engine` (enum legacy,plugins, "legacy", S), `legacy_env` (as listener), `unit_nice` (int 5, -10..19, S, plist Nice), `unit_throttle_s` (int 15, 1..300, S, plist ThrottleInterval), `unit_keep_alive` (bool true, S, plist KeepAlive), `crash_window_s`, `crash_guard_after`, `crash_legacy_after`, `crash_guard_revert_s` (as listener). All deny.

**server.http** (speech; binds: no wildcard, each loopback or tailnet)
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| bind | list[str] | ["127.0.0.1","100.77.98.89"] | 1..4 items | S | deny | MIRA_SPEECH_BIND | |
| port | int | 8765 | 1024..65535 | S | deny | MIRA_SPEECH_PORT | |
| token_file | path secret | "~/.config/mira/speech.token" | | S | deny | MIRA_SPEECH_TOKEN_FILE | data-plane bearer, read once at start (v1) |
| base_dir | path | "~/Library/Application Support/mira-speech" | | S | deny | MIRA_SPEECH_DIR | models live in base_dir/models |
| max_stt_s | float | 30.0 | 5..60 s | S | deny | (MAX_STT_BYTES) | |
| max_tts_chars | int | 600 | 100..2000 chars | L | deny | (MAX_TTS_CHARS) | |
| tts_body_max | int | 4096 | 1024..65536 bytes | L | deny | (literal) | |

**control.http (speech)**: `owner_token_file` ("~/.config/mira/control.owner.token"), `pi_token_file` ("~/.config/mira/control.pi.token"), `trial_default_s` 600, `trial_max_s` 1800, `agent_writes_per_10min` 12, `busy_defer_s` (float 10, 0..120 s: an expiry swap waits until no /tts or /stt finished this recently), `history_keep` 50, `changelog_max_bytes` 5000000. All L, deny. `required=False`, `default_enabled=False`.

**stt.engine**
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| kind | str | "sherpa-moonshine:sherpa-onnx-moonshine-base-en-int8" | `backend[-fp32]:name`, backend registered | R | deny | MIRA_STT | backend and model, legacy syntax |
| threads | int | 4 | 1..8 | R | deny | MIRA_THREADS | STT threads |
| warm | bool | true | | L | deny | | warm a new model with 1 s of silence before the swap |

**stt.faster_whisper**: `compute` (enum int8,int8_float32,int16,float32; "int8"; R; MIRA_STT_COMPUTE), `beam` (int 1, 1..5, L, MIRA_STT_BEAM), `prompt` (str `"Vocabulary: " + DEFAULT_VOCAB + "."`, 0..1000 chars, L, MIRA_STT_PROMPT, "" turns it off), `hotwords` (str "", 0..400 chars, L, MIRA_STT_HOTWORDS), `language` (str "en", L). All deny. `condition_on_previous_text=False` is FIXED.
**stt.sherpa_moonshine**: `retry_min_s` (float 1.0, 0..5 s, L), `retry_rms` (float 0.01, 0..0.1, L), `retry_offset_s` (float 0.2, 0..1 s, L). All deny.
**stt.sherpa_whisper**, **stt.sherpa_moonshine_v2**, **stt.sherpa_transducer**: no knobs (`-fp32` and the nemo variant come from `stt.engine.kind`).

**tts.engine**
| key | type | default | bounds (unit) | ap | agent | legacy | description |
|---|---|---|---|---|---|---|---|
| voice | voice | "vits-piper-en_US-amy-medium#0" | a usable catalog voice | R (L when the same multi-speaker model) | trial; only usable voices with lang en_*, and featured or measured rtf at most voices.catalog.agent_max_rtf | MIRA_TTS_DIR + MIRA_TTS_VOICE | her voice |
| speed | float | 1.0 | 0.5..1.6 x | L | 0.8..1.3 step 0.1 | (literal generate speed) | how fast she talks |
| peak | float | 0.89 | 0.3..0.98 | L | 0.6..0.95 step 0.05 | MIRA_TTS_PEAK | how loud her replies are |
| threads | int | 4 | 1..8 | R | deny | MIRA_TTS_THREADS (default MIRA_THREADS) | |
| max_loaded | int | 2 | 1..3 | L | deny | | TTS models resident at once (active plus one candidate) |
| warm | bool | true | | L | deny | | warm a new model with one short phrase before the swap |

**tts.piper**: `length_scale` (float 1.0, 0.5..2.0, R, MIRA_TTS_LENGTH_SCALE, deny). **tts.kokoro**: no knobs.
**voices.catalog**: `rescan_s` (float 300, 30..3600 s, L), `featured` (list[str] ["amy","jenny","lessac","kristin","hfc_female"], L: offered first in a dialogue), `agent_max_rtf` (float 0.5, 0.1..2.0, L: a non-featured voice is settable by voice only when its measured real-time factor is at most this, so a slow Kokoro voice cannot push replies past the 45 s TTS timeout), `rtf_file` (path "~/.config/mira/voices.rtf.json", S). All deny. `mira voices bench` (owner, offline on the mini) renders one sentence per usable voice and writes `rtf_file`.

Agent-settable knobs, complete list (everything else is deny): `agc.mic.target`, `agc.mic.max_gain`, `vad.silero.end_silence`, `loop.turns.wait_speech`, `loop.turns.max_s`, `loop.turns.followup_s`, `brain.hermes.session_ttl` on the Pi; `tts.engine.voice`, `tts.engine.speed`, `tts.engine.peak` on the mini. None is rebuild or restart class on the Pi.

Topics, spoken names, effects and phrases (in the specs; the generated skill glossary comes from these):
| key | topic | spoken | effect when raised | phrases |
|---|---|---|---|---|
| tts.engine.voice | voice | my voice | | "change your voice", "tune your voice model", "sound different" |
| tts.engine.speed | voice | how fast I talk | talks faster | "you talk too fast", "slow down", "speak faster" |
| tts.engine.peak | voice | how loud I talk | louder replies | "too quiet", "too loud", "speak up" |
| agc.mic.target | hearing | microphone boost | hears quiet speech from farther away, and more room noise | "you can't hear me", "you don't hear me from my desk" |
| agc.mic.max_gain | hearing | most microphone boost | quiet rooms get boosted more | "you miss me when I talk softly" |
| vad.silero.end_silence | timing | how long I wait for you to finish | fewer cut-offs, slower answers | "you cut me off", "you wait too long" |
| loop.turns.wait_speech | timing | how long I wait for you to start | more time after my name | "you stop listening before I talk" |
| loop.turns.max_s | timing | longest request | longer requests fit | "you stop me mid sentence" |
| loop.turns.followup_s | timing | how long I keep listening after answering | longer chance to reply without my name | "you stop listening too soon" |
| brain.hermes.session_ttl | brain | how long I remember our conversation | remembers longer between questions | "you forget what we were talking about" |
| wake.kws.threshold (owner) | wake | wake sensitivity | fires less often | "you wake up too much", "you don't wake up" |
| gate.wake.wake_word (owner) | wake | the names I answer to | | "you answer to the wrong name" |
