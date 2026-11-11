# Mira plugins cutover runbook v2 (M6 and M7 tonight, M8 and M9 later), written 2026-11-10 about 22:45

For the orchestrator, run with the owner in the room. Replaces `wall/docs/build-2026-11-10/CUTOVER_RUNBOOK.md` for the repaired tooling (main at 73de1da, release 73de1dad0ad0).

How this was checked: every command was compared with `mira <cmd> --help` and the code on main. The Pi CLI sequence (adopt, set, unit render, config validate, doctor, rollback) and the mini CLI sequence (adopt, tokens, set, config validate, doctor, status, rollback) were rehearsed on the Mac in throwaway homes using copies of the Pi's `listen.env` and unit and the mini's real plist. `deploy speech --dry-run` and `deploy pi --dry-run` were run for real against the machines (they change nothing). Read-only facts were taken from both machines at 22:28 to 22:31. Nothing live was changed by the writing of this runbook. Lines marked NOT REHEARSED could only be checked against the code.

Release id rule: `Deployer.git_head()` runs `git rev-parse --short=12 HEAD` and names the release folder with it (a `-dirty` suffix for `--allow-dirty`). Main is 73de1dad0ad0, `wall/` is clean, and `releases/73de1dad0ad0` is already staged on the Pi: 45 files, byte-identical to `git archive HEAD` (md5 of all 44 code files compared at 22:30; the Pi's locale is en_GB, so the check sorts both sides locally with `LC_ALL=C`), with `bin/mira` executable and `deploy/mira-listen.dropin.tmpl` present. `~/.local/bin/mira` on the Pi already links to `releases/73de1dad0ad0/bin/mira` and prints its help.

Conventions (paste once into the orchestrator shell):
```
REL=73de1dad0ad0
PI()   { ssh -o ConnectTimeout=8 -o HostName=192.168.1.179 mira "$@"; }      # user mira (uid 1000), sudo -n works
MINI() { ssh -o BatchMode=yes -o ConnectTimeout=8 aryavora@192.168.1.174 "$@"; }   # user aryavora (uid 501)
REPO="/Users/aryavora/Desktop/Personal Projects/mira"; WALL="$REPO/wall"
SCR=/private/tmp/claude-501/-Users-aryavora-Desktop-Personal-Projects-agentic-os/a699da48-f6d2-4aec-8907-3db40e3fb372/scratchpad/mira-plugins
XDG='XDG_RUNTIME_DIR=/run/user/1000'
PM=/home/mira/.local/bin/mira                    # the mira command on the Pi
MM='"$HOME/.local/bin/mira"'                     # the mira command on the mini (used inside MINI '...')
D=20261110                                       # fixed suffix for backups, even after midnight
export MIRA_DEPLOY_PI_SSH=mira@192.168.1.179 MIRA_DEPLOY_SPEECH_SSH=aryavora@192.168.1.174 PYTHONDONTWRITEBYTECODE=1
```
Never print a token. Every token step moves values through pipes or files. `--offline` is a global option and also works after the subcommand (`mira set ... --offline`).

Exit codes you will see: 0 ok, 7 queued for restart (`pending_restart`, not an error), 8 nothing answers (online command with no server), 1 `unit render --check` found a difference.

---------------------------------------------------------------------------------------------------

## 0. Before you start

### 0.1 What must be true (run all, stop on any mismatch)

a. Code and release agree. The release id must be the current main commit and the tree clean:
```
git -C "$REPO" rev-parse --short=12 HEAD          # must print 73de1dad0ad0 (if not, see 0.2)
git -C "$REPO" status --porcelain -- wall          # must print nothing
PI "cd /home/mira/mira-listen/releases/$REL && find . -type f | wc -l && cat RELEASE.json && ls -l bin/mira deploy/mira-listen.dropin.tmpl"
```
Expect `45`, `{"built": ..., "machine": "pi", "release": "73de1dad0ad0"}`, `-rwxr-xr-x ... bin/mira` and the template. Byte check (read-only, about 10 s):
```
T=$(mktemp -d "$SCR/cmp.XXXXXX"); git -C "$REPO" archive HEAD wall/mira_launch.py wall/mira_listen.py wall/mira_listen_v1.py wall/mira_shadow.py wall/mirakit wall/listener_plugins wall/bin/mira wall/deploy/mira-listen.dropin.tmpl | tar -x -C "$T"
(cd "$T/wall" && find . -type f | sort | xargs md5 -r | awk '{print $1, substr($2,3)}') > "$T/mac.txt"
PI "cd /home/mira/mira-listen/releases/$REL && find . -type f ! -name RELEASE.json | sort | xargs md5sum | awk '{print \$1, substr(\$2,3)}'" > "$T/pi.txt"
diff <(LC_ALL=C sort -k2 "$T/mac.txt") <(LC_ALL=C sort -k2 "$T/pi.txt") && echo IDENTICAL; rm -rf "$T"
```
Expect `IDENTICAL` (44 lines each).

b. The release's own checks pass on the Pi (read-only, a few seconds):
```
PI "$PM check --fast"
```
Expect `mira check: ok (17 plugins, fast)` (the Pi release holds the listener plugins only; `--fast` is the code-only rules), exit 0. Anything else means do not start.

c. The Pi is quiet. The restage worker was running the full suite on the Pi (scratch folder `/home/mira/.mira-restage-test.*`, throttled, about 32 minutes from 22:29). Nothing may start on the Pi until it is finished and cleaned, because it competes with the listener for CPU and a stage-test leftover must not be left in shared folders:
```
PI 'ps -eo pid,args | grep -c "[p]ytest"; ls -d /home/mira/.mira-restage-* /home/mira/.mira-setup.sh /tmp/pytest-of-mira /home/mira/.verify.* 2>&1 | head'
```
Expect `0` then only "No such file" lines. Also require the restage worker's own result: its report must say the full suite finished with no failures (expected 1447 passed, 8 skipped, the repair pass's count; the run log was `/home/mira/.mira-restage-test.*/run.log` before it deletes the folder). A running or failed suite means the release is not yet "staged and tested": do not start. If `/tmp/pytest-of-mira` exists, `PI 'rm -rf /tmp/pytest-of-mira'` (mira-owned scratch). Leftover `.mira-restage-*` or `.mira-setup.sh` belong to the restage worker: ask it to delete them; do not delete another worker's folder yourself while its process is alive.

d. The Pi still runs the originals (the md5s from the pre-flight and the stage verification):
```
PI 'md5sum /home/mira/mira-listen/mira_listen.py /home/mira/.config/mira/listen.env /home/mira/mira-listen/MIRA_WALL_IDENTITY.md /home/mira/.config/systemd/user/mira-listen.service /usr/local/bin/mira-audio; cat /etc/mira/say-volume'
```
Expect `8990842340a5ce68aa11515d0432f4ed`, `77909fedcf8c1939fb2354d48ac79fb3`, `cf5d457ff61e387169f8fbb1b5bc675f`, `9773a69a58b43c51c1d5f06f48e51e43`, `8bfbdbbfe656237b42e239cdc1fc36ea`, then `12`. A different persona md5 means someone edited it: merge their change into the repo persona before 2.2. `PI 'ls /home/mira/.config/mira/mira.json /home/mira/mira-listen/current /home/mira/.config/systemd/user/mira-listen.service.d 2>&1'` must say "No such file" three times (nothing adopted or applied yet).

e. Services up, no restarts (22:28 values: listener PID 64358, splitter PID 31452):
```
PI "$XDG systemctl --user show mira-listen mira-speech-split -p MainPID -p NRestarts -p ActiveState -p MemoryCurrent"
```
Both `active`, `NRestarts=0`. A different PID is fine if it is a known orchestrator restart; write it down.

f. The mini runs the original server:
```
MINI 'cd "$HOME/Library/Application Support/mira-speech" && md5 -q server.py && md5 -q "$HOME/Library/LaunchAgents/org.aryavora.mira-speech.plist" && ls snapshots server_v1.py 2>&1 | head -2; ls "$HOME/.config/mira/"; curl -s -m5 http://127.0.0.1:8765/health; echo; launchctl print gui/501/org.aryavora.mira-speech | grep -E "state =|pid =|runs ="'
```
Expect `ffe5ae0ee3881249f1102b27dd1755c4`, the plist `70ae0256c300db23c57b57216f19216c`, two "No such file" (snapshots, server_v1.py), a `.config/mira` listing of only `speech.token` (no `speech.json`, no state folder), `/health` with `"ok": true`, `"stt": "faster-whisper:tiny.en"`, `"voice": "amy"`, `state = running`, `runs = 1`, pid 22675 (22:31 value).

g. Ports and health on the Pi:
```
PI 'ss -ltn | grep -E ":(8650|8651) " || echo free; cat /sys/class/thermal/thermal_zone0/temp; free -m | sed -n 2p; uptime'
```
Expect `free`, temp under 70000, available memory over 800 MB (22:30 reading: 1.7 GB available, 50 C).

h. Mic state. The Pi must be listening on a live mic during the switch: `PI "$XDG journalctl --user -u mira-listen --no-pager -o cat | grep -E 'mic (live|muted)' | tail -1"` should end in `mic live` (22:28: `19:04:36 mic live`). Why it matters: Agent OS brings up the morning brief on a muted to live change of the mic state. If the mic is muted at the switch, the new process re-reports `muted` about 10 s after start (a mute is 10 s of digital zeros), which is correct, but the owner must not unmute until the new listener has said `mic muted` in the journal. See section 3.

i. The owner is in the room, knows the interruptions (0.4) and has said which night gain he wants (0.4).

### 0.2 If main moved after staging
If step a prints another commit, `REL` must be that new id and the Pi needs it staged. From `$WALL`: `python3 -B -m mirakit.cli deploy pi --dry-run` (read-only; expect `A` lines for every file since `current` does not exist yet, about 44, then `planned commands:`), then `python3 -B -m mirakit.cli deploy pi --stage` (about 30 to 60 s, runs `mira check --fast` in the release, refreshes the `~/.local/bin/mira` link, restarts nothing). Never run `deploy pi --activate`, plain `deploy pi`, or `deploy pi --rollback` tonight: they restart the unit without an owner window. If main did not move, do not re-stage: `--stage` deletes and recreates the release folder.

### 0.3 Back up (copies only, reversible)
```
PI "cd /home/mira && cp -p .config/mira/listen.env .config/mira/listen.env.bak-cutover-$D \
  && cp -p mira-listen/MIRA_WALL_IDENTITY.md mira-listen/MIRA_WALL_IDENTITY.md.bak-persona-$D \
  && cp -p .config/systemd/user/mira-listen.service .config/mira/mira-listen.service.bak-cutover-$D \
  && ls -la .config/mira | grep bak-cutover; ls -d .config/systemd/user/mira-listen.service.d 2>&1"
MINI 'cd "$HOME/Library/Application Support/mira-speech" && cp -p server.py server.py.bak-cutover-20261110 && cp -p "$HOME/Library/LaunchAgents/org.aryavora.mira-speech.plist" "$HOME/Library/LaunchAgents/org.aryavora.mira-speech.plist.bak-cutover-20261110" && ls -la server.py.bak-cutover-20261110 "$HOME/Library/LaunchAgents/"*.bak-cutover-*'
```
Expect the three Pi backups and "No such file" for the drop-in directory (there are no drop-ins yet). The mini backup of `server.py` is a second copy of the original on top of the one `deploy speech` makes (`server.py.bak-plugins-<Mac date>`). The plist backup sits in LaunchAgents with a name launchd ignores (it loads only `*.plist`; `.plist.bak-...` ends otherwise).

Record the listener's current startup line and settings for comparison (save to files on the Mac, nothing is changed):
```
mkdir -p "$SCR/cutover-before"
PI "$XDG journalctl --user -u mira-listen --no-pager -o cat | grep 'mira-listen up (' | tail -1" | tee "$SCR/cutover-before/startup-line.txt"
PI "$XDG systemctl --user show mira-listen -p MainPID -p ActiveEnterTimestamp -p NRestarts -p MemoryCurrent -p CPUUsageNSec" | tee "$SCR/cutover-before/unit-show.txt"
PI 'grep -vEi "token|key|secret|pass" /home/mira/.config/mira/listen.env' > "$SCR/cutover-before/listen.env.txt"
MINI 'curl -s -m5 http://127.0.0.1:8765/health' > "$SCR/cutover-before/mini-health.txt"
```
The startup line to match afterwards: `HH:MM:SS mira-listen up (mic), kws threshold 0.05 score 1.0, agc target 0.05 max 10.0x rest 3.0x gate 1.8` (the 19:04:35 line). Baselines for the watch: memory 92 MB; CPU averaged about 22 percent of one core since 19:04:17 (`CPUUsageNSec` 2673 s over about 3.4 h; the room was nearly silent, with one wake and no failures, so this baseline is thin). Journal baseline since 19:04: 1 wake, 1 `wake_accepted`, 0 `wake_rejected`, 0 failures, 1 `mic live`, 0 `Traceback`.

### 0.4 Tell the owner (get a yes for each window)
- About 35 minutes in all. Two interruptions: the mini speech restart (about 30 s, she cannot hear; her replies would still be spoken because speech goes through the Pi splitter to aiserver, but nobody can talk to her) and the Pi listener restart (about 20 s, deaf and silent). A third Pi restart comes later at M8. Say "Ready?" and wait for a yes before each.
- Night window. From 23:00 to 06:00 (Pi local time, America/New_York) she plays everything, replies, previews and the offline chime, at `night_gain` times normal. Her speaking level is the speaker helper's 12 percent (`/etc/mira/say-volume`), and the night gain scales the samples on top of that: 12 percent x 0.5 is 6 percent of full output, 6 dB quieter. With `night_gain` 0.7 it is 8.4 percent (3 dB quieter). Any spoken check after 23:00 will sound quiet, and it only starts when the plugin engine runs (after 2.6). It is now about 22:45, so the Pi checks will most likely fall in the night window. Ask once: keep the decided 0.5, or 0.7? The commands below carry `night_gain` only if he chooses 0.7.
- No recordings are needed and nothing plays unless a step says so.

---------------------------------------------------------------------------------------------------

## 1. M6: the mini speech server

INTERRUPTS: she cannot hear for about 30 s at 1.4. Everything before 1.4 changes files only and the running server keeps running the original. If the mini's server restarted for any reason between 1.1 and 1.4, it would still run the legacy server: `server.py` falls back to `server_v1.py` when `speech.json` is missing or says `core.engine` is `legacy` (code: `choose_engine`).

### 1.1 Dry run, then ship the files (no restart, about 10 s)
```
cd "$WALL" && python3 -B -m mirakit.cli deploy speech --dry-run
```
Read-only form. Expect 38 change lines: `M server.py`, `A server_v1.py`, `A bin/mira`, 19 `A mirakit/...`, 16 `A speech_plugins/...`; then `copied 73de1dad0ad0 to ...; snapshot .../snapshots/<stamp>.tar` and the kickstart hint (in a dry run these two lines are cosmetic, nothing was copied: check with `MINI 'ls "$HOME/Library/Application Support/mira-speech/snapshots"'` giving "No such file"), then `planned commands:` listing the snapshot, the `.bak` copy, the tar extract and the command link, all marked `+`. Then for real:
```
cd "$WALL" && python3 -B -m mirakit.cli deploy speech
```
About 5 s. Expect `mira command: ~/.local/bin/mira -> /Users/aryavora/Library/Application Support/mira-speech/bin/mira`, `copied 73de1dad0ad0 to /Users/aryavora/Library/Application Support/mira-speech; snapshot .../snapshots/<yyyymmdd-hhmmss>.tar` and `nothing was restarted; when you are ready: launchctl kickstart -k gui/501/org.aryavora.mira-speech`. Run it once only: a second run on another date would copy the new facade `server.py` into a newer `server.py.bak-plugins-<date>`, and the tool's rollback picks the newest `.bak`. The tool refuses to replace a plain file at `~/.local/bin/mira`; the mini has none.
Check (behaviour unchanged):
```
MINI 'cd "$HOME/Library/Application Support/mira-speech" && ls snapshots server_v1.py bin/mira && md5 -q server.py.bak-plugins-* server_v1.py && ls -l "$HOME/.local/bin/mira" && curl -s -m5 http://127.0.0.1:8765/health'
```
Expect the `.bak` and `server_v1.py` both `ffe5ae0ee3881249f1102b27dd1755c4`, the link, and `/health` as before with the same uptime counter still running (the process was not touched). Roll back this step: `cd "$WALL" && python3 -B -m mirakit.cli deploy speech --rollback` (restores the snapshot and `server.py` from the `.bak`; the new `server_v1.py`, `mirakit/`, `speech_plugins/`, `bin/mira` stay, harmless).

### 1.2 Adopt and create tokens (files only)
```
MINI "$MM adopt"
```
Read-only dry run. Expect exactly three rows, source `plist`: `server.http.bind ["127.0.0.1","100.77.98.89"]`, `stt.engine.kind faster-whisper:tiny.en`, `stt.engine.threads 4`, then `dry run: 3 values shown; pass --write ...`. Any other row: stop.
```
MINI "$MM adopt --write"
MINI "$MM tokens install --machine speech"
```
Expect `wrote /Users/aryavora/.config/mira/speech.json (core.engine stays legacy, core.legacy_env is ignore)` (the `core.legacy_env is ignore` part is why the plist variables stop mattering: their values are frozen into the file). Then a table: `owner .../control.owner.token created 0600` and `pi .../control.pi.token created 0600`, exit 0. Values are never printed. The Pi copy of the Pi token is an M8 step (section 5); skip it tonight.
Check: `MINI "$MM config validate; $MM doctor"`. Expect `ok: ... is valid for the speech`, and a doctor table with `config ok` (revision 1, engine legacy), `unit ok`, `token modes ok`, `binds ok`, `pins ok`; warnings are expected for `port` (the rehearsal, with nothing serving, said `port 8765 is free: the process is not serving it`; on the mini the legacy server is serving, so either wording is fine), `env ignored` (the three plist variables), `last good` (written after the first clean plugin boot). A row marked `fail` means stop and fix before 1.4.
Rollback of this step: `MINI 'rm "$HOME/.config/mira/speech.json"'` (the legacy server needs none; the control tokens can stay).

### 1.3 Set the engine (file only)
```
MINI "$MM set core.engine=plugins --offline --reason cutover"
```
Expect `core.engine legacy -> plugins pending_restart (S-000001)` and exit 7. Nothing changes until the restart.

### 1.4 Restart (OWNER WINDOW: announce "she is deaf for about 30 seconds", wait for "Ready")
```
MINI "$MM restart"
```
Writes `~/.config/mira/state/planned`, then runs `launchctl kickstart -k gui/501/org.aryavora.mira-speech`; expect `restarted org.aryavora.mira-speech`. On the mini, `restart` needs no token or server. If kickstart fails over ssh (service not found, domain permission), run the same `launchctl kickstart -k gui/501/org.aryavora.mira-speech` from a Terminal on the mini with computer use. Rollback right now: 1.7.

### 1.5 Verify (60 s, no sound)
```
sleep 25
MINI 'tail -n 6 "$HOME/Library/Logs/mira-speech/server.log"; echo ---; tail -n 5 "$HOME/Library/Logs/mira-speech/server.err.log"; curl -s -m5 http://127.0.0.1:8765/health; echo; launchctl print gui/501/org.aryavora.mira-speech | grep -E "state =|pid =|runs ="'
```
Expect in `server.log` `engine plugins: core.engine is plugins` then `plugin engine running`; an err log that is empty or holds only warnings (if it has a `Traceback`, roll back); `/health` with `ok`, `stt` faster-whisper:tiny.en, `tts` vits-piper-en_US-amy-medium, `voice` amy, a small `uptime_s`, plus the new fields `engine`, `revision`, `voice_id`; `state = running`, a new PID (not 22675), `runs = 2`. If `/health` is not up 60 s after the kickstart, roll back at once. About how long the gap lasts: the old server took about 30 s to load models; time yours from the kickstart to the first good `/health` and write it down.
```
MINI "$MM status; $MM inventory"
```
Online now (the tokens exist): expect `speech plugins ...`, no failed plugins; `inventory` lists the speech plugins `active`. (NOT REHEARSED against a live server.)
Round trip from the Pi over the real path, token on stdin (never in argv); the wav is `$WALL/tests/fixtures/mira_pause_the_music.wav`:
```
# the server needs 16 kHz mono 16-bit WAV; the fixture is 22.05 kHz, so convert it first
afconvert -f WAVE -d LEI16@16000 -c 1 "$WALL/tests/fixtures/mira_pause_the_music.wav" /tmp/stt16k.wav
PI 'umask 077; cat > /tmp/stt16k.wav' < /tmp/stt16k.wav
# stdin carries only the header config (curl -K -), the audio goes from the file, so the two never share a pipe
PI 'printf "header = \"Authorization: Bearer %s\"\n" "$(cat /home/mira/.config/mira/speech.token)" | curl -s -m20 -K - -H "Content-Type: audio/wav" --data-binary @/tmp/stt16k.wav http://100.77.98.89:8765/stt; rm -f /tmp/stt16k.wav'
rm -f /tmp/stt16k.wav
PI 'printf "header = \"Authorization: Bearer %s\"\n" "$(cat /home/mira/.config/mira/speech.token)" | curl -s -m20 -K - -H "Content-Type: application/json" -d "{\"text\":\"Testing the speech server.\"}" -D - -o /dev/null http://100.77.98.89:8765/tts | head -8'
```
First expect `{"text": "pause the music"...` (case and punctuation may differ) with `ms` under 3000. Second expect `HTTP/1.1 200`, `Content-Type: audio/wav` and an `X-Mira-Voice` header; the body goes to /dev/null so nothing plays. Skip the spec's `jenny` voice trial. `mira voices` is online only (offline it says "This machine has no voice catalog"): `MINI "$MM voices | head"` is a nice-to-have, not a gate.
Last, the owner says one wake phrase while the Pi is still the original listener: "Mira, what time is it". Hearing must work: `PI "$XDG journalctl --user -u mira-listen --since -2min --no-pager -o cat | tail -6"` shows `wake`, `wake_accepted`, `heard ...`, `reply ...`, `followup 8.0s` and no `offline`.

### 1.6 What to watch on the mini for the next 10 minutes
`MINI 'tail -n 20 "$HOME/Library/Logs/mira-speech/server.err.log"; launchctl print gui/501/org.aryavora.mira-speech | grep -E "runs ="'`. `runs` must stay 2. A rising count means launchd is restarting it.

### 1.7 Rollback of M6 (one command, about 30 s deaf again)
```
MINI "$MM rollback"
```
The server and the tokens exist by then, so it takes the online path (no "working on the file" line) and prints `core.engine plugins -> legacy pending_restart` followed by `restarted org.aryavora.mira-speech on the legacy engine`; if the server is down it says `no process answers ...; working on the file instead` and does the same on the file. Then check `/health` as in 1.5; the log shows `engine legacy: core.engine is legacy`. Owner sees: 30 s where a wake gets the offline chime.
Harder fallbacks, in order:
1. `MINI 'rm "$HOME/.config/mira/speech.json" && launchctl kickstart -k gui/501/org.aryavora.mira-speech'` (no settings file: legacy server, the same code as the original).
2. `MINI 'cd "$HOME/Library/Application Support/mira-speech" && cp -p server.py.bak-cutover-20261110 server.py && launchctl kickstart -k gui/501/org.aryavora.mira-speech'` (the original file back; md5 must be `ffe5ae0e...`).
3. `cd "$WALL" && python3 -B -m mirakit.cli deploy speech --rollback`, then kickstart.
Automatic fallback on the mini: the facade counts unplanned starts in `~/.config/mira/state/starts`; the 4th unplanned start inside 600 s (`core.crash_legacy_after` 4, window `core.crash_window_s` 600) runs the legacy server; an unimportable plugin module or an unreadable `speech.json` runs it at once. launchd's 15 s throttle between starts makes that more than a minute of no hearing, so do not wait for it: use 1.7.

---------------------------------------------------------------------------------------------------

## 2. M7: the Pi listener

INTERRUPTS: about 20 s deaf and silent at 2.6 (cold start 15.6 to 16.5 s measured under the scope, about 18 s live). Steps 2.1 to 2.5 change files and run `daemon-reload` only; the running listener keeps running the original `mira_listen.py` until 2.6. Do not start M7 until 0.1c is clean and M6 verified (so any trouble is on one machine at a time).

Since the repair pass, the window between adopt and the unit change is safe: the legacy mirror (`listen.env`, `legacy.args`) now carries the true persona and chime paths and only flags the v1 listener accepts, so even an unplanned restart in that window starts the same listener as before.

### 2.1 Pre-flight snapshot (read-only)
Already saved in 0.3. Re-read it: `cat "$SCR/cutover-before/startup-line.txt"`.

### 2.2 Persona (file only; the old listener reads it only at start)
The repo persona at main is the merged text (2709 bytes, sha256 `15cb030443630089afd3655920954d696331c4c99309c874656c36929b32540d`, today's live text byte for byte plus the "Where you are" section). Check the live file did not change since the backup, then install:
```
PI 'md5sum /home/mira/mira-listen/MIRA_WALL_IDENTITY.md'     # must still be cf5d457ff61e387169f8fbb1b5bc675f
shasum -a 256 "$WALL/MIRA_WALL_IDENTITY.md"                   # must be 15cb0304...2540d
cat "$WALL/MIRA_WALL_IDENTITY.md" | PI 'cat > /home/mira/mira-listen/.persona.new && sha256sum /home/mira/mira-listen/.persona.new'
PI 'install -m 644 /home/mira/mira-listen/.persona.new /home/mira/mira-listen/MIRA_WALL_IDENTITY.md && rm /home/mira/mira-listen/.persona.new && wc -c /home/mira/mira-listen/MIRA_WALL_IDENTITY.md'
```
Expect the same sha256, then `2709 /home/mira/mira-listen/MIRA_WALL_IDENTITY.md`. Rollback: `PI 'cp -p /home/mira/mira-listen/MIRA_WALL_IDENTITY.md.bak-persona-20261110 /home/mira/mira-listen/MIRA_WALL_IDENTITY.md'`. The added "quieter from eleven to six" sentence becomes true only once `night_window` runs (2.3 and 2.6).

### 2.3 Adopt, then the cutover values (files only; run 2.3 as one block)
Dry run first (read-only):
```
PI "$PM adopt"
```
Expect a table of 19 rows in the rehearsal (the dry run lists values whose source is the environment, a flag, the install, or that differ from the default; `mira adopt --all` shows every key, once, for review against `cutover/expected-mira.json.md`). The rows that must be there with these values:

| key | expected | source |
|---|---|---|
| speech.link.url | `http://127.0.0.1:8766` (the loopback splitter; the plugin cannot express split STT and TTS addresses, never change this) | env |
| speech.link.token_file | `/home/mira/.config/mira/speech.token` | flag |
| wake.kws.threshold | 0.05 | env |
| wake.kws.score | 1 | env |
| wake.kws.kws_dir | `/home/mira/mira-listen/kws` | flag |
| vad.silero.model | `/home/mira/mira-listen/models/silero_vad.onnx` | flag |
| agc.mic.on, target, max_gain, rest, gate | true, 0.05, 10, 3, 1.8 | env |
| intent.music.url | `http://100.77.98.89:4534` | env |
| intent.music.auth_file | `/home/mira/.config/mira/music_auth.json` | flag |
| brain.hermes.url, key_file | `http://127.0.0.1:8642`, `/home/mira/.config/mira/hermes_api_key` | env, flag |
| reporter.agentos.url, token_file | `http://127.0.0.1:3456/api/hooks/voice`, `/home/mira/.config/mira/voice_token` | env, flag |
| persona.file.path | `/home/mira/mira-listen/MIRA_WALL_IDENTITY.md` (an absolute path, not `${HERE}`) | install |
| playback.miraaudio.chime | `/home/mira/mira-listen/assets/offline.wav` (absolute) | install |

If a persona or chime row still shows `${HERE}`, adopt printed a warning that no such file exists beside the install: stop (the file is missing on the Pi; both exist now: persona 2709 bytes after 2.2, `assets/offline.wav` sha256 `2181dc9d...bf1ea`). Not produced by adopt, on purpose: `playback.miraaudio.night_window` (set next) and `control.http` (disabled until M8).
```
PI "$PM adopt --write"
PI "$PM set playback.miraaudio.night_window=23:00-06:00 persona.file.reread=turn --offline --reason cutover"
```
(Add `playback.miraaudio.night_gain=0.7` to the second command only if the owner chose it.) Expect `wrote /home/mira/.config/mira/mira.json (core.engine stays legacy, core.legacy_env is ignore)`, `wrote the legacy mirror next to it (listen.env, legacy.args)` (a `listen.env.bak-adopt-<date>` copy of the old file appears beside it), then two lines:
```
playback.miraaudio.night_window  -> 23:00-06:00 applied (L-000001)
persona.file.reread start -> turn applied (L-000001)
```
exit 0 (offline, file-only knobs report `applied`). The persona and chime need no `mira set` any more: the repaired adopt wrote the true paths. `mira.json` is revision 2 here (3 after 2.5). A dry run of the set is `--dry-run` (it prints `... dry_run (L-...)` and changes nothing).
Check the mirror:
```
PI 'grep -E "MIRA_IDENTITY_FILE|MIRA_CHIME|MIRA_SPEECH_URL|MIRA_KWS_THRESHOLD|MIRA_AGC_TARGET" /home/mira/.config/mira/listen.env; tail -4 /home/mira/.config/mira/legacy.args; grep -c releases /home/mira/.config/mira/listen.env /home/mira/.config/mira/legacy.args; ls -l /home/mira/.config/mira/mira.json'
```
Expect `MIRA_IDENTITY_FILE=/home/mira/mira-listen/MIRA_WALL_IDENTITY.md`, `MIRA_CHIME=/home/mira/mira-listen/assets/offline.wav`, `MIRA_SPEECH_URL=http://127.0.0.1:8766`, `MIRA_KWS_THRESHOLD=0.05`, `MIRA_AGC_TARGET=0.05`, a `legacy.args` tail of `--identity`, the persona path, `--chime`, the chime path (one token per line), `0` and `0` for the `releases` counts, and `mira.json` mode `-rw-------`.
Prove the v1 listener accepts the whole `legacy.args` (this is what the crash fallback and `mira rollback` will feed it; the repaired mirror should always pass, this is the check):
```
PI "cd /home/mira/mira-listen/releases/$REL && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /home/mira/mira-listen/venv/bin/python -B -c \"import mira_listen_v1 as v; a=[l for l in open('/home/mira/.config/mira/legacy.args').read().splitlines() if l]; v.build_parser().parse_args(a); print('legacy.args ok', len(a), 'tokens')\""
```
Expect `legacy.args ok <n> tokens` (32 in the rehearsal). An argparse error (`unrecognized arguments`) means the mirror holds a flag v1 rejects, which the repair should make impossible: stop, do not restart anything, and tell the owner (the crash fallback and `mira rollback` would both feed that file to v1). `mira rollback --offline` strips unknown flags as part of its run but also restarts, so use it only if you are ready to be on the legacy engine.
Rollback of 2.3: `PI 'cd /home/mira/.config/mira && cp -p listen.env.bak-cutover-20261110 listen.env && rm -f mira.json mira.json.lock legacy.args && rm -rf state history changes.jsonl'` (the original unit and listener never read `mira.json`).

### 2.4 Unit drop-in and `current` (files and daemon-reload; the process is untouched)
Read-only form first:
```
PI "$PM unit render --check"
```
Expect a unified diff adding `/home/mira/.config/systemd/user/mira-listen.service.d/mira.conf` with `ExecStart=`, `ExecStart=%h/mira-listen/venv/bin/python -B %h/mira-listen/current/mira_launch.py --config %h/.config/mira/mira.json`, `MemoryMax=200M`, `CPUQuota=40%`, `Nice=5`, `Restart=always`, `RestartSec=3`; exit 1 is normal (nothing installed). Then:
```
PI "$PM unit apply --release $REL"
```
Expect `wrote /home/mira/.config/systemd/user/mira-listen.service.d/mira.conf`, `current -> releases/73de1dad0ad0`, `systemctl --user daemon-reload`. Check: `PI "$XDG systemctl --user cat mira-listen | tail -12; $XDG systemctl --user show mira-listen -p MainPID -p NRestarts"` shows the drop-in text and the same MainPID as before. The unit now says "changed on disk"; that is expected and harmless until the restart. Rollback of 2.4: `PI "rm /home/mira/.config/systemd/user/mira-listen.service.d/mira.conf && $XDG systemctl --user daemon-reload"` (and `rmdir` the `.d` folder; the `current` link can stay).
If a crash happened now it would start `mira_launch.py` with `core.engine` still `legacy`, which runs `mira_listen_v1.py` with `legacy.args`: the original behaviour.

### 2.5 Engine last, then the validation
```
PI "$PM set core.engine=plugins --offline --reason cutover"
```
Expect `core.engine legacy -> plugins pending_restart (L-000002)` and exit 7. Then:
```
PI "$PM config validate; $PM doctor"
```
Expect `ok: ... is valid for the listener` and a table with `config ok` (revision 3, engine plugins, mode 0600), `unit ok` (the drop-in matches), `argv overrides ok`, `env ignored ok`, `binds ok`, `port ok` (control.http disabled, 8650 free), `pins ok` (sherpa-onnx 1.12.40). Expected warnings: `token modes` (control tokens come at M8) and `last good` (written after the first clean boot). Any `fail` row: stop and fix.
Re-run the `legacy.args` parse check of 2.3 once more (the `set` rewrote the mirror).

### 2.6 The restart (OWNER WINDOW: announce "deaf and silent for about 20 seconds", wait for "Ready")
```
PI "$PM restart --offline"
```
Use `--offline`: plain `mira restart` first tries the control API, which is off and has no token yet, so it stops with the token message after writing the planned marker without restarting. Expect `restarted mira-listen`. It writes `~/.config/mira/state/planned` (so the start counts as planned) and runs `systemctl --user restart mira-listen`. Do not touch the mic or speak until 2.7 passes.

### 2.7 Verify the process (within 60 s; journal first, then the owner)
```
sleep 22
PI "$XDG systemctl --user is-active mira-listen; $XDG systemctl --user show mira-listen -p MainPID -p NRestarts -p MemoryCurrent"
PI "ps -o args= -p \$($XDG systemctl --user show mira-listen -p MainPID --value)"
PI "$XDG journalctl --user -u mira-listen --since -90s --no-pager -o cat | cut -c1-200"
PI 'tail -3 /home/mira/.config/mira/state/starts; ls -la /home/mira/.config/mira/mira.json.lastgood'
```
Expect: `active`; a new MainPID (not 64358); `NRestarts=0`; process args containing `.../current/mira_launch.py --config /home/mira/.config/mira/mira.json` and NOT `mira_listen_v1.py` (that would mean the launcher fell back to legacy: read the `mira-launch:` journal lines for the reason); a startup line `mira-listen up (mic), kws threshold 0.05 score 1.0, agc target 0.05 max 10.0x rest 3.0x gate 1.8` identical to 0.3 apart from the time; `mic live` right after it (or `mic muted` after about 10 s if the mic is muted); no `Traceback`, no `boot:` or `config_error:` lines; `starts` ending `<epoch> planned`; `mira.json.lastgood` appearing within about a minute of the first clean boot; `MemoryCurrent` under 150 MB (baseline 92 MB, hard cap 200 MB). If the startup line is missing 45 s after the restart, roll back (2.9) before the owner speaks.
Tonight `mira status` and `mira inventory` fail (online, control.http off): `PI "$PM inventory --offline"` shows only the file state and proves nothing about the process. The journal is the evidence until M8.

### 2.8 The three things the owner says (one at a time; the owner speaks, the orchestrator reads the journal)
Read the journal after each with `PI "$XDG journalctl --user -u mira-listen --since -40s --no-pager -o cat | cut -c1-220"`. After 23:00 everything below plays at night level (see 0.4); the journal and the wall are the primary evidence, loudness is by ear.

1. A wake and a short answer. Owner: "Mira, what's on my calendar today?" Journal, in order: `wake spotter hit 'MIRA', agc <n>x`, `wake_accepted opens 'Mira, what's ...'`, `heard <the question>`, `reply <text>`, `followup 8.0s`. Checks: the reply is 25 words or fewer and plain speech (a long markdown style answer means the persona did not load: `from_file` cannot be read tonight, so judge by reply style); no `hermes failed`, no `voice hook failed`, no `offline` line, no `Traceback`. On the wall: idle, listening, thinking, speaking, idle in that order (the owner or a screenshot; `voice hook failed` absent is the machine check). Loudness: the same as before at the 12 percent level before 23:00, half the amplitude after.
2. A follow-up without her name. Within 8 seconds of her finishing (the follow-up window is `followup_s` 8.0, not 15): "And tomorrow?" Journal: `followup_capture <n>s` (this line is the proof that no wake word was needed), then `heard And tomorrow?`, `reply ...`, `followup 8.0s`. If you only see `wake_rejected` or nothing, the follow-up VAD did not catch it; repeat once inside 8 s before calling it a failure.
3. A music command. Owner: "Mira, pause the music." (start something playing first if nothing is, or accept the "Done" reply). Journal: `wake ...`, `wake_accepted ...`, `heard pause the music`, `music pause` (this line is the local intent: no Hermes round trip), `reply <short confirmation>`, `followup 8.0s`. Fail signs: `music-api failed` (reply "I could not reach the music player", check `PI 'curl -s -m5 -o /dev/null -w "%{http_code}\n" http://100.77.98.89:4534/'`), or a Hermes style long answer with no `music` line.

Then the plugin engine's resource use against the baselines: `PI "$XDG systemctl --user show mira-listen -p MemoryCurrent -p CPUUsageNSec"`; compute the average core share over 10 minutes from two readings 600 s apart (`delta CPUUsageNSec / 1e9 / 600`); the baseline is about 0.22 of a core, the cap 0.40. Alert above 0.35 sustained.

### 2.9 Rollback of M7
One command (about 20 s deaf again):
```
PI "$PM rollback --offline"
```
Expect `core.engine plugins -> legacy pending_restart (L-000003)`, then `legacy cannot represent these tuned values (they stay in the file for when you switch back):` listing `persona.file.reread = turn (legacy default start)` and `playback.miraaudio.night_window = 23:00-06:00 (legacy default )` (and `night_gain` only if changed), then `restarted mira-listen on the legacy engine`. Plain `mira rollback` also works tonight (the repair): with no usable owner token it prints `mira: no usable owner token at ...; working on the file instead (offline)` and does the same thing. The launcher then runs `mira_listen_v1.py` from the release with the tuned values of `legacy.args`; the persona and chime stay the shared absolute paths; night mode is off (v1 has none). Verify like 2.7, with one difference: the launcher replaces itself with `os.execv`, so the process args show only `.../releases/73de1dad0ad0/mira_listen_v1.py` followed by the `legacy.args` flags (no `mira_launch.py`); that is the correct legacy state. The journal shows `mira-launch:` only if it fell back; after a clean rollback the startup line matches 0.3 and `mic live` follows.
Fallbacks in order:
1. Last resort, runs the untouched original file with the original unit and environment:
```
PI "rm /home/mira/.config/systemd/user/mira-listen.service.d/mira.conf && cp -p /home/mira/.config/mira/listen.env.bak-cutover-20261110 /home/mira/.config/mira/listen.env && $XDG systemctl --user daemon-reload && $XDG systemctl --user restart mira-listen"
```
and, if the owner wants the old persona text too: `PI 'cp -p /home/mira/mira-listen/MIRA_WALL_IDENTITY.md.bak-persona-20261110 /home/mira/mira-listen/MIRA_WALL_IDENTITY.md'`. Then `md5sum /home/mira/mira-listen/mira_listen.py` is `8990842340a5ce68aa11515d0432f4ed` and `ps` shows `mira_listen.py` (not `mira_launch.py`).
2. `deploy pi --rollback` from the Mac needs `releases/.previous`, which exists only after a `--activate`; tonight it answers `no previous release recorded`. Skip it.
Automatic fallback: the launcher (`mira_launch.py`) records every start in `~/.config/mira/state/starts` (a marker younger than 60 s makes it `planned`). Within `core.crash_window_s` 600 s: the 3rd unplanned start turns the boot guard on (a cautious start that reverts after `crash_guard_revert_s` 1800 s), the 4th unplanned start execs the legacy listener from the release with `legacy.args`. With systemd's `RestartSec=3` that is roughly a minute after the first crash. An unimportable plugin engine or an unreadable `mira.json` fall back to legacy immediately. The fallback keeps the file saying `plugins`; after it, run the one-command rollback so the next planned restart does not try the plugin engine again, and read the journal for why.

### 2.10 The mic chain test (recommended, needs the owner's yes; journal only, it does not bring up the brief)
Ask the owner to press the mic's touch mute and wait 15 s, then unmute. Journal: `mic muted` about 10 s after the mute (10 s of digital zeros), then `mic live` within about a second of the unmute, and no `voice hook failed` around either. That proves the listener side of the 05:45 path on the new engine. It cannot prove the wall side tonight: `brief_on_wake` (agentic-os `pi/server/screens.py`) acts only between 05:00 and 11:00 local time, only once per day (an in-memory flag, set when it fires; it also skips if the rotation is paused), so a test at night shows nothing on the wall and does not use up tomorrow's brief. Do not repeat this test between 05:00 and 11:00: it would fire the brief and consume the day's flag.

---------------------------------------------------------------------------------------------------

## 3. After the switch: how long to watch, what is trouble, and 05:45

Watch for 24 hours (rung 7). Tonight stay for at least 30 minutes after 2.8, then check every 30 minutes until the owner sleeps, then once at about 05:30 and again at 06:00.
```
PI "$XDG systemctl --user show mira-listen -p NRestarts -p MemoryCurrent -p CPUUsageNSec -p ActiveEnterTimestamp; tail -5 /home/mira/.config/mira/state/starts"
PI "$XDG journalctl --user -u mira-listen --since -30min --no-pager -o cat | grep -E 'Traceback|mira-launch|boot:|config_error|invalid configuration|failed|offline|Speech is offline|speech machine call|change .* failed|wake_rejected' | cut -c1-200"
```
Lines that mean trouble:
- `mira-launch: crash loop: running the legacy listener`, `mira-launch: plugin engine not importable (...)`, `mira-launch: config unreadable: running the legacy listener`: the safety net fired. Roll back (2.9) and read why.
- `Traceback`, `boot: ...`, `config_error: ...; running the last-good file`, `invalid configuration`, `change <id> failed`: a config or boot fault.
- `offline stt:` or `offline tts:` marks, or the offline chime: the speech path (loopback splitter to the mini for STT, to aiserver for TTS) is down. Check the splitter (`mira-speech-split`) and the mini `/health` first.
- `hermes failed: ...`: the brain is unreachable. `music-api failed: ...`: the music player. `voice hook failed: ...`: Agent OS is not receiving state and mic posts (this one breaks the morning brief).
- `event <kind> failed`: a plugin event handler raised.
- `wake_rejected` far above baseline (baseline since 19:04 was 0 because the room was quiet; compare with the legacy listener's rate if you have it); many `heard nothing` rejects mean the spotter or AGC behave differently.
- `NRestarts` above 0, a new MainPID, any `unplanned` line in `state/starts` that you did not cause, `MemoryCurrent` over 150 MB (cap 200 MB, the unit kills it above), CPU share above 0.35 of a core sustained.
On the mini: `runs =` in `launchctl print` rising, any `Traceback` in `server.err.log`.

### What must hold at 05:45 (the morning brief)
Agent OS (`pi/server/ambient.py`) brings up the morning brief when it receives a post with `mic: live` while its stored mic state is `muted`. Its inputs come from this listener: `mute.zero` judges the mic (muted after 10 s of digital zeros, live again at the first block with fewer than 90 percent zeros, a `muted` repeat every 60 s while muted) and `reporter.agentos` posts `{state, level, heard, reply, at, mic}` to `http://127.0.0.1:3456/api/hooks/voice` with the `voice_token`. So by 05:45:
1. `mira-listen` is `active` and has not restarted in the last few minutes (a restart while muted is fine: it reports `muted` again about 10 s after start; a restart at the exact moment of unmuting can lose the transition).
2. `NRestarts=0` (or the restarts are explained), no `voice hook failed` lines overnight, and the plugin engine (or legacy after a rollback; both post `mic`) is the one running.
3. While the mic is muted the journal has one `mic muted` line from the last state change; there are no `mic live` flips overnight (a flip would have brought up the brief early and would mean zeros are not reaching 99 percent).
4. When he unmutes: the journal shows `mic live` at that moment and the wall shows the brief, unless the 06:00 push or an earlier unmute already showed it today (`brief_on_wake` fires once a day, between 05:00 and 11:00, and not while rotation is paused; at 05:45 on a school day the 06:00 push has not happened yet, so the unmute should bring it up). Check at 05:45 or just after: `PI "$XDG journalctl --user -u mira-listen --since '05:30' --no-pager -o cat | grep -E 'mic |failed|Traceback'"`.
5. Night gain applies to anything she speaks until 06:00 (6 percent of full output with the decided 0.5). If the brief ever includes spoken text before 06:00, it will be quiet by design.

---------------------------------------------------------------------------------------------------

## 4. M8: the control plane (not tonight; one more Pi restart of about 20 s)
`control.http` is restart class (repair pass): `mira enable` is an online command and needs the API it would start. Route: `PI "$PM tokens install --machine listener"` (creates the owner token 0600 and the agent token `/etc/mira/control.agent.token` 0640 root:mira with an ACL for user `agent`, through `sudo -n`, which changes `/etc/mira` from root:root to root:mira; `say-volume` and `speaker.env` are untouched), first copying the mini's Pi token without printing it: `MINI 'cat "$HOME/.config/mira/control.pi.token"' | PI 'umask 077; cat > /home/mira/.config/mira/speech.control.token && chmod 400 /home/mira/.config/mira/speech.control.token'`; then `PI "$PM enable control.http --offline"`, the legacy.args parse check of 2.3, an owner window, `PI "$PM restart --offline"`, then `mira status`, `mira inventory` (17 listener plugins, all active except `source.wav`), `mira doctor` (token modes now ok), `curl -s -m5 http://127.0.0.1:8650/v1/health`, and the 120 s canary `mira set agc.mic.target=0.06 --trial 120` (it reverts to 0.05 by itself). Rollback: `mira disable control.http --offline` plus `mira restart --offline`. Details are in the first runbook, section 4.

## 5. M9: the Hermes tools (not tonight; no listener restart)
Needs M8 first (every tool answers `control_unreachable` otherwise) and three owner decisions before anything is installed: the voice path (the plugin speech server cannot be what the splitter sends TTS to: aiserver's Kokoro `am_eric` ignores `speed`, `peak` and the mini's voice ids, so either run the plugin server on aiserver or drop `tts.engine.speed`, `peak` and `voice` from the agent range), the persona Honesty bullet versus SOUL.md, and the knowledge text fixes (map "too quiet" and "speak up" to the wall tool `voice_volume`, update the quiet-night facts). Stale copies elsewhere: the Hermes skill references, the vault notes and `agentic-os/pi/mira-mcp/mock_control.py` still call `wake.kws` rebuild class and `control.http` live; regenerate them from the repaired docs first. Install steps and rollback are in the first runbook, section 5.

---------------------------------------------------------------------------------------------------

## 6. What changed since the first runbook

- Release id is `73de1dad0ad0` (main, not the old feature-branch commit 71dcc7c). Main contains the work; there is no branch step. The old release folder `71dcc7c34598` is still on the Pi and unused; leave it (the wrapper picks the newest by mtime until `current` exists, and `unit apply --release` sets `current` explicitly).
- No hand steps for the two tooling gaps: the release ships `deploy/mira-listen.dropin.tmpl` and `bin/mira`, and `mira deploy` links `~/.local/bin/mira` (already done on the Pi; the mini gets it in 1.1).
- `mira adopt` writes the true persona and chime paths itself (source `install`), so the old `mira set persona.file.path=... playback.miraaudio.chime=...` step is gone; only `night_window` and `persona.file.reread=turn` are set after adopt.
- The legacy mirror only ever holds flags v1 accepts, so the old hand-run sanitizer step (3.4) became a read-only parse check.
- `mira rollback` works with no tokens and no server (file path, says so), so plain `mira rollback` is safe; `--offline` is kept in the commands because it is deterministic. Other online commands print one line that names `--offline`.
- `control.http` and every `wake.kws` knob are restart class (first runbook said live and rebuild).
- The speech server notes `busy` before the reply leaves (the test flake is fixed in code).
- Follow-up window is 8 s, not 15.
- Tonight's order: M6 (mini) before M7 (Pi); `--offline` on `set` and Pi `restart` until M8; `jenny` voice trial skipped; `deploy ... --activate` forbidden tonight.
- Added: a second backup copy of the mini's `server.py`, the byte-identical release check, the `mira check --fast` check, the restage-worker quiet check, the mini `launchctl` run counter, the mic chain test, and the 05:45 section.

## 7. What I still expect to go wrong on the real machines, and why

1. **Pi CPU competition.** The restage worker's suite (CPUQuota 40, nice 10) runs on the Pi until about 23:01. If it is still running, the restart's 18 s cold start can stretch and the scope's timing measurements are not comparable. Reason: both share 4 cores with the listener, kiosk and Agent OS. Gate 0.1c exists for this.
2. **Night volume will look like a failure.** After 23:00 the plugin engine plays at 6 percent of full output; an owner or orchestrator testing by ear may call it "too quiet" and roll back. Reason: the decision is real and the check is by ear because `mira inventory` cannot show the `night` flag until M8.
3. **`launchctl kickstart` over ssh on the mini** may be refused (domain permission or no GUI session). Reason: `kickstart` normally works only for the logged-in console user. Fallback is a Terminal on the mini through computer use. The Mac rehearsal's own `kickstart` returned "Could not find service" for the same label on the Mac, which only shows the command form is right.
4. **A longer mini gap than 30 s.** The plugin engine imports `mirakit` and the speech plugins before the model loads, and `faster-whisper:tiny.en` loads at start. Reason: the 30 s is the legacy server's figure; the first plugin boot has no measured time. If `/health` is not up at 60 s, roll back; the owner hears the offline chime meanwhile.
5. **`mira status` and `mira inventory` on the mini** (1.5) were not rehearsed against a live server, only the offline paths. Reason: the control API (`/ctl/v1/...` on port 8765, owner token) exists only inside a running plugin server. A 401 or a missing route there means the tokens were not read; it does not block the cutover, `/health` and the log lines are the gate.
6. **`mira voices` on the mini** printed "This machine has no voice catalog" offline in the rehearsal; online it is untested. Not a gate.
7. **The wall's state sequence** (idle, listening, thinking, speaking, idle) cannot be read by the orchestrator: Agent OS's `GET /api/glance/voice` answered 401 "active session required" from the Pi. Reason: it needs a session. The owner's eyes are the evidence, with `voice hook failed` absent as the machine check.
8. **The first follow-up may be missed.** `followup_s` is 8 s from the end of her speech; the cold plugin engine has had no room trial (rungs 5 and 6 were not run, the shadow run was dropped by the owner). The journal baseline is thin (one wake since 19:04). Expect to repeat check 2 once; a repeated miss inside the window is a real difference from the old listener (look at `followup_capture` timing) and should be recorded before any tuning.
9. **Wake rejects and CPU at the first real use.** The plugin engine's per-chunk path is new; the baseline for CPU (about 0.22 of a core) comes from an almost silent evening. Reason: the old numbers do not cover speech-heavy hours, so judge trends over hours, not minutes.
10. **Stale knowledge outside the repo** still says `control.http` is live and `wake.kws` is rebuild class (Hermes skill references, vault notes, the mock control API). It cannot affect tonight, but an agent that reads it may suggest a live toggle that cannot work.
11. **A rehearsal mishap to know about:** while rehearsing `mira rollback` for the mini on the Mac, the command ran `launchctl kickstart -k gui/501/org.aryavora.mira-speech` on this Mac (overriding HOME does not stop launchctl). The service does not exist here, so nothing happened. Do not rehearse mini commands that restart or roll back on the Mac again.
