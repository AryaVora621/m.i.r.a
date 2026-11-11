# Mira plugins cutover runbook (M6 to M9), written 2026-11-10 ~21:40

For the orchestrator, run with the owner present. Every command below was checked against the real CLI help and code at commit 71dcc7c (release id 71dcc7c34598). The CLI steps (adopt, set, unit, rollback, tokens, doctor) were rehearsed on the Mac in throwaway homes with the Pi's real `mira-listen.service` and `listen.env` and the mini's real plist; nothing was run against the live machines.

Conventions (paste once into the orchestrator shell):
```
REL=71dcc7c34598
PI()   { ssh -o ConnectTimeout=8 -o HostName=192.168.1.179 mira "$@"; }     # logs in as user mira (uid 1000), sudo -n works
MINI() { ssh -o BatchMode=yes aryavora@192.168.1.174 "$@"; }               # logs in as aryavora (uid 501)
WALL="/Users/aryavora/Desktop/Personal Projects/mira/wall"
SCR=/private/tmp/claude-501/-Users-aryavora-Desktop-Personal-Projects-agentic-os/a699da48-f6d2-4aec-8907-3db40e3fb372/scratchpad/mira-plugins
XDG='XDG_RUNTIME_DIR=/run/user/1000'
```
Never print a token. Every token command below moves values through pipes or files.

---------------------------------------------------------------------------------------------------

## 0. Before you start

### 0.1 What must be true (check each, stop on a mismatch)

a. The live listener is the original: `PI 'md5sum /home/mira/mira-listen/mira_listen.py'` gives `8990842340a5ce68aa11515d0432f4ed`.
b. Env, persona and unit unchanged: `PI 'md5sum /home/mira/.config/mira/listen.env /home/mira/mira-listen/MIRA_WALL_IDENTITY.md /home/mira/.config/systemd/user/mira-listen.service'` gives `77909fedcf8c1939fb2354d48ac79fb3`, `cf5d457ff61e387169f8fbb1b5bc675f`, `9773a69a58b43c51c1d5f06f48e51e43`.
c. Listener and splitter up, no restarts: `PI "$XDG systemctl --user show mira-listen mira-speech-split -p MainPID -p NRestarts -p ActiveState"`; both active, NRestarts=0 (at 21:18 the listener was PID 64358 and the splitter PID 31452).
d. The patched speaker helper: `PI 'md5sum /usr/local/bin/mira-audio; cat /etc/mira/say-volume'` gives `8bfbdbbfe656237b42e239cdc1fc36ea` and `12`.
e. The mini speech server is the original: `MINI 'md5 "$HOME/Library/Application Support/mira-speech/server.py"'` gives `ffe5ae0ee3881249f1102b27dd1755c4`.
f. The release is staged and intact: `PI "cd /home/mira/mira-listen/releases/$REL && find . -type f | wc -l && cat RELEASE.json"` gives `43` and `{"built": 1794360665, "machine": "pi", "release": "71dcc7c34598"}`.
g. Ports free: `PI 'ss -ltn | grep -E ":(8650|8651) " || echo free'` prints `free`.
h. Pi health: `PI 'cat /sys/class/thermal/thermal_zone0/temp; free -m | sed -n 2p; uptime'`; temp under 70000, available memory over 800 MB.
i. Remove the stage tests' leftovers in shared /tmp (mira-owned scratch): `PI 'rm -rf /tmp/pytest-of-mira'`.

### 0.2 Two tooling gaps to close first (found while verifying; reasons in section 6)

**Gap A: `mira unit render` and `mira unit apply` fail on the Pi.** The release holds code only and `unit` reads `deploy/mira-listen.dropin.tmpl` from beside `mirakit/`; the manifest does not ship it (reproduced: `FileNotFoundError .../releases/<sha>/deploy/mira-listen.dropin.tmpl`). Pick one:
- Preferred: add `{"src": "deploy/mira-listen.dropin.tmpl", "dst": "deploy/mira-listen.dropin.tmpl"}` to `pi.files` in `wall/deploy/manifest.json`, commit, set `REL=<new sha>`, then run section 1 again (dry run and `--stage`). Everything below then uses the new sha. The old release folder can stay or go (`PI 'rm -r /home/mira/mira-listen/releases/71dcc7c34598'`).
- Stop-gap that keeps this release (nothing running references the release folder; the template is the repo file at the same commit):
  ```
  PI "mkdir -p /home/mira/mira-listen/releases/$REL/deploy"
  git -C "$WALL/.." show 71dcc7c:wall/deploy/mira-listen.dropin.tmpl | PI "cat > /home/mira/mira-listen/releases/$REL/deploy/mira-listen.dropin.tmpl"
  ```
  Verify the md5 on the Pi equals `git -C "$WALL/.." show 71dcc7c:wall/deploy/mira-listen.dropin.tmpl | md5`.

**Gap B: the `mira` command is not installed on either machine.** `wall/bin/mira` is a sh wrapper meant for `~/.local/bin/mira`, but the manifest does not ship it and no step installs it (the Pi's `~/.local/bin` holds only `claude`; the mini has no `mira`). Install it (a new file that nothing live reads):
```
cat "$WALL/bin/mira" | PI 'cat > /home/mira/.local/bin/mira && chmod 755 /home/mira/.local/bin/mira'
cat "$WALL/bin/mira" | MINI 'mkdir -p "$HOME/.local/bin" && cat > "$HOME/.local/bin/mira" && chmod 755 "$HOME/.local/bin/mira"'
PI '/home/mira/.local/bin/mira --help | head -3'        # expect: usage: mira [-h] [--offline] ...
MINI '"$HOME/.local/bin/mira" --help | head -3'
```
The wrapper runs the CLI of `current` (or the newest release while `current` does not exist) with `MIRA_MACHINE=listener` on the Pi and `speech` on the mini.

### 0.3 Land the code
HEAD of `/Users/aryavora/Desktop/Personal Projects/mira` is 71dcc7c on branch `feat/mira-voice-loop`; `main` is at 7a0bb0e and does not contain it (HEAD is 4 commits ahead). `deploy` ships HEAD of whatever branch is checked out. Per the git rules, fast-forward main (`git merge --ff-only feat/mira-voice-loop` from main, then push) when the owner says go, or say plainly that the staged release came from the feature branch. A dirty `wall/` is refused by `deploy`.

### 0.4 Back up (copies only, all reversible)
```
D=20261110
PI "cd /home/mira && cp -p .config/mira/listen.env .config/mira/listen.env.bak-cutover-$D \
  && cp -p mira-listen/MIRA_WALL_IDENTITY.md mira-listen/MIRA_WALL_IDENTITY.md.bak-persona-$D \
  && cp -p .config/systemd/user/mira-listen.service .config/mira/mira-listen.service.bak-cutover-$D \
  && ls -la .config/mira | grep bak-cutover"
MINI 'cd "$HOME/Library/Application Support/mira-speech" && cp -p "$HOME/Library/LaunchAgents/org.aryavora.mira-speech.plist" org.aryavora.mira-speech.plist.bak-20261110 && ls -la *.bak-*'
```
`deploy speech` makes its own snapshot and `server.py.bak-plugins-2026-11-10`; `adopt --write` makes `listen.env.bak-adopt-2026-11-10`.

### 0.5 Tell the owner
- About 40 minutes with three short gaps: the mini speech restart (about 30 s, she cannot hear), the Pi listener restart (about 20 s), and a second Pi restart at M8 (about 20 s). Say "Ready?" and wait for a yes before each.
- **Night window:** from 23:00 she plays at half level again (`night_gain` 0.5 on top of the 12 percent helper level, about 6 percent of full). Spoken checks after 23:00 will sound broken. Do them before 23:00, or decide now whether to set `night_gain=0.7` (about 8 percent).
- No recordings are needed and nothing plays unless a step says so.

---------------------------------------------------------------------------------------------------

## 1. Confirm the stage (no interruption, about 2 minutes)

From the Mac in `$WALL` (deploy is stdlib only; the manifest's ssh aliases do not resolve, so override them):
```
cd "$WALL"
export MIRA_DEPLOY_PI_SSH=mira@192.168.1.179 MIRA_DEPLOY_SPEECH_SSH=aryavora@192.168.1.174
PYTHONDONTWRITEBYTECODE=1 python3 -B -m mirakit.cli deploy pi --dry-run
```
Expect 43 `A` lines (the diff is against `current`, which does not exist yet), then `planned commands:` listing the stage, the `state/planned` write, `ln -sfn`, `.previous` and a `systemctl --user restart mira-listen`; nothing is executed. If you changed the manifest (Gap A), run `... deploy pi --stage` now; expect `staged <sha> in /home/mira/mira-listen/releases/<sha>` and a passing `mira check --fast`.
**Warning:** never run `deploy pi --activate` (or `deploy pi` without `--stage`) tonight; `--activate` restarts the unit with no owner window.

---------------------------------------------------------------------------------------------------

## 2. M6: the mini speech server (INTERRUPTS: she cannot hear for about 30 s. Speech output continues because replies come from aiserver through the splitter, with the mini only as fallback)

The mini holds all of her hearing (`/stt`), so this is a real gap. It goes first so any trouble is on the fallback path while the Pi still runs the original listener.

### 2.1 Ship the files (no restart, about 5 s)
```
cd "$WALL"
PYTHONDONTWRITEBYTECODE=1 python3 -B -m mirakit.cli deploy speech --dry-run
```
Expect `M server.py`, `A server_v1.py`, `A mirakit/...` (19), `A speech_plugins/...` (17), then `planned commands:` (snapshot, `.bak`, tar extract), nothing executed. Then:
```
PYTHONDONTWRITEBYTECODE=1 python3 -B -m mirakit.cli deploy speech
```
Expect `copied 71dcc7c34598 to /Users/aryavora/Library/Application Support/mira-speech; snapshot .../snapshots/<stamp>.tar` and `nothing was restarted; when you are ready: launchctl kickstart -k gui/501/org.aryavora.mira-speech`.
Check (behaviour is unchanged: there is no `speech.json` yet, so the new `server.py` would still run the frozen v1 server):
```
MINI 'cd "$HOME/Library/Application Support/mira-speech" && ls snapshots server_v1.py && md5 server.py server.py.bak-plugins-2026-11-10 && curl -s -m5 http://127.0.0.1:8765/health'
```
Expect the `.bak` md5 `ffe5ae0ee3881249f1102b27dd1755c4` and `/health` still `{"ok": true, "stt": "faster-whisper:tiny.en", "tts": "vits-piper-en_US-amy-medium", "voice": "amy", ...}` (same PID 22675).

### 2.2 Adopt the plist and create tokens (no restart)
```
MINI '"$HOME/.local/bin/mira" adopt'
```
Expect exactly three rows, source `plist`: `server.http.bind ["127.0.0.1","100.77.98.89"]`, `stt.engine.kind faster-whisper:tiny.en`, `stt.engine.threads 4`, then `dry run: 3 values shown; pass --write ...`. If anything else shows, stop.
```
MINI '"$HOME/.local/bin/mira" adopt --write'     # wrote /Users/aryavora/.config/mira/speech.json (core.engine stays legacy ...)
MINI '"$HOME/.local/bin/mira" tokens install --machine speech'     # owner and pi tokens created, 0600, values never printed
```
Copy the Pi token to the Pi without printing it (used by the Pi to mini proxy at M8; harmless now):
```
MINI 'cat "$HOME/.config/mira/control.pi.token"' | PI 'umask 077; cat > /home/mira/.config/mira/speech.control.token && chmod 400 /home/mira/.config/mira/speech.control.token && ls -l /home/mira/.config/mira/speech.control.token'
```
Expect `-r-------- 1 mira mira 44 ...`.

### 2.3 Switch and restart (OWNER WINDOW: announce, wait for "Ready")
```
MINI '"$HOME/.local/bin/mira" set core.engine=plugins --offline --reason cutover'
```
Expect `core.engine legacy -> plugins pending_restart (S-000001)` and exit code 7 (pending restart, not an error).
```
MINI '"$HOME/.local/bin/mira" restart'
```
Writes the planned marker, then runs `launchctl kickstart -k gui/501/org.aryavora.mira-speech`; expect `restarted org.aryavora.mira-speech`. If kickstart fails over ssh (service not found or a domain permission error), run the same line from a Terminal on the mini using computer use.

### 2.4 Verify (60 s, no sound)
```
sleep 25; MINI 'tail -n 8 "$HOME/Library/Logs/mira-speech/server.log"; tail -5 "$HOME/Library/Logs/mira-speech/server.err.log"; curl -s -m5 http://127.0.0.1:8765/health'
```
Expect in the log `engine plugins: core.engine is plugins` then `plugin engine running`; an empty or warnings-only err log; `/health` equal to before (`ok`, `stt`, `tts`, `voice` amy, small `uptime_s`) plus the additive fields `engine`, `revision`, `voice_id`. If `/health` is not up after 60 s, roll back at once.
Round trip from the Pi over the real path, token on stdin (never in argv):
```
cat "$WALL/tests/fixtures/mira_pause_the_music.wav" | PI 'printf "header = \"Authorization: Bearer %s\"\n" "$(cat /home/mira/.config/mira/speech.token)" | curl -s -m20 -K - -H "Content-Type: audio/wav" --data-binary @- http://100.77.98.89:8765/stt'
```
Expect `{"text": "pause the music"...` (case and punctuation may differ) and `ms` under 3000.
```
PI 'printf "header = \"Authorization: Bearer %s\"\n" "$(cat /home/mira/.config/mira/speech.token)" | curl -s -m20 -K - -H "Content-Type: application/json" -d "{\"text\":\"Testing the speech server.\"}" -D - -o /dev/null http://100.77.98.89:8765/tts | head -8'
```
Expect a `200` status line and `Content-Type: audio/wav` (and the new `X-Mira-Voice` header); the body goes to /dev/null, nothing plays. `MINI '"$HOME/.local/bin/mira" voices | head'` lists the catalog (if it asks for a token, use `voices --offline`). Skip the spec's `jenny --trial 120` voice switch tonight: it changes the fallback voice and nothing audible depends on it.
Finally the owner says one wake phrase (the Pi is still the original listener): hearing must work. Watch `PI "$XDG journalctl --user -u mira-listen --since -2min --no-pager -o cat | tail -5"` for the normal wake lines and no `offline`.

### 2.5 Rollback of M6 (one command, about 30 s deaf again)
```
MINI '"$HOME/.local/bin/mira" rollback --offline'
```
Expect `core.engine plugins -> legacy ...` and `restarted org.aryavora.mira-speech on the legacy engine`; then the `/health` check. Harder fallback if the CLI is unusable: `MINI 'cd "$HOME/Library/Application Support/mira-speech" && cp -p server.py.bak-plugins-2026-11-10 server.py && launchctl kickstart -k gui/501/org.aryavora.mira-speech'`. There is an automatic fallback to the legacy server after 4 unplanned starts in 10 minutes, but launchd's 15 s throttle makes that over a minute; do not wait for it.
Owner sees: 30 s where wake words get the offline chime.

---------------------------------------------------------------------------------------------------

## 3. M7: the Pi listener (INTERRUPTS: about 20 s deaf and silent at the restart; cold start is 15.6 to 16.5 s under the scope and 18 s live)

Everything up to 3.5 changes files only; the running listener keeps running the original `mira_listen.py`. Run 3.2 and 3.3 back to back: `adopt --write` regenerates `listen.env` and for a moment it holds release-folder paths for the persona and chime (a v1 restart inside that gap would lose them).

### 3.1 Pre-flight snapshot (the comparison for 3.7)
```
PI "$XDG journalctl --user -u mira-listen --no-pager -o cat | grep 'mira-listen up (' | tail -1"
PI "$XDG systemctl --user show mira-listen -p MainPID -p ActiveEnterTimestamp -p NRestarts"
```
Keep the startup line text; the plugin engine must print the same fields.

### 3.2 Persona and adopt (files only)
Install today's merged persona. The live file is the 2460 byte one; `cutover/MIRA_WALL_IDENTITY.merged.md` equals the repo's file at 71dcc7c (2709 bytes, sha256 `15cb030443630089afd3655920954d696331c4c99309c874656c36929b32540d`). First check the live file did not change since the backup: `PI 'md5sum /home/mira/mira-listen/MIRA_WALL_IDENTITY.md'` must still be `cf5d457ff61e387169f8fbb1b5bc675f`; if it differs, someone edited it, merge their change by hand.
```
cat "$SCR/cutover/MIRA_WALL_IDENTITY.merged.md" | PI 'cat > /home/mira/mira-listen/.persona.new && sha256sum /home/mira/mira-listen/.persona.new'
PI 'install -m 644 /home/mira/mira-listen/.persona.new /home/mira/mira-listen/MIRA_WALL_IDENTITY.md && rm /home/mira/mira-listen/.persona.new && wc -c /home/mira/mira-listen/MIRA_WALL_IDENTITY.md'
```
Expect the sha256 above, then `2709`. The old listener reads the persona only at start, so nothing changes until the restart; the added "quieter at night" sentence becomes true when `night_window` is set in 3.3.
```
PI '/home/mira/.local/bin/mira adopt --all | head -50'     # review once against listen.env and the unit (cutover/expected-mira.json.md, section A)
PI '/home/mira/.local/bin/mira adopt --write'
```
Expect `wrote /home/mira/.config/mira/mira.json (core.engine stays legacy, core.legacy_env is ignore)` and `wrote the legacy mirror next to it (listen.env, legacy.args)`. Key values: `speech.link.url http://127.0.0.1:8766`, `wake.kws.threshold 0.05`, `agc.mic.*` 0.05 / 10 / 3 / 1.8, `intent.music.url http://100.77.98.89:4534`.

### 3.3 The cutover values (drift B1; immediately after 3.2)
```
PI '/home/mira/.local/bin/mira set persona.file.path=/home/mira/mira-listen/MIRA_WALL_IDENTITY.md playback.miraaudio.chime=/home/mira/mira-listen/assets/offline.wav playback.miraaudio.night_window=23:00-06:00 persona.file.reread=turn --offline --reason cutover'
```
Expect four lines ending `pending_restart (L-000001)` and exit code 7 (ssh reports 7; normal). Add `playback.miraaudio.night_gain=0.7` to the same command only if the owner chose it.
```
PI 'grep -E "MIRA_IDENTITY_FILE|MIRA_CHIME|MIRA_SPEECH_URL" /home/mira/.config/mira/listen.env; tail -4 /home/mira/.config/mira/legacy.args; grep -c releases /home/mira/.config/mira/listen.env /home/mira/.config/mira/legacy.args'
```
Expect `MIRA_IDENTITY_FILE=/home/mira/mira-listen/MIRA_WALL_IDENTITY.md`, `MIRA_CHIME=/home/mira/mira-listen/assets/offline.wav`, `MIRA_SPEECH_URL=http://127.0.0.1:8766`, a legacy.args tail of `--identity <path> --chime <path>`, and `0` for both `releases` counts.

### 3.4 Clean legacy.args (needed; section 6, item 4; repeat after every later `mira set`)
After any `mira set`, the legacy mirror carries flags the v1 listener does not define (six in the rehearsal: `--followup-wait-s --followup-seed-chunks --wake-seed-chunks --preroll-chunks --offline-text --level-scale`). The launcher's crash fallback runs v1 with that file and v1 exits 2, so the safety net would fail. Only `mira rollback` strips them. Strip them with the release's own v1 parser (same logic as `mirakit.cli.sanitize_legacy_args`):
```
PI "cd /home/mira/mira-listen/releases/$REL && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/mira/mira-listen/venv/bin/python -B - <<'EOF'
import os, mira_listen_v1 as v
p = os.path.expanduser('~/.config/mira/legacy.args')
known = set(v.build_parser()._option_string_actions)
kept, skip, dropped = [], False, []
for t in open(p).read().splitlines():
    if t.startswith('--'):
        skip = t not in known
        if skip:
            dropped.append(t); continue
    elif skip:
        continue
    kept.append(t)
open(p, 'w').write('\n'.join(kept) + '\n'); print('dropped', dropped)
EOF
chmod 600 /home/mira/.config/mira/legacy.args"
```
Expect `dropped []` right after `adopt --write` and a list of the unknown flags after a `set`.

### 3.5 Unit and engine (files and daemon-reload; the running process is untouched; needs Gap A and Gap B)
```
PI '/home/mira/.local/bin/mira unit render --check'
```
Expect a diff adding `/home/mira/.config/systemd/user/mira-listen.service.d/mira.conf` with `ExecStart=`, `ExecStart=%h/mira-listen/venv/bin/python -B %h/mira-listen/current/mira_launch.py --config %h/.config/mira/mira.json`, `MemoryMax=200M`, `CPUQuota=40%`, `Nice=5`, `Restart=always`, `RestartSec=3` (exit code 1 is normal: nothing installed yet).
```
PI "/home/mira/.local/bin/mira unit apply --release $REL"
```
Expect `wrote /home/mira/.config/systemd/user/mira-listen.service.d/mira.conf`, `current -> releases/71dcc7c34598`, `systemctl --user daemon-reload`. Check `PI "$XDG systemctl --user cat mira-listen | tail -12"` shows the drop-in and `show -p MainPID` is still the old PID.
Set the engine last, so an unplanned crash before the restart cannot start the plugin engine by surprise:
```
PI '/home/mira/.local/bin/mira set core.engine=plugins --offline --reason cutover'      # core.engine legacy -> plugins pending_restart (L-000002), exit 7
```
Run 3.4 again (the set rewrote the mirror), then:
```
PI '/home/mira/.local/bin/mira config validate; /home/mira/.local/bin/mira doctor'
```
Expect a valid config and a doctor table with `config ok`, `unit ok`, `argv overrides ok`, `env ignored ok`, `port ok`, `pins ok`; `token modes warn` (tokens come in M8) and `last good warn` (written after the first clean boot) are expected.

### 3.6 The restart (OWNER WINDOW: announce, wait for "Ready", "deaf for about 20 seconds")
Use `--offline`: without it `mira restart` first tries the control API, whose owner token does not exist yet, and stops with `the owner token file ... is not readable` (reproduced; the planned marker is written but nothing restarts).
```
PI '/home/mira/.local/bin/mira restart --offline'
```
Expect `restarted mira-listen`. It writes `~/.config/mira/state/planned` (so the start counts as planned) and runs `systemctl --user restart mira-listen`.

### 3.7 Verify (within 60 s: journal first, then the owner)
```
sleep 22
PI "$XDG systemctl --user is-active mira-listen; $XDG systemctl --user show mira-listen -p MainPID -p NRestarts -p MemoryCurrent"
PI "ps -o args= -p \$($XDG systemctl --user show mira-listen -p MainPID --value)"
PI "$XDG journalctl --user -u mira-listen --since -90s --no-pager -o cat | cut -c1-200"
PI 'tail -3 /home/mira/.config/mira/state/starts; ls -la /home/mira/.config/mira/mira.json.lastgood'
```
Expect `active`; a new MainPID; `NRestarts=0`; process args containing `.../current/mira_launch.py --config /home/mira/.config/mira/mira.json` and NOT `mira_listen_v1.py` (that means the launcher fell back to legacy: read the `mira-launch:` journal lines for why); a `mira-listen up (` line matching 3.1; no `Traceback`; `starts` ending `<epoch> planned`; `mira.json.lastgood` appearing after the first clean boot (up to a minute); `MemoryCurrent` under 150 MB.
`mira status` and `mira inventory` are online commands and fail until M8; `mira inventory --offline` only shows the file's state (every plugin `PENDING`) and proves nothing about the running process.
Owner script (before 23:00), one at a time:
1. "Mira, what's on my calendar today?" A short spoken answer (25 words at most; a long markdown-style answer means the persona did not load); the wall shows `idle, listening, thinking, speaking, idle`.
2. Within 15 s, without the wake word: "And tomorrow?" A follow-up answer.
3. "Mira, pause the music." The local music action and a short confirmation, no Hermes round trip.
4. Same loudness as before (12 percent helper level).
Then the 24 h watch (rung 7), `sv08watch` style: tail the journal, every 30 minutes check `NRestarts`, `MemoryCurrent` and `CPUUsageNSec` against `reports/preflight.json` (baseline is only about 2 days: allow a wide tolerance).

### 3.8 Rollback of M7 (one command, about 20 s deaf again)
```
PI '/home/mira/.local/bin/mira rollback --offline'
```
Use `--offline` until the tokens exist: plain `mira rollback` tries the control API first and stops with `the owner token file ... is not readable` (reproduced), the worst moment for it. Expect `core.engine plugins -> legacy ...`, possibly a list of values legacy cannot represent (`playback.miraaudio.night_window = 23:00-06:00`), a line about dropped `legacy.args` flags, and `restarted mira-listen on the legacy engine`. The launcher then runs `mira_listen_v1.py` from the release with the tuned values; the persona and chime stay the shared absolute paths.
Fallbacks in order: `deploy pi --rollback` from the Mac needs `releases/.previous`, which exists only after a `--activate`, so tonight it answers `no previous release recorded`; skip it. Last resort, runs the untouched original file:
```
PI "rm /home/mira/.config/systemd/user/mira-listen.service.d/mira.conf && $XDG systemctl --user daemon-reload && $XDG systemctl --user restart mira-listen"
```
and, if the owner wants the old persona back: `PI 'cp -p /home/mira/mira-listen/MIRA_WALL_IDENTITY.md.bak-persona-20261110 /home/mira/mira-listen/MIRA_WALL_IDENTITY.md'`.
Automatic fallback: 3 unplanned starts in 10 minutes turn the boot guard on, 4 run the legacy listener (works only if 3.4 was done).

---------------------------------------------------------------------------------------------------

## 4. M8: the control plane (INTERRUPTS: one more restart of about 20 s; to avoid it, fold 4.1 and 4.2 into 3.5 at the cost of changing two things at once)

The spec calls `mira enable control.http` live, but it cannot bootstrap itself: it is an online command and needs the control API it would start. The CLI route is a file edit plus a restart (the code has no config file watcher and no SIGHUP).

### 4.1 Tokens (no interruption)
```
PI '/home/mira/.local/bin/mira tokens install --machine listener'
```
Expect a table: `owner /home/mira/.config/mira/control.owner.token created 0600`; `agent /etc/mira/control.agent.token created 0640 root:mira, acl u:agent:r` (it runs `sudo -n install -d -o root -g mira -m 755 /etc/mira`, which changes the group of the existing `/etc/mira` directory from root to mira; `say-volume` and `speaker.env` inside are untouched; `setfacl` is installed on the Pi); `speech /home/mira/.config/mira/speech.control.token exists 0400` (copied in 2.2; `missing_copy_from_mini` means repeat that copy). Exit code 0; a `failed` row means a sudo or ACL problem, stop.

### 4.2 Enable and restart (OWNER WINDOW)
```
PI '/home/mira/.local/bin/mira enable control.http --offline'       # control.http.enabled false -> true applied
```
Run 3.4, then announce and:
```
PI '/home/mira/.local/bin/mira restart --offline'
```
Repeat the 3.7 journal checks (new PID, `active`, planned start, no `mira_listen_v1.py` in the args).

### 4.3 Verify
```
PI '/home/mira/.local/bin/mira status'
PI '/home/mira/.local/bin/mira inventory'
PI '/home/mira/.local/bin/mira doctor'
PI 'curl -s -m5 http://127.0.0.1:8650/v1/health'
```
Expect: `status` shows machine listener, engine plugins, a revision, loop state idle, no failed plugins; `inventory` lists 17 plugins all active except `source.wav`; `doctor` all ok (token modes now ok); `/v1/health` returns JSON (a 401 without a token also proves it is up). Persona check: read the `persona.file` entry in `PI '/home/mira/.local/bin/mira --json inventory'` for `from_file: true`; if the field is not there, use `mira describe persona.file` and the spoken length probe ("bring up my 3D printers" 3 words, weather about 22, calendar about 17).
Canary (owner present, no sound):
```
PI '/home/mira/.local/bin/mira set agc.mic.target=0.06 --trial 120'
sleep 130; PI '/home/mira/.local/bin/mira get agc.mic.target --effective'     # back to 0.05 on its own
PI '/home/mira/.local/bin/mira set agc.mic.target=0.06'; PI '/home/mira/.local/bin/mira changes | head -3'
PI '/home/mira/.local/bin/mira undo'
PI '/home/mira/.local/bin/mira get agc.mic.target --effective'                # 0.05
```
Run 3.4 afterwards. Expect change log lines in `mira changes` and `MIRA_AGC_TARGET=0.05` in the legacy mirror at the end.

### 4.4 Rollback of M8
`PI '/home/mira/.local/bin/mira disable control.http'` (live, no restart). If the control plane blocks the CLI: `... disable control.http --offline` plus `restart --offline`.

---------------------------------------------------------------------------------------------------

## 5. M9: the Hermes tools (NO listener interruption; Hermes reconciles MCP config changes without a gateway restart)

**Do not start tonight without two owner decisions and the text fixes** (drift.md sections 5 and 7):
1. Voice path: her live voice is Kokoro `am_eric` on aiserver through the splitter, whose v1 server ignores `speed`, `peak` and the mini's voice ids. Choose (a) run the plugin speech server on aiserver behind the splitter, or (b) drop `tts.engine.speed`, `tts.engine.peak` and `tts.engine.voice` from the agent range for now and keep `voice_volume` as the only loudness control.
2. The Honesty bullet in the persona (she may send mail or start a print by voice after a spoken confirmation) conflicts with SOUL.md (nothing like that from a voice turn).
3. Fix the knowledge texts: map "too quiet", "too loud", "speak up" to the wall tool `voice_volume`, remove `tts.engine.peak` from the ten agent knobs, update the architecture, `Mira Speech Server.md` and `Tune Mira.md` (splitter, aiserver, 12 percent, night half level), rewrite the SOUL.md last paragraph in both copies. The skill folder exists only in the scratchpad (`$SCR/knowledge/hermes-skill/mira`); its planned repo home `agentic-os:pi/hermes-skills/mira-self/` does not exist yet.

M8 must be done first, or every tool answers `control_unreachable`. Commands (from `agentic-os/pi/mira-mcp`; root through `sudo -n` on the Pi):
```
cd "/Users/aryavora/Desktop/Personal Projects/agentic-os/pi/mira-mcp"
tar -cf - server.py requirements.txt smoke_test.py mock_control.py dialogue_eval.py | PI 'mkdir -p /home/mira/.mcp-stage && tar -xf - -C /home/mira/.mcp-stage'
PI 'sudo -n install -d -o root -g root -m 755 /opt/mira-mcp && for f in server.py requirements.txt smoke_test.py mock_control.py dialogue_eval.py; do sudo -n install -o root -g root -m 644 /home/mira/.mcp-stage/$f /opt/mira-mcp/$f; done; rm -rf /home/mira/.mcp-stage'
PI 'sudo -n python3 -m venv /opt/mira-mcp/venv && sudo -n /opt/mira-mcp/venv/bin/pip install -q -r /opt/mira-mcp/requirements.txt'     # about 2 minutes, needs PyPI (mcp==1.30.0)
PI 'sudo -n -u agent /opt/mira-mcp/venv/bin/python /opt/mira-mcp/smoke_test.py | tail -3'                                  # expect: all passed
PI 'for c in /home/agent/.hermes/config.yaml /home/agent/.hermes/profiles/claude/config.yaml; do sudo -n cp -p $c $c.bak-mira-20261110; done'
```
Add a `mira-eval` entry to BOTH configs under the existing top-level `mcp_servers:` (do not add a second `mcp_servers:` key), keeping owner `agent:agent` and mode 0600:
```yaml
  mira-eval:
    command: /opt/mira-mcp/venv/bin/python
    args: [/opt/mira-mcp/server.py]
    env:
      MIRA_CONTROL_URL: http://127.0.0.1:8651
      MIRA_CONTROL_TOKEN_FILE: <throwaway token file printed by dialogue_eval.py --print-env>
    cwd: /opt/mira-mcp
```
Wait for `MCP servers reconciled ... added=['mira-eval']` in `/home/agent/.hermes/logs/gateway.log`, then run `sudo -n -u agent python3 /opt/mira-mcp/dialogue_eval.py` (mock on 8651 with `--voice-turn always`; it refuses 8650; nine phrases through `hermes -z`, PASS or FAIL each; rerun a FAIL once). Remove `mira-eval` from both configs, add the real `mira` entry (the block in `pi/mira-mcp/README.md` step 4: URL `http://127.0.0.1:8650`, token file `/etc/mira/control.agent.token`). Verify: `PI 'sudo -n -u agent /home/agent/.local/bin/hermes -z "list your mira tools"'` shows twelve `mcp__mira__*` tools and `mira_status` returns the live status.
Skill, memory line and persona, each backed up first:
```
PI 'sudo -n -u agent install -d -m 775 /home/agent/.hermes/skills/mira'     # then install the FIXED skill files with install -o agent -g agent -m 664
PI 'sudo -n -u agent /home/agent/.local/bin/hermes skills list | grep -i mira'     # mira-self listed
```
`MEMORY.md` line: `$SCR/knowledge/hermes-MEMORY-line.txt` (1095 of 2200 characters used now). Persona lines: `$SCR/cutover/persona-after-m9.md`; with `persona.file.reread=turn` no restart is needed; `cp -p` to `.bak-persona-<date>` first and stay under 3000 bytes (2709 now). The owner speaks the voice script last.
**Rollback M9:** remove the `mira` entry from both configs (reconciles without a restart) and restore the `.bak-mira-20261110` configs, the skill folder, `MEMORY.md` and the persona backup.

---------------------------------------------------------------------------------------------------

## 6. Tooling I believe will fail on the real machines (with reasons)

1. **`mira unit render|apply` on the Pi (blocker).** It reads `<release>/deploy/mira-listen.dropin.tmpl`, which the manifest does not ship: `FileNotFoundError` (reproduced with a release-only copy of the 42 files). Fix: Gap A.
2. **No `mira` command on either machine (blocker for every `mira ...` line).** `wall/bin/mira` is never shipped or installed. Fix: Gap B, or call `PYTHONPATH=<release> <venv>/bin/python -B -m mirakit.cli`.
3. **Online commands fail until M8.** Without `control.owner.token` the CLI stops before any network call: `mira status`, `inventory`, `restart` and `rollback` without `--offline`, `enable`, and `set` without `--offline` all print `the owner token file ... is not readable`. The spec's 7.1 steps 4 to 6 (`mira restart`, `mira status`, `mira inventory`, `mira rollback`) fail as written. The runbook uses `--offline` or the journal. Bare `mira rollback` before 4.1 is the case that matters.
4. **`legacy.args` goes stale after every `mira set` (breaks the automatic fallback).** Reproduced: right after `adopt --write` all 16 flags are known to v1; after one `mira set` it holds 22, six unknown to `mira_listen_v1.py`. `mira_launch.py` feeds that file to v1 on a crash-loop fallback and argparse exits 2, so the 4-starts safety net would loop. Only `mira rollback` cleans it. Code fix: call the sanitizer after every mirror write. Until then, step 3.4 after every set.
5. **`mira enable control.http` cannot be live-first.** Section 4: two restarts instead of zero. Later fix: a SIGHUP or file-watch reload.
6. **`launchctl kickstart` over ssh on the mini.** `gui/501` works for `print` (checked); `kickstart` normally works while the owner is logged in at the console. If it fails, run from a Terminal on the mini.
7. **A broken mini restart deafens her for more than 30 s.** The 15 s launchd throttle times 4 unplanned starts before the legacy fallback is over a minute of no STT; the rollback is by hand (2.5).
8. **Branch.** `deploy` ships HEAD of the checked-out branch: 71dcc7c is on `feat/mira-voice-loop`, not `main` (0.3).
9. **Speech suite flake (not a deploy blocker, fix soon).** `speech_plugins/server_http.py` calls `plugin.note_busy()` in the `finally` after the response is written, so `tests/speech/test_sp_server_http.py` (asserts `busy == 1` right after the reply) can see 0 on a slow machine. Re-run of that file on the Pi during verification: 26 passed (intermittent, as the stage found). The runtime effect is only that the busy-defer clock is stamped a few ms after the reply.
10. **`deploy speech --rollback` restores only what existed.** The first snapshot holds only the old `server.py`; the new `server_v1.py`, `mirakit/` and `speech_plugins/` stay on disk (harmless to the restored server). A second `deploy speech` would snapshot the facade, but rollback still ends by copying `server.py` from the `.bak` file, so the result is right.
11. **Rung 5 and rung 6 were not run** (no room recordings; shadow dropped by the owner), so the first real-room evidence is the live cutover and the 24 h watch.

---------------------------------------------------------------------------------------------------

## 7. What is different from the spec because of today's changes

- **Persona is shared, not per release (drift B1, B2).** The live persona and the offline chime stay in `/home/mira/mira-listen/`, outside the release; `persona.file.path` and `playback.miraaudio.chime` are set to those absolute paths (3.3) and `persona.file.reread=turn`. Today's two live edits are kept: the repo (since 71dcc7c) and `cutover/MIRA_WALL_IDENTITY.merged.md` are the live text byte for byte plus the night sentence. The knowledge package's persona must not be installed.
- **Speech goes through the splitter.** `speech.link.url` stays `http://127.0.0.1:8766`; the splitter and aiserver's Kokoro `am_eric` are the live voice, the mini is hearing plus fallback voice. The plugin cannot express split STT and TTS addresses; do not edit `speech.link.url`.
- **Loudness.** Speaking level is the helper's 12 percent (`/etc/mira/say-volume`, wall tool `voice_volume`). Night mode multiplies: `night_window=23:00-06:00` and `night_gain` 0.5 give about 6 percent at night. Previews are never refused at night (`control.http.preview_quiet` is "").
- **No shadow run (M5) and no 24 h gate before cutover** (owner decision, addendum 2). Rung 5 not run; the rung 7 baseline is about 2 days (3 spotter hits).
- **Order and flags differ from spec 7.1 and 7.2:** M6 before M7; `--offline` on `set`, `restart` and `rollback` until M8; wrapper and template installed by hand; `legacy.args` cleaned after every `set`; the control plane needs a second restart; no `unit apply` on the mini (nothing to change there); the `jenny` trial skipped.
- **Stage method.** The Pi test scratch needs `voice/models/kws` and `bin/mira` in the wall layout and cannot put pytest on the subprocess `PYTHONPATH` (the stage's two failures); a full Pi run takes about 32 minutes throttled.
- **Hermes (M9) waits** on the voice-path decision and the knowledge fixes; SOUL.md has today's house map and a stale last paragraph ("not live yet") to rewrite in both copies, backups first.
- **Wall at 2560x1440** has no effect on the plugin release; older 1920x1080 reference screenshots are not pixel comparable.
- **Release and code state:** 71dcc7c34598 comes from branch `feat/mira-voice-loop` (main lacks it until merged); the live `mira_listen.py` (md5 `8990842340a5...`) and the mini's `server.py` (md5 `ffe5ae0e...`) are the frozen originals and stay on disk as the last resort.
