# wall: Mira's voice for the wall

- `speech/server.py`: `mira-speech`, runs on the Mac mini. STT (sherpa-onnx Moonshine base) and TTS (sherpa-onnx Piper,
  amy-medium) over HTTP on :8765 with a bearer token. `speech/bench_stt.py`, `speech/bench_tts.py` made the choices.
  `speech/org.aryavora.mira-speech.plist` is the LaunchAgent.
- `mira_listen.py`: runs on the Pi. Wake word, VAD, then mini STT, Hermes, mini TTS, `mira-audio`; posts voice state to
  Agent OS. `--wav FILE` runs it on a recording on any machine with sherpa-onnx.
- `MIRA_WALL_IDENTITY.md`: the persona sent to Hermes as per-turn `instructions`.
- `tests/`: `python -m pytest -q` from this folder (needs the mini server and a token file, see `tests/conftest.py`).
  `python tests/kws_report.py` prints wake-word hit and false-trigger counts.
- `deploy/`: Pi service file and install steps (not installed).
