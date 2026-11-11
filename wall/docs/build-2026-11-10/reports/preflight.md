# Pre-flight P0 report (2026-11-10, about 19:40 to 19:58 EST)

Machine readable twin: preflight.json. Verdict: all P0 gates pass (checksums equal, T-pi 73 passed), but two things block a switch-over (below) and one gate decision changes the spec.

## Blockers and decisions
1. No room recordings of the owner exist on the Pi or this Mac. Rung 5 (room_eval, kws_report) cannot run until he records takes; announce first and wait for his Ready click.
2. Thin baseline: the 7 day journal covers only about 2 days (first start 2026-11-09 17:00), with 3 spotter hits, 2 accepted, 1 rejected. Rung 7 "within the day-to-day spread" has almost nothing to compare against.
3. Gate 2.3.3 fails: a spotter build takes 5.0 s unthrottled and 11.7 to 13.6 s under CPUQuota=40%, against a 2.0 s limit. Ship every wake.kws knob as `restart` (S), not rebuild (R). Peak RSS during an overlapped build was 99 MB (limit 170 MB), so memory is fine; time is the problem.
4. Cold start is long: 15.6 to 16.5 s to the first chunk in the scope; the live restart at 19:04:17 logged its "up" line at 19:04:35 (18 s). With RestartSec=3 each restart is about 19 to 21 s deaf.

## 5.1 item by item
1. Checksums: Pi mira_listen.py = 8990842340a5ce68aa11515d0432f4ed = mira_listen_v1.py (equal). Mini server.py = ffe5ae0ee3881249f1102b27dd1755c4 = speech/server_v1.py (equal).
2. Pi: Python 3.13.5, venv Python 3.13.5 (pip 25.1.1). pytest is not in the listener venv (not installed). 8650 and 8651 are free. 8766 is the speech splitter (127.0.0.1). Other listeners: 22, 25, 443, 465, 993, 995, 3001, 3456 (agent-os), 4190, 8025, 8080, 8642 (Hermes), 8770, 8790, 8791, 20241, plus ephemeral. 4 cores, 3.8 GB RAM (2.0 GB available), SoC 46.7 C, load average 4 to 6 with the kiosk running.
3. Baseline (counts only):
   - 2026-11-09: 4 starts, 1 spotter hit, 1 accepted, 0 rejected, 2 follow-up captures, 1 follow-up window, 2 empty, 0 offline, 1 reply.
   - 2026-11-10: 5 starts, 2 spotter hits, 1 accepted, 1 rejected, 1 follow-up window, 3 mic muted, 5 mic live, 2 voice hook failed (URLError), 1 reply, 0 offline, 0 no_speech.
   - Wake to reply: 39 s and 31 s (two samples, includes capture, STT and Hermes). AGC gain 2.1x to 4.4x (three samples).
   - Listener unit: PID 64358, NRestarts 0 (all 9 starts were planned stops), MemoryCurrent 78.7 MB then 98.1 MB ten minutes later (MemoryMax 200 M), VmHWM 149.7 MB, VmRSS 106.9 MB. Idle CPU over 600 s: 109.0 CPU seconds = 18.2 percent of one core. Caveat: my scratch runs shared the cores during that window, so treat it as an upper bound; take the rung-7 comparison number in a genuinely quiet window.
   - Mini: /health ok (faster-whisper:tiny.en, vits-piper-en_US-amy-medium, voice amy, uptime 18.6 h). server.log since 2026-11-09 10:06: TTS 392 requests, p50 332 ms, p95 867 ms, max 1450 ms (rtf p50 0.15); STT 240 requests, p50 331 ms, p95 1051 ms, max 2202 ms; one 401 each on /tts and /stt, one 404 on /api/agent/screens; error log empty.
4. Rebuild and cold start (scope CPUQuota=40%, MemoryMax=200M, nice 10 inside; WAV of low noise; fake mira-audio never called): spotter build 13592, 13090, 12113 ms; dispose then build 13471, 12201, 11669 ms; unthrottled nice 10 build 5035, 5110, 5081 ms; VmHWM 42 MB after import, 81 MB after build and after dispose plus rebuild, 99 MB when two spotters overlap; VAD build 92, 93, 30 ms; import 1.5 to 2.9 s; cold start to first chunk 15649, 16533, 16115 ms.
5. T-pi: 73 passed in 187.61 s, exit 0 (four legacy files, v1 as mira_listen.py, v1 server as speech/server.py, live mini STT and TTS for the 8 e2e tests). A first run gave 1 failed, 72 passed because I left MIRA_WALL_IDENTITY.md out of the scratch copy (persona fell back to the one-line default); adding it fixed it. Future staged copies must include that file. pytest 9.2.0 went into a scratch `--target` dir, never the live venv.
6. Hermes: default profile MCP servers mail, owner-tasks, printers, vault, wall; claude profile owner-tasks, printers, wall. platform_toolsets lists only api_server. MCP is per profile, so F11 holds (a mira MCP would be visible to every default-profile session). owner-tasks exposes assign_task (plus list_tasks, get_task, withdraw_task).
7. Room recordings: none. Only /tmp/mon.wav, m_mon.wav, song.wav, m_song.wav on the Pi (made 17:15 to 17:16 today, not opened, probably music or TTS tests) and the chime assets/offline.wav; on the Mac the Piper fixtures, wake_samples/mira_01..05.wav and kws test_wavs. Ask the owner.

## Comparison state (for later)
- Listener: PID 64358, start Tue Nov 10 19:04:17 2026, ActiveEnterTimestamp Tue 2026-11-10 19:04:17 EST, NRestarts 0. listen.env md5 77909fedcf8c1939fb2354d48ac79fb3. Persona file (MIRA_WALL_IDENTITY.md) md5 cf5d457ff61e387169f8fbb1b5bc675f. Unit file md5 9773a69a58b43c51c1d5f06f48e51e43.
- Splitter (mira-speech-split, proxy.py): PID 31452, ActiveEnterTimestamp Tue 2026-11-10 17:55:52 EST, NRestarts 0, proxy.py md5 c12203606e83c38915da5f28032369d0, speech-split.env md5 63dcea6ad906d44b5632f1953ae27da0, unit md5 16210691e50b8c42f70c78097f8a09f4.
- Mini speech server: launchd PID 22675, started Tue Nov 10 01:05:55 2026, binds 127.0.0.1 and 100.77.98.89 on 8765, plist env MIRA_STT=faster-whisper:tiny.en, MIRA_SPEECH_BIND=127.0.0.1,100.77.98.89, MIRA_THREADS=4, Nice 5.
- Checked again after the scratch runs: listener PID, start time, NRestarts and both md5s unchanged.

## Side effects, disclosed
My three cold-start runs left the voice hook URL at its default, so each posted one "mic live" state to agent-os at 127.0.0.1:3456 with no token. It answered HTTPError (rejected), so the wall did not change. The T-pi run used fakes for the hook. The scratch dir on the Pi (mode 700 under /home/mira) and my Mac staging files are deleted. Nothing live was restarted or edited.

## Things to save to the vault (this worker is read-only for it)
Spotter build 5 s unthrottled and 12 to 13 s under the unit's quota; cold start about 16 to 18 s; wake.kws must be restart-class; stage copies need MIRA_WALL_IDENTITY.md; `Nice=` is not valid for systemd-run scopes (use `nice -n 10` inside); the Pi keeps only about 2 days of listener journal.
