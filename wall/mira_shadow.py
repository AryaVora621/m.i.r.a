#!/usr/bin/env python3
"""mira_shadow: a counting-only second listener for the shadow run (spec 6.6).

It runs the front of the listener beside the live one: its own microphone source, the mute detector, the AGC and the
wake spotter, with the live listener's values. It never captures speech, never calls STT, Hermes, the wall or the
speaker, and it writes nothing outside its work folder (no legacy mirror, no change log, no control plane). Output is
one line per event on stdout:

    shadow hit t=<epoch> agc=<gain>          one per spotter hit
    shadow mic t=<epoch> muted|live          one per mute verdict change
    shadow stats t=<epoch> cpu=<percent> vmhwm_kb=<kb>    every --stats-s seconds

tests/tools/shadow_compare.py compares these lines with the live journal.

    python mira_shadow.py --config /tmp/mira-shadow/mira.json [--workdir /tmp/mira-shadow] [--wav FILE]
"""

import argparse
import json
import os
import resource
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import listener_plugins  # noqa: E402
from mirakit.core import Core  # noqa: E402

KEEP = ("source.pipewire", "source.wav", "mute.zero", "agc.mic", "wake.kws")
RATE = 16000


def say(line):
    print(line, flush=True)


def vmhwm_kb():
    try:
        for ln in Path("/proc/self/status").read_text().splitlines():
            if ln.startswith("VmHWM:"):
                return int(ln.split()[1])
    except (OSError, ValueError):
        pass
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak / 1024) if sys.platform == "darwin" else int(peak)   # macOS reports bytes, Linux KiB


def shadow_config(src_path, wav=None):
    """The adopted config cut down to the plugins the shadow runs. Everything else is dropped, so nothing in it can
    start a network client or a port; the engine and revision keys stay so the file still loads."""
    doc = json.loads(Path(src_path).read_text(encoding="utf-8"))
    plugins = doc.get("plugins") if isinstance(doc.get("plugins"), dict) else {}
    keep = {k: v for k, v in plugins.items() if k in KEEP}
    pw = keep.get("source.pipewire") or {"config": {}}
    wv = keep.get("source.wav") or {"config": {}}
    if wav:
        keep["source.pipewire"] = dict(pw, enabled=False)
        keep["source.wav"] = dict(wv, enabled=True, config=dict(wv.get("config") or {}, path=str(wav)))
    else:
        keep["source.pipewire"] = dict(pw, enabled=True)
        keep["source.wav"] = dict(wv, enabled=False)
    for name in ("mute.zero", "agc.mic", "wake.kws"):
        keep.setdefault(name, {"enabled": True, "config": {}})
        keep[name] = dict(keep[name], enabled=True)
    doc["plugins"] = keep
    doc["machine"] = "listener"
    core = doc.get("core") if isinstance(doc.get("core"), dict) else {}
    doc["core"] = dict(core, engine="plugins")
    return doc


def build_core(config, workdir, wav=None):
    """The shadow's Core: file copy under workdir, HOME under workdir, no mirror, no change log."""
    workdir = Path(workdir)
    (workdir / "home").mkdir(parents=True, exist_ok=True)
    path = workdir / "shadow-config.json"
    path.write_text(json.dumps(shadow_config(config, wav), indent=1), encoding="utf-8")
    os.environ["HOME"] = str(workdir / "home")
    specs = [s for s in listener_plugins.specs() if s.name in KEEP]
    return Core.from_file(str(path), argv=[], machine="listener", specs=specs, parser=None, mirror=False,
                          changelog=False, here=HERE, home=workdir / "home", required_services=())


def run(core, stats_s=600.0, stop=None, out=say):
    """The counting loop. Returns the number of hits. `stop` is a threading.Event-like with is_set()."""
    src, mute, agc, kws = core.service("source"), core.service("mute", None), core.service("agc", None), core.service("wake")
    stream = kws.create_stream()
    hits = 0
    t_wall, t_cpu = time.time(), time.process_time()
    next_stats = time.monotonic() + stats_s
    try:
        while stop is None or not stop.is_set():
            c = src.read()
            if c is None:
                break
            if mute is not None:
                verdict = mute.feed(c)  # the raw chunk, as the live loop feeds it
                if verdict in ("muted", "live"):
                    out(f"shadow mic t={time.time():.3f} {verdict}")
            if agc is not None:
                c = agc.process(c)
            stream.accept_waveform(RATE, c)
            while kws.is_ready(stream):
                kws.decode_stream(stream)
            if kws.get_result(stream):
                hits += 1
                out(f"shadow hit t={time.time():.3f} agc={float(agc.gain) if agc is not None else 1.0:.2f}")
                kws.reset_stream(stream)
            if stats_s and time.monotonic() >= next_stats:
                now, cpu = time.time(), time.process_time()
                out(f"shadow stats t={now:.3f} cpu={100.0 * (cpu - t_cpu) / max(now - t_wall, 1e-6):.1f} vmhwm_kb={vmhwm_kb()}")
                t_wall, t_cpu, next_stats = now, cpu, time.monotonic() + stats_s
    finally:
        src.close()
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser(description="Counting-only shadow of the listener front end")
    ap.add_argument("--config", required=True, help="the adopted mira.json (read only)")
    ap.add_argument("--workdir", default="/tmp/mira-shadow")
    ap.add_argument("--wav", help="read this WAV instead of the microphone (tests)")
    ap.add_argument("--stats-s", type=float, default=600.0)
    args = ap.parse_args(argv)
    core = build_core(args.config, args.workdir, args.wav)
    core.boot()
    stop = _Stop()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    say(f"shadow up t={time.time():.3f} source={'wav' if args.wav else 'mic'}")
    try:
        run(core, args.stats_s, stop)
    except KeyboardInterrupt:
        pass
    finally:
        core.shutdown()
    return 0


class _Stop:
    def __init__(self):
        self._set = False

    def set(self):
        self._set = True

    def is_set(self):
        return self._set


if __name__ == "__main__":
    sys.exit(main())
