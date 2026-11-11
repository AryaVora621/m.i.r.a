# Drift audit: what changed live on 2026-11-10 and what the plugin release does about it

Read-only audit. Live facts were read from the Pi and the mini (nothing changed, nothing restarted, no sound). The adopt behaviour was proven by running the real `mira adopt --write` and `mira set --offline` from this repo's code in a temp release folder with the Pi's real `listen.env` and unit text (temp dir deleted afterwards).

Still true on the machines (spec P0 step 1): Pi `mira_listen.py` md5 8990842340a5ce68aa11515d0432f4ed equals `mira_listen_v1.py`; the mini's `server.py` md5 ffe5ae0ee3881249f1102b27dd1755c4 equals `speech/server_v1.py`. Today's work did not touch either file, so the freeze still holds.

## 0. Verdict and the changes to the cutover runbook

Two things would regress at the switch-over if the runbook in spec 7.1 is followed as written. Everything else is a decision or a doc fix.

| # | What | Severity |
|---|---|---|
| B1 | `mira adopt` leaves `persona.file.path` and `playback.miraaudio.chime` as `${HERE}/...`. After the switch `${HERE}` is `releases/<sha>/`, which holds code only. She would answer with no persona ("You are Mira.") and the offline chime would be missing. The legacy mirror writes the same dead paths into `listen.env` and `legacy.args`, so `mira rollback` and the last-resort path lose them too. | blocker |
| B2 | The repo persona is the old text and the knowledge package's persona is built on the 1743 byte original. Installing either (as the knowledge README and `deploy/README.md` step 2 say) overwrites today's two live edits. | blocker |
| D1 | Her speaking level is a file plus a setter on the Pi (12 percent) and the night gain multiplies on top of it: about 6 percent at night. | owner decision |
| D2 | The Hermes self-tuning knowledge says things that are wrong for today's live voice path (Kokoro on aiserver through the splitter), and duplicates the loudness control she already has. | fix before the later Hermes step |

Runbook deltas (add to spec 7.1; all as user mira on the Pi unless noted):
1. Before M4/M7, on the Mac: put the persona into the repo (section 1) and commit it.
2. After `mira adopt --write` (7.1 step 2) and before `mira set core.engine=plugins --offline` (step 4), run:
   `mira set persona.file.path=/home/mira/mira-listen/MIRA_WALL_IDENTITY.md playback.miraaudio.chime=/home/mira/mira-listen/assets/offline.wav playback.miraaudio.night_window=23:00-06:00 persona.file.reread=turn --offline --reason cutover`
   Then check `listen.env` has `MIRA_IDENTITY_FILE=/home/mira/mira-listen/MIRA_WALL_IDENTITY.md` and `MIRA_CHIME=/home/mira/mira-listen/assets/offline.wav`, and the tail of `legacy.args` shows the same two absolute paths. (Proven in the temp run: after the `set`, both mirror files carry the absolute paths, `night_window` is `23:00-06:00`, `night_gain` stays 0.5.)
3. Install the merged persona (section 1) before the restart in step 4, so one restart loads it.
4. After the restart, prove the persona loaded: the persona plugin status must say `from_file` true (`mira inventory` or `mira describe persona.file`), and a Hermes reply-length probe (as in the voice-live report: "bring up my 3D printers" 3 words, weather about 22, calendar about 17, no sound) must still be short.
5. Keep the splitter and its unit; do not point `speech.link.url` anywhere else (section 3).
6. Check `md5sum /usr/local/bin/mira-audio` equals `agentic-os:pi/deploy/mira-audio/mira-audio` (8bfbdbbfe656237b42e239cdc1fc36ea today) before and after; the plugin engine calls that same binary.

## 1. The persona file (edited twice on the Pi, not in the repo)

What is live: `/home/mira/mira-listen/MIRA_WALL_IDENTITY.md`, 2460 bytes, owner mira, mtime Nov 10 19:02. Backups beside it: `.bak-brief-20261110` (1743 bytes, the original) and `.bak-sayvol-20261110`. Compared with the repo copy `wall/MIRA_WALL_IDENTITY.md` (which is the original), the live text replaced the one-to-three-sentences bullet with the 25 word hard limit, added the "confirm in five words or fewer" bullet, the screens sentence (printers, school, week, exams, homelab, morning brief, evening recap, build board, a short answer, a list, a timer) and the line about changing her own voice level.

What the release does: `persona.file.path` defaults to `${HERE}/MIRA_WALL_IDENTITY.md`. The live unit has no `--identity` flag and `listen.env` has no `MIRA_IDENTITY_FILE`, so adopt produces the default (proved: the dry run does not even list it, because it equals the default). `Core.here` is the folder above `mirakit/`, resolved through the `current` symlink, so it is `/home/mira/mira-listen/releases/<sha>/`. The manifest ships only `mira_launch.py mira_listen.py mira_listen_v1.py mira_shadow.py mirakit listener_plugins`. The file is not there, and `Persona._load` silently uses the fallback "You are Mira." when it is missing (no error, no lint, only `status().from_file == false`). The legacy mirror also expands `${HERE}` before writing, so `listen.env` gets `MIRA_IDENTITY_FILE=.../releases/<sha>/MIRA_WALL_IDENTITY.md` and `legacy.args` gets `--identity` and `--chime` with the release paths. That means `mira rollback`, the automatic fallback after 4 unplanned starts, and even the "remove the drop-in and run the original file" path (the original `mira_listen.py` reads `MIRA_IDENTITY_FILE` from the same `listen.env`) would run with no persona. All of this was reproduced in the temp run.

What to change:
- Runbook (mandatory, B1): the `mira set` line in the deltas above. It makes the plugin engine and every legacy path read the one shared live file, which is also the file Hermes and the owner edit in place today.
- Repo (B2), in this order:
  1. Now: copy the Pi's live text into `wall/MIRA_WALL_IDENTITY.md` (a copy of it is the first block of the merged file; `sha256sum` must match the Pi) and commit, so a repo-to-Pi copy (deploy README step 2, the knowledge README's persona install) can no longer undo today's edits.
  2. At M7: install `cutover/MIRA_WALL_IDENTITY.merged.md` to the Pi (`cp -p` to `.bak-persona-<date>` first, then `install -o mira -g mira -m 644`) and to the repo in the same step, before the restart.
  3. Do NOT install the knowledge package's `MIRA_WALL_IDENTITY.md` as it stands. It is built on the 1743 byte original and lacks the 25 word limit, the five-word confirmation rule, the screen list and the volume line, and its screens paragraph tells her "A screen goes away on its own after a few minutes", which contradicts today's "never describe how long it stays". Its self-tuning lines wait for M9 (`cutover/persona-after-m9.md`).
- Merged text: today's live text byte for byte, plus one short section ("Where you are": the bedroom, accidental wakes, and "From eleven at night to six in the morning you speak more quietly. You still answer."). The night sentence is true only once the plugin engine runs with `night_window` set, which is why it is added at M7 and not before. 2709 bytes, under the 3000 cap. Not added on purpose: "The wall is quiet from eleven to six" (the owner decided she speaks at night, only quieter), "Most answers are one" (the 25 word rule is stricter), the draft's "By voice you only do easy-to-undo things" paragraph (policy, and it names the mira tools that do not exist yet; Hermes's SOUL.md already carries the voice rules).
- Open policy mismatch for the owner: the live persona's Honesty section lets her send mail or start a print if "he clearly asked" and she confirmed out loud, while Hermes's SOUL.md says nothing of that kind is done from a voice turn. Pre-existing, not changed here.
- With `persona.file.reread=turn` (set in the runbook line, a live knob), a persona edit no longer needs a listener restart; today's two edits each needed one.

Risk if skipped: B1 gives her no persona and no offline chime from the first minute (replies come back long and markdown-ish again), and breaks rollback quietly. B2 silently reverts the short-reply and wall-screen work.

## 2. The listener environment file (speech goes to the splitter)

What is live: `/home/mira/.config/mira/listen.env` (0600, edited 18:02) has `MIRA_SPEECH_URL=http://127.0.0.1:8766`, `MIRA_HERMES_URL=http://127.0.0.1:8642`, `MIRA_VOICE_URL=http://127.0.0.1:3456/api/hooks/voice`, `MIRA_MUSIC_URL=http://100.77.98.89:4534`, `MIRA_KWS_THRESHOLD=0.05`, `MIRA_KWS_SCORE=1.0`, `MIRA_AGC=1`, `MIRA_AGC_TARGET=0.05`, `MIRA_AGC_MAX_GAIN=10`, `MIRA_AGC_REST=3`, `MIRA_AGC_GATE=1.8`. The unit passes the path flags `--kws-dir --vad-model --speech-token-file --hermes-key-file --voice-token-file --music-auth-file`. (Backups: `listen.env.bak-split-20261110`, `listen.env.bak-tune-20261109`.)

What the release does: adopt reads exactly these two sources (the unit's ExecStart and `listen.env`). Proved by running it: `speech.link.url` becomes `http://127.0.0.1:8766` (source env). The `speech.link` validator accepts loopback (checked: `127.0.0.1` and `localhost` pass, a LAN address is refused, the aiserver tailnet address passes), so adopt and `mira set` both keep it. Every value in the file survives, and the mirror regenerated by adopt writes `MIRA_SPEECH_URL=http://127.0.0.1:8766` back, plus the other env knobs. Full expected output: `cutover/expected-mira.json.md`. The adopt dry run will show the tuned values with source env or flag, but it hides values equal to the code default, so persona path, chime and AGC are not listed unless the owner passes `--all`.

What to change: nothing in the env mapping. Runbook: review the dry run with `mira adopt --all` once. Repo docs: `deploy/listen.env.example` and `deploy/mira.example.json` still show the Tailscale mini address for `speech.link.url`. Anyone who installs the example, or `mira deploy` shipping it, bypasses the splitter and drops her back to the mini's Piper voice (STT keeps working). Change both examples to the loopback address with a comment naming the splitter, or add a `mira check` finding for it.

Risk if skipped: none at cutover from adopt itself; the examples are a later trap.

## 3. Can the plugin engine express "tts from one address, stt from another, with a fallback"?

No. `speech.link` has one `url` and one `token_file`; `stt.remote` and `tts.remote` both reuse that one `Speech` object (`tts.remote` has voice, speed, peak, chunk knobs only, no address). There is no fallback: any URLError, timeout or HTTP error raises `SpeechOffline`, which plays the offline chime. The aiserver has no `/stt`, so pointing `speech.link.url` at it would break hearing. The splitter on 127.0.0.1:8766 must therefore stay, unchanged, as the one speech address. It already handles the parts the plugin cannot: it drops the listener's bearer before calling aiserver (different token), re-reads the aiserver token per request, retries on the mini on 5xx, 401, 403, timeout or refusal, and skips aiserver for 60 seconds after a failure. The plugin sends the same `/stt` and `/tts` routes and the same JSON, with extra fields only when set (all empty today), so nothing the splitter does changes.

Things to know:
- `speech.link.stt_timeout_s` 30 and `tts_timeout_s` 45 are the old hard-coded values. Worst case the splitter spends 8 s on aiserver and up to 60 s on the mini, longer than 45 s; this is how v1 behaves too, so no regression.
- The plugin records the `X-Mira-Voice` header and pins chunk 2 and 3 of a reply to the voice chunk 1 got. aiserver runs the v1 server, which sends no such header, so pinning does nothing (same as v1). Corner case: chunk 1 falls back to the mini after the plugin mini is deployed (M6), the mini answers with a header, chunk 2 sends that Piper voice id to the splitter; the 60 s skip window normally keeps chunk 2 on the mini too, so this is harmless.
- `control.http.speech_url` is a separate knob (the mini's control API, default `http://100.77.98.89:8765`), so the loopback speech address does not break the Pi-to-mini control proxy. Leave it as the default.
- Consequence for tuning (more in section 7): requests via the splitter reach aiserver's v1 server, which reads only `text` and `voice`. Per-request `speed` and `peak`, and any voice id from the mini's catalog, have no effect on the live voice.
- Hardening, optional: the splitter unit is not ordered before the listener. Add a separate drop-in (not the one `mira unit apply` renders) with `Wants=mira-speech-split.service` and `After=mira-speech-split.service` so a boot never starts the listener with nothing on 8766. The splitter itself restarts, and the listener's offline chime covers a miss, so this is not a blocker.
- Later feature, not tonight: a `tts.remote` url, token file and fallback URL would retire the splitter. The aiserver report already asks for it.

Risk if skipped: none, provided nobody edits `speech.link.url`.

## 4. The audio helper: 12 percent speaking level and the night settings

What is live: `/usr/local/bin/mira-audio` (root 755, md5 8bfbdbbf...) passes `--volume=<percent/100>` to `pw-play` for `say`, from `/etc/mira/say-volume` (12). `/usr/local/bin/mira-say-volume` is the setter (3 to 40 percent), and Hermes's wall MCP `voice_volume` calls it. The patched file is in `agentic-os:pi/deploy/mira-audio/mira-audio`; the music repo's copy and its `install-speaker.sh` are not patched, so re-running that installer silently returns her to full volume.

What the release does: the plugin engine runs the same binary (`playback.miraaudio.bin` is `mira-audio`; no env var in the live file), so the level survives the switch untouched. The plugin knows nothing about `say-volume`. Night mode is applied before the file reaches the helper: inside `night_window` the plugin scales every 16 bit sample by `night_gain` (default 0.5) in a temp copy, and the helper then applies the 12 percent. The two multiply: 0.12 x 0.5 = 0.06, about 6 percent of full level, roughly 24 dB below full, outside the window it is 12 percent. If the owner or Hermes sets the base level to 6 percent the night level is 3 percent, and base 3 percent gives 1.5 percent at night, below the setter's own 3 percent floor. The reply "level" that drives the wall's ring is computed from the unscaled WAV in both v1 and the plugin, so the ring does not change with either setting.

Adopt gives `night_window` "" (empty, off) and `night_gain` 0.5, because nothing in the legacy world has them. The 23:00 to 06:00 window is not produced by adopt; it is a runbook `mira set` (deltas above, the repo's `deploy/mira.example.json` already carries it). The listener runs in America/New_York on the Pi, so the window is local time there.

What to change:
- Runbook: set `night_window=23:00-06:00` as above.
- Owner decision: whether 6 percent at night is right. It is very quiet (a normal-voice sample played at 12 percent is already low), and an owner test after 23:00 will sound as if the wall broke. Options: keep 0.5; raise to `night_gain=0.7` (about 8 percent); or tell the owner at cutover that night is half of today's level. Note that `voice_volume` and the setter only know the daytime base and say nothing about night, and `night_gain` is owner-only by policy while Hermes can still move the base freely (3 to 40 percent, no trial, no rate limit, outside the mira policy).
- Later: make loudness a playback knob (the say-volume report already says so) so there is one path, one policy, one trial. Until then keep `voice_volume` as the only loudness control for her (section 7).
- Repo: patch the music repo's `deploy/speaker/mira-audio` and `install-speaker.sh` to match, or at least keep the check in runbook delta 6.

Risk if skipped: B1 aside, none at cutover; an installer re-run undoes the volume, and nights are quieter than the owner may expect.

## 5. Hermes: the wall MCP (screens, voice_volume) and the SOUL.md house map

What is live: Hermes config has an MCP server `wall` (`/opt/wall-mcp/server.py`, venv `/opt/wall-mcp/venv`) with list_screens, show_screen, clear_screen, voice_volume and the others; Agent OS caps Hermes at 3 screens (wall-cap), print-studio is an allowed template, cad and keepsake are refused. `SOUL.md` (both copies, `~agent/.hermes/SOUL.md` and `profiles/claude/SOUL.md`, 9242 bytes) has the house map digest, the wall paragraph and the voice rules.

What the release does: nothing here depends on the listener engine. The plan's M9 adds a second MCP named `mira` (and a temporary `mira-eval`); the names do not collide with `wall`, and none of its tool names overlap `voice_volume`. The M9 steps say "back up both Hermes configs": those now include the `wall` block, so the backups are the right restore point. `MEMORY.md` is 1095 of 2200 characters, so the one-line pointer still fits.

What to change: SOUL.md's last paragraph reads "Mira's parts are being rebuilt as plugins with settings she can tune by voice. That is not live yet; today she cannot change her own settings." That is already wrong: she can change her own speaking volume today through `voice_volume`. Replace it with "Today you can change only her speaking volume, with the wall tool voice_volume. The rest of her settings are tuned with the mira tools once they exist." and update it again at M9. Both copies, backups first (the repo copy of the SOUL text in `agentic-os` if tracked). The knowledge README says "Do not touch SOUL.md"; that predates today's house-map edit and should be amended to "edit it only through a backed-up, matching change to both copies".

Risk if skipped: Hermes tells the owner she cannot change her settings, then does.

## 6. The wall at 2560x1440

What is live: the wall kiosk renders at 2560x1440; pushed screens scale their 1920x1080 canvas to the viewport (commit 0c1250f). The listener and speech server only post state and level to the Agent OS hook (`reporter.agentos`) and have no resolution dependency; the spec, the knobs and the knowledge drafts contain no 1920 or 1080 numbers. Nothing to change in the plugin release. Only note for the later visual checks: reference screenshots in older briefs are 1920x1080 and are not comparable pixel for pixel.

## 7. The Hermes knowledge texts written during the build (`scratchpad/mira-plugins/knowledge/`)

Install rule stays: nothing from `vault/` before the cutover has passed an owner tuning test. Findings, by file:

- `MIRA_WALL_IDENTITY.md` and `.diff.md`: see section 1. Not installable as written. The diff file's claim "Everything the house draft added is kept" is about the draft, not the live file.
- `hermes-skill/mira/mira-self/SKILL.md`, `references/knobs.md`, `glossary.md`, `dialogues.md` map "too quiet", "too loud" and "speak up" to `tts.engine.peak` (0.6 to 0.95, on the mini). That conflicts with what exists: loudness of her voice is already the wall tool `voice_volume` (Pi, 3 to 40 percent), the owner's phrases for it are in that tool's instructions, and `tts.engine.peak` on the mini no longer changes the live voice (next bullet). She would have two tools for one phrase and the wrong one would do nothing audible. Fix: map loudness phrases to `voice_volume` in the skill, the glossary and the dialogues; remove `tts.engine.peak` from the ten agent knobs until loudness is a playback knob.
- Same files, `tts.engine.speed`, `tts.engine.voice` and `mira_set_voice`, `mira_list_voices`, `mira_preview(speed, peak)`: they act on the mini's speech server. The live voice is Kokoro `am_eric` on aiserver through the splitter, and the mini's Piper voice is only the fallback. aiserver runs the v1 server (`tts_only.py` on `server_orig.py`), which ignores `speed` and `peak` and falls back to its default voice for a name it does not know. So changing speed, peak or voice by voice would be inaudible today, and a preview would play the unchanged Kokoro voice while claiming a trial. Owner decision before M9: (a) run the plugin speech server on aiserver and point the splitter at it, with its own control token, or (b) drop speed, peak and voice from the agent range for now (keep the Pi hearing and timing knobs, and `voice_volume`). The vault notes carry the same assumption.
- `references/architecture.md` ("The three machines", step 5 of "One turn", the Plugins list) and `vault/new/Tools/Services/Mira Speech Server.md` ("TTS: Piper", "Kokoro voices are too slow for replies"): out of date. Replies are rendered by Kokoro `am_eric` on aiserver's GTX 1050 in about 1 s, with the mini's Piper as fallback, through the splitter on the Pi; she is played at 12 percent and quieter at night. Add the splitter and aiserver to the architecture and name where each knob really lands. `Mira Wall Listener.md` row "voice, speed, loudness: on the mini" has the same error.
- `architecture.md` and `Tune Mira.md` say "She plays everything more quietly at night"; correct once the runbook `night_window` is set, and should add that the base level is 12 percent so night is about 6 percent.
- Duplicated: the persona draft's tuning paragraph and the skill's "Load the mira-self skill first" duplicate nothing today (no skill installed), but `voice_volume` is already in the live persona line "You can make your own voice louder or quieter when he asks". The later persona must keep that single line and not add a second loudness instruction.
- `references/dialogues.md`, `SKILL.md` section 6 and the glossary mention previews and `quiet_hours`: consistent with the owner's rule (no refusals at night) as long as `control.http.preview_quiet` stays "". Previews also go through the splitter, so they play the aiserver voice.
- `vault/updates/Projects/mira.md` and the new notes describe the engine as the current state: fine after cutover, but add today's facts that no draft knows: the splitter and aiserver voice, `/etc/mira/say-volume` and `mira-say-volume`, the `voice_volume` tool, the persona edits, the wall at 2560x1440, the `AGENT_MAX_SCREENS=3` rule.
- `wall-AGENTS.md`: its escape-hatch section is right; add a line that the persona lives at `/home/mira/mira-listen/MIRA_WALL_IDENTITY.md` and is shared across releases (B1), and that the splitter must be running.
- `test_knowledge.py` and the scaffold example are unaffected.

## 8. Other findings

- The mini's `speech.json` adopt (M6) is unaffected: its plist and `server.py` are unchanged today. After M6 the mini only matters as the fallback voice and as the STT host, which is what the splitter already uses it for.
- `deploy/README.md` (listener setup) has no splitter, no say-volume and no persona-sharing note; add them so a rebuild from the repo reproduces the wall.
- The sandboxed temp run used `HOME` and the release folder in a throwaway dir; nothing on the Pi was written. Not tested: the plugin engine itself running against the live files (that is M7).
