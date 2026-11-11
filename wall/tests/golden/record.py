"""Golden recorder: runs one scenario through a listener module against the fakes and returns a canonical dict.

Usage (from wall/):
  python -B tests/golden/record.py --record [name ...]     write tests/golden/<scenario>.json from mira_listen_v1
  python -B tests/golden/record.py --determinism N [name ...]   run each scenario N times with v1 and report any diff

Instrumentation touches only instance attributes the refactor keeps (spec 4.1): `lis.agc.process`, `lis._mark`,
`lis.capture`, `lis.src.read`. Everything is called through, so behaviour is unchanged.

Normalization of the hook posts (the reporter thread's wall-clock timing makes level-only posts and the
coalescing of text changes vary from run to run; nothing else is touched):
  1. drop the `at` timestamp;
  2. drop a post whose (state, heard, reply, mic) equals the previous post's: that is a level-only post
     (or a repeated mic report), and its level depends on timing;
  3. merge consecutive posts with the same (state, mic), keeping the last (a text-only change either
     replaced a queued item or went out as a second post, depending on timing);
  4. round `level` to 2 decimals.
`mic` and the order of state changes are kept. Hermes session titles carry a timestamp and become "<title>".
Temp file names passed to `mira-audio say` become "<tmp>" (the file's sha256 is kept).
"""
import hashlib
import importlib
import json
import os
import platform
import re
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

GOLDEN = Path(__file__).resolve().parent
WALL = GOLDEN.parent.parent
REPO = WALL.parent
for p in (str(WALL), str(GOLDEN.parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

from golden import fakes, scenarios  # noqa: E402


def normalize_hook(items):
    """items: [(t, body)] in arrival order; see the module docstring for the rules."""
    posts = []
    prev = None
    for _, b in items:
        b = {k: v for k, v in b.items() if k != "at"}
        key = (b.get("state"), b.get("heard"), b.get("reply"), b.get("mic"))
        if key == prev:
            continue
        prev = key
        posts.append(b)
    merged = []
    for b in posts:
        if merged and (merged[-1].get("state"), merged[-1].get("mic")) == (b.get("state"), b.get("mic")):
            merged[-1] = b
        else:
            merged.append(b)
    for b in merged:
        b["level"] = round(float(b.get("level", 0.0)), 2)
    return merged


def _normalize_hermes(calls):
    out = []
    for c in calls:
        body = json.loads(c["body"]) if c["body"] else None
        if c["path"] == "/api/sessions" and body and "title" in body:
            body = dict(body, title=re.sub(r"^Mira wall .*", "<title>", body["title"]))
        out.append({"path": c["path"], "body": body})
    return out


def _normalize_audio(lines):
    out = []
    for ln in lines:
        m = re.fullmatch(r"say (\S+) (\w+)", ln)
        if m:
            name = m.group(1) if m.group(1) == "offline.wav" else "<tmp>"
            out.append(f"say {name} {m.group(2)}")
        else:
            out.append(ln)
    return out


def argv_for(sc, tmp, urls, tokens, audio_bin):
    argv = ["--kws-dir", str(REPO / "voice" / "models" / "kws"),
            "--vad-model", str(WALL / "models" / "silero_vad.onnx"),
            "--identity", str(Path(__file__).parent / "persona_fixture.md"),  # frozen text: the recordings embed it, the shipped persona changes
             "--chime", str(WALL / "assets" / "offline.wav"),
            "--speech-url", urls["speech"], "--speech-token-file", tokens["speech"],
            "--hermes-url", urls["hermes"], "--hermes-key-file", tokens["hermes"],
            "--voice-url", urls["voice"] + "/api/hooks/voice", "--voice-token-file", tokens["voice"],
            "--music-url", urls["music"], "--music-auth-file", tokens["music"],
            "--mira-audio", str(audio_bin), "--error-hold", "0.3"]
    if scenarios.KWS_THRESHOLD:
        argv += ["--kws-threshold", scenarios.KWS_THRESHOLD]
    return argv + list(sc.get("extra", []))


def run_scenario(module, scenario, tmpdir):
    """Run `scenario` (dict or name) through `module` (an imported listener module); return the canonical dict."""
    sc = scenarios.BY_NAME[scenario] if isinstance(scenario, str) else scenario
    tmp = Path(tmpdir)
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "home").mkdir(exist_ok=True)
    wav = tmp / "in.wav"
    wav.write_bytes(module.pcm16_wav_bytes(sc["audio"](module)))
    tokens = fakes.write_tokens(tmp)
    audio = fakes.FakeAudio(tmp)
    stt = sc.get("stt", [])
    speech = fakes.FakeSpeech([] if stt == "down" else stt, sc.get("tts_errors"))
    hermes = fakes.FakeHermes(sc.get("hermes", []))
    hook = fakes.FakeHook()
    music = fakes.FakeMusic(sc.get("volumes", [50]))
    urls = {"speech": f"http://127.0.0.1:{fakes.free_port()}" if stt == "down" else speech.url,
            "hermes": hermes.url, "voice": hook.url, "music": music.url}
    saved = dict(os.environ)
    for k in [k for k in os.environ if k.startswith("MIRA_")]:
        del os.environ[k]
    os.environ["HOME"] = str(tmp / "home")
    agc_digests, marks, caps, raw = [], [], [], [0]
    exception = None
    try:
        args = module.build_parser().parse_args(argv_for(sc, tmp, urls, tokens, audio.bin))
        lis = module.Listener(args, module.WavSource(str(wav)))

        if lis.agc is not None:
            orig_process = lis.agc.process

            def process(c):
                out = orig_process(c)
                agc_digests.append(hashlib.sha256(np.ascontiguousarray(out, dtype=np.float32).tobytes()).hexdigest())
                return out
            lis.agc.process = process

        orig_mark = lis._mark

        def mark(kind, detail=""):
            r = orig_mark(kind, detail)
            marks.append([kind, detail, round(lis.src.clock(), 3)])
            return r
        lis._mark = mark

        orig_capture = lis.capture

        def capture(*a, **kw):
            start = round(lis.src.clock(), 3)
            r = orig_capture(*a, **kw)
            caps.append([start, round(lis.src.clock(), 3), None if r is None else int(len(r))])
            return r
        lis.capture = capture

        orig_read = lis.src.read

        def read():
            c = orig_read()
            if c is not None:
                raw[0] += 1
            return c
        lis.src.read = read

        try:
            lis.run()
        except Exception as e:  # S16 expects ValueError to propagate; record the type, never hide it
            exception = type(e).__name__
    finally:
        os.environ.clear()
        os.environ.update(saved)
        for s in (speech, hermes, hook, music):
            s.stop()
    return {
        "agc_digests": agc_digests,
        "raw_chunks": raw[0],
        "events": marks,
        "captures": caps,
        "hook": normalize_hook(hook.posts()),
        "hermes_bodies": _normalize_hermes(hermes.calls),
        "stt_sha256": speech.stt_sha256(),
        "tts_bodies": [json.loads(c["body"]) for c in speech.calls if c["path"] == "/tts"],
        "audio_calls": _normalize_audio(audio.calls()),
        "music_calls": [{"method": c["method"], "path": c["path"], "body": c["body"].decode() or None,
                         "headers": {k: v for k, v in c["headers"].items() if k.startswith("x-music-")}}
                        for c in music.calls],
        "exception": exception,
        "_raw_hook": [(round(t, 3), b) for t, b in hook.posts()],
    }


def canonical(result):
    """The comparable part of a result: no timing-bearing raw hook list."""
    return json.loads(json.dumps({k: v for k, v in result.items() if not k.startswith("_")}))


_CACHE = {}


def cached_run(modname, name):
    """One run per (module, scenario) per process; the parity and characterization tests share it."""
    key = (modname, name)
    if key not in _CACHE:
        module = importlib.import_module(modname)
        with tempfile.TemporaryDirectory() as d:
            _CACHE[key] = run_scenario(module, name, d)
    return _CACHE[key]


def stored_path(name):
    return GOLDEN / f"{name}.json"


def record(names):
    module = importlib.import_module("mira_listen_v1")
    for name in names:
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory() as d:
            res = canonical(run_scenario(module, name, d))
        doc = {"scenario": name, "machine": platform.machine(), "numpy": np.__version__, "result": res}
        stored_path(name).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
        print(f"recorded {name} in {time.monotonic() - t0:.1f}s", flush=True)


def determinism(names, n):
    module = importlib.import_module("mira_listen_v1")
    bad = 0
    for name in names:
        runs = []
        for _ in range(n):
            with tempfile.TemporaryDirectory() as d:
                runs.append(canonical(run_scenario(module, name, d)))
        for i, r in enumerate(runs[1:], 1):
            for k in r:
                if r[k] != runs[0][k]:
                    bad += 1
                    print(f"DIFF {name} run {i} key {k}")
                    print("  a:", json.dumps(runs[0][k])[:600])
                    print("  b:", json.dumps(r[k])[:600])
        print(f"{name}: {'stable' if not bad else 'see diffs'}", flush=True)
    return bad


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--record":
        record(argv[1:] or scenarios.NAMES)
    elif argv and argv[0] == "--determinism":
        sys.exit(1 if determinism(argv[2:] or scenarios.NAMES, int(argv[1])) else 0)
    else:
        print(__doc__)
