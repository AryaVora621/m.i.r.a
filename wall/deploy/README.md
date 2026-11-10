# Pi install for mira-listen (not done yet)

Prerequisites: workstream C reports PipeWire, the USB mic and the speaker working, and `/usr/local/bin/mira-audio`
(`duck`, `unduck`, `say FILE.wav`, `status`) exists. The Mac mini speech server must answer
`curl http://100.77.98.89:8765/health` from the Pi (the mini's Tailscale address: the server binds only loopback and that address, so the Pi must be on the tailnet; the bearer token is never sent over the LAN). Run as root or with sudo unless a line says `as mira`.
Backends (Hermes, Agent OS, music-api) are plain URLs in `listen.env`; none is assumed to live on the Pi.

1. Files and venv (as mira; the Pi is aarch64, Python 3):

       mkdir -p ~/mira-listen/models ~/mira-listen/kws ~/mira-listen/assets ~/.config/mira ~/.config/systemd/user
       python3 -m venv ~/mira-listen/venv
       ~/mira-listen/venv/bin/pip install "sherpa-onnx==1.12.40" numpy

   Pin 1.12.40: sherpa-onnx 2.0.x lost the espeak frontend (Piper TTS breaks) and the listener is tested on 1.12.40.

2. Copy from the Mac (repo `mira`): `wall/mira_listen.py`, `wall/MIRA_WALL_IDENTITY.md` to `~/mira-listen/`,
   `wall/assets/offline.wav` to `~/mira-listen/assets/`, `wall/models/silero_vad.onnx` to `~/mira-listen/models/`
   (or fetch it, see `wall/models/README.md`), and the keyword model `voice/models/kws/{tokens.txt,encoder.onnx,decoder.onnx,joiner.onnx,keywords.txt}`
   to `~/mira-listen/kws/` (that folder is gitignored in the repo; recipe in `voice/adapters/wake.py`).

3. Secrets, owned by mira, mode 0400, copied by the orchestrator (never printed, never in git):

       sudo install -o mira -g mira -m 0400 /opt/agent-os/data/voice_token.txt  ~mira/.config/mira/voice_token
       sudo install -o mira -g mira -m 0400 /opt/agent-os/data/hermes_api_key   ~mira/.config/mira/hermes_api_key
       sudo install -o mira -g mira -m 0400 <music_auth.json>                   ~mira/.config/mira/music_auth.json
       # speech token: from the Mac, over ssh, straight into the file
       ssh aryavora@100.77.98.89 'cat ~/.config/mira/speech.token' | sudo install -o mira -g mira -m 0400 /dev/stdin ~mira/.config/mira/speech.token

   `music_auth.json` is `{"u": "<navidrome user>", "t": "<subsonic token>", "s": "<salt>"}` (an admin Navidrome user,
   see music CONTRACT.md).

4. Config and service (as mira):

       cp deploy/listen.env.example ~/.config/mira/listen.env
       cp deploy/mira-listen.service ~/.config/systemd/user/
       systemctl --user daemon-reload && systemctl --user enable --now mira-listen
       journalctl --user -u mira-listen -f

   The mira user needs lingering (`loginctl enable-linger mira`, already on for the kiosk) and the service reaches
   PipeWire through the user session.

5. Checks: say "Mira, what time is it" near the mic. Expect, in the journal, `wake`, `heard`, `reply`; on the wall,
   states listening, thinking, speaking, idle. Idle CPU: `top -p $(pgrep -f mira_listen.py)` should sit under 20 percent
   of one core; if not, keep `--kws-threads 1` and check that nothing else is feeding the stream.
   Offline test: stop the speech server on the mini; the wall shows "Speech is offline" and a chime plays.

Tuning knobs (env or args): `MIRA_KWS_THRESHOLD` (0.05 on the wall; the code default is 0.15; higher is stricter),
`MIRA_KWS_SCORE` (1.0; raising it made recall worse on the room recordings), `--end-silence` (0.7 s), `--followup-s` (8),
`--session-ttl` (180 s), and the mic AGC: `MIRA_AGC` (1), `MIRA_AGC_TARGET` (0.05 rms), `MIRA_AGC_MAX_GAIN` (10x),
`MIRA_AGC_REST` (3x, the gain in a quiet room), `MIRA_AGC_GATE` (1.8, speech must beat the noise floor by this factor to
raise the gain), `MIRA_AGC_CEILING` (0.9 peak), `MIRA_AGC_RELEASE` (0.25 per 100 ms) and `MIRA_AGC_DECAY` (0.03).
`MIRA_LEVEL_LAG` (0.15 s) is how long `mira-audio say` takes to start sound, for the wall's reply level.

Journal: every spotter hit logs `wake spotter hit 'MIRA', agc 4.6x` and then its verdict, `wake_accepted opens 'Mira what time'`
(first three words only) or `wake_rejected heard 'Mirror, play some music'` (first 60 characters), so near-misses show up in
`journalctl --user -u mira-listen`. Wall levels: `listening` and `speaking` also post level-only updates (same state, new
`level`) up to 5 per second; while she speaks the level follows the reply audio's loudness.

Offline replay of a room recording through the live wake path (Pi, in the venv, niced; needs the speech token file):
`nice -n 19 ~/mira-listen/venv/bin/python -I wall/tests/room_eval.py TAKE.wav ... --kws-dir ~/mira-listen/kws --vad-model ~/mira-listen/models/silero_vad.onnx --speech-token-file ~/.config/mira/speech.token`.
Keep the recordings out of the repo.
