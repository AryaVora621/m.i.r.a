#!/usr/bin/env python3
"""mira_launch: the listener's entry point under systemd (spec 4.3). Stdlib only, so it still runs when the plugin
engine cannot even be imported.

It picks the engine (`core.engine` in the config file: "plugins" or "legacy"), counts this start in
<cfgdir>/state/starts so a crash loop falls back to the frozen v1 listener, and then either runs
`mira_listen.main` here or replaces itself with `mira_listen_v1.py` carrying the tuned values of
<cfgdir>/legacy.args. Every sibling file is found through the config file's folder.
"""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLANNED_FRESH_S = 60.0
DEFAULT_WINDOW_S = 600.0
DEFAULT_LEGACY_AFTER = 4
DEFAULT_GUARD_AFTER = 3
KEEP_STARTS = 200


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] mira-launch: {msg}", file=sys.stderr, flush=True)


def config_arg(argv):
    """(path or None, argv with every --config removed)."""
    path, rest, i = None, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--config" and i + 1 < len(argv):
            path = argv[i + 1]
            i += 2
            continue
        if a.startswith("--config="):
            path = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    return path, rest


def read_config(path):
    """The parsed file, or None when it is missing, unreadable or not a JSON object."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def record_start(cfgdir, now):
    """Append '<epoch> planned|unplanned' to state/starts. A planned marker younger than 60 s makes this start planned
    and is consumed (an older one is stale and is dropped). Returns the unplanned epochs after this start."""
    state = Path(cfgdir) / "state"
    planned, kind = state / "planned", "unplanned"
    try:
        if planned.exists() and now - planned.stat().st_mtime <= PLANNED_FRESH_S:
            kind = "planned"
        planned.unlink(missing_ok=True)
    except OSError:
        pass
    starts = state / "starts"
    try:
        state.mkdir(parents=True, exist_ok=True)
        with open(starts, "a", encoding="utf-8") as f:
            f.write(f"{now:.0f} {kind}\n")
    except OSError as e:
        log(f"cannot record start ({e.strerror})")
    lines = []
    try:
        lines = starts.read_text(encoding="utf-8").splitlines()
    except OSError:
        pass
    if len(lines) > KEEP_STARTS:
        try:
            lines = lines[-KEEP_STARTS:]
            tmp = starts.with_name("starts.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, starts)
        except OSError:
            pass
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "unplanned":
            try:
                out.append(float(parts[0]))
            except ValueError:
                continue
    return out


def legacy_args(cfgdir):
    try:
        lines = (Path(cfgdir) / "legacy.args").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [ln for ln in lines if ln != ""]


def _num(core, key, default, cast):
    try:
        return cast(core.get(key, default))
    except (TypeError, ValueError):
        return default


def launch(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    flag_path, rest = config_arg(argv)
    cfg_path = flag_path or os.environ.get("MIRA_CONFIG") or None
    default_path = Path.home() / ".config" / "mira" / "mira.json"
    if cfg_path is None and default_path.exists():
        cfg_path = str(default_path)
    now = time.time()
    core = {}
    if cfg_path is not None:
        cfg = read_config(cfg_path)
        cfgdir = Path(cfg_path).parent
        if cfg is None:
            log("config unreadable: running the legacy listener")
            engine = "legacy"
        else:
            core = cfg.get("core") if isinstance(cfg.get("core"), dict) else {}
            engine = core.get("engine", "legacy")
    else:
        cfgdir = Path.home() / ".config" / "mira"
        engine = os.environ.get("MIRA_ENGINE", "legacy")
    window = _num(core, "crash_window_s", DEFAULT_WINDOW_S, float)
    legacy_after = _num(core, "crash_legacy_after", DEFAULT_LEGACY_AFTER, int)
    guard_after = _num(core, "crash_guard_after", DEFAULT_GUARD_AFTER, int)

    # A start is recorded whatever the engine: a crash loop of the legacy listener is not hidden by the fallback.
    n = sum(1 for t in record_start(cfgdir, now) if now - t <= window)
    if engine == "plugins" and n >= legacy_after:
        log("crash loop: running the legacy listener")
        engine = "legacy"
    if engine == "plugins" and n >= guard_after:
        os.environ["MIRA_BOOT_GUARD"] = "1"
    if engine == "plugins":
        try:
            if str(HERE) not in sys.path:
                sys.path.insert(0, str(HERE))
            import mira_listen
        except Exception as e:
            log(f"plugin engine not importable ({type(e).__name__}): running the legacy listener")
            engine = "legacy"
    if engine == "legacy":
        cmd = [sys.executable, "-B", str(HERE / "mira_listen_v1.py"), *rest, *legacy_args(cfgdir)]
        os.execv(sys.executable, cmd)
        return 0
    if flag_path is None and cfg_path is not None:
        argv = [*argv, "--config", cfg_path]
    return mira_listen.main(argv)


if __name__ == "__main__":
    sys.exit(launch())
