"""`mira`, the one tuning command (spec 2.8). Stdlib only at import time.

Online it talks to the control API with the owner token; `--offline` edits the file under the lock through
`mirakit.core.Core.from_file` (refused with exit 8 unless given, so nobody edits behind a running process by
accident). Both paths return the same JSON shapes: the offline backend runs the very same dispatcher as the
HTTP server, in process, with the owner scope.

Heavy modules (core, envmap, the plugin packages, mira_listen) are imported inside the commands that need them,
so `mira --help` and the online commands work with nothing installed but the standard library.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shlex
import shutil
import socket
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib import error as urlerror
from urllib import request as urlrequest

EXIT_OK, EXIT_USAGE, EXIT_UNKNOWN, EXIT_QUEUED, EXIT_POLICY, EXIT_CONFLICT, EXIT_FAILED, EXIT_PENDING, \
    EXIT_UNREACHABLE, EXIT_SPEECH_DOWN = range(10)

EXIT_BY_CODE = {
    "invalid": EXIT_USAGE, "unknown_voice": EXIT_USAGE, "internal": EXIT_USAGE, "missing_service": EXIT_USAGE,
    "unknown_key": EXIT_UNKNOWN, "unknown_plugin": EXIT_UNKNOWN,
    "unauthorized": EXIT_POLICY, "policy": EXIT_POLICY, "not_in_voice_turn": EXIT_POLICY, "quiet_hours": EXIT_POLICY,
    "session_cap": EXIT_POLICY, "agents_locked": EXIT_POLICY, "rate_limited": EXIT_POLICY, "busy": EXIT_POLICY,
    "stale_revision": EXIT_CONFLICT, "undo_conflict": EXIT_CONFLICT, "file_changed": EXIT_CONFLICT,
    "provider_conflict": EXIT_CONFLICT,
    "speech_unreachable": EXIT_SPEECH_DOWN,
}
STATUS_EXIT = {"applied": EXIT_OK, "confirmed": EXIT_OK, "dry_run": EXIT_OK, "reverted": EXIT_OK,
               "queued": EXIT_QUEUED, "pending_restart": EXIT_PENDING, "failed": EXIT_FAILED}
SEVERITY = [EXIT_FAILED, EXIT_PENDING, EXIT_QUEUED]      # worst first when one command returns several changes

COMMANDS = ("status", "inventory", "describe", "get", "set", "reset", "undo", "confirm", "changes", "session",
            "enable", "disable", "reload", "restart", "agents", "preview", "voices", "schema", "tuning-guide",
            "config", "adopt", "unit", "rollback", "deploy", "tokens", "doctor", "check", "docs", "plugin")
PATH_FLAGS = {"--config", "--kws-dir", "--vad-model", "--speech-token-file", "--hermes-key-file", "--voice-token-file",
              "--music-auth-file", "--identity", "--chime"}
MACHINE_UNITS = {"listener": "mira-listen", "speech": "org.aryavora.mira-speech"}


class CliError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class ApiError(Exception):
    def __init__(self, status: int, body: Mapping[str, Any]) -> None:
        super().__init__(str(body.get("message", "")))
        self.status, self.body = status, dict(body)


class Unreachable(Exception):
    pass


# --------------------------------------------------------------------------------------------
# context and backends
# --------------------------------------------------------------------------------------------

@dataclass
class Ctx:
    args: argparse.Namespace
    env: Mapping[str, str]
    home: Path
    out: Callable[[str], None]
    err: Callable[[str], None]
    root: Path                                   # the wall/ directory of the running code
    machine: str = "listener"
    cfg_path: Path = field(default_factory=Path)
    backend: Any = None
    runner_factory: Optional[Callable[..., Any]] = None
    editor: Optional[Callable[[Sequence[str]], int]] = None
    offline_opener: Optional[Callable[["Ctx"], Any]] = None
    local_runner: Any = None

    @property
    def cfgdir(self) -> Path:
        return self.cfg_path.parent

    @property
    def json(self) -> bool:
        return bool(getattr(self.args, "json", False))

    def opt(self, name: str, default: Any = None) -> Any:
        return getattr(self.args, name, default)


def detect_machine(env: Mapping[str, str], home: Path, config_arg: str = "") -> str:
    named = env.get("MIRA_MACHINE", "")
    if named in ("listener", "speech"):
        return named
    if config_arg:
        try:
            m = json.loads(Path(config_arg).read_text()).get("machine")
            if m in ("listener", "speech"):
                return m
        except (OSError, ValueError, AttributeError):
            pass
    cfg = home / ".config" / "mira"
    if (cfg / "mira.json").exists() and not (cfg / "speech.json").exists():
        return "listener"
    if (cfg / "speech.json").exists() and not (cfg / "mira.json").exists():
        return "speech"
    return "speech" if sys.platform == "darwin" else "listener"


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _plugin_cfg(doc: Mapping[str, Any], name: str) -> dict:
    body = (doc.get("plugins") or {}).get(name) or {}
    cfg = body.get("config") if isinstance(body, dict) else None
    return cfg if isinstance(cfg, dict) else {}


def resolve_target(ctx: Ctx) -> tuple[str, str, str]:
    """(base url, path prefix, owner token file) for online mode."""
    doc = read_json(ctx.cfg_path)
    url = ctx.opt("url") or ctx.env.get("MIRA_CONTROL_URL", "")
    token = ctx.opt("token_file") or ctx.env.get("MIRA_CONTROL_TOKEN_FILE", "")
    if ctx.machine == "speech":
        srv = _plugin_cfg(doc, "server.http")
        port = int(srv.get("port", 8765))
        url = url or f"http://127.0.0.1:{port}"
        prefix = "/ctl"
    else:
        ctl = _plugin_cfg(doc, "control.http")
        url = url or f"http://{ctl.get('bind', '127.0.0.1')}:{int(ctl.get('port', 8650))}"
        prefix = ""
    ctl = _plugin_cfg(doc, "control.http")
    token = token or str(ctl.get("owner_token_file") or "") or str(ctx.home / ".config" / "mira" / "control.owner.token")
    return url.rstrip("/"), prefix, os.path.expanduser(token.replace("${HOME}", str(ctx.home)))


class TokenMissing(CliError):
    """No usable owner token. Only the online path needs one, so rollback treats this like an unreachable process."""


class OnlineBackend:
    def __init__(self, base: str, prefix: str, token_file: str, timeout_s: float = 30.0) -> None:
        self.base, self.prefix, self.token_file, self.timeout_s = base, prefix, token_file, timeout_s

    def _token(self) -> str:
        try:
            token = Path(self.token_file).read_text().strip()
        except OSError:
            raise TokenMissing(EXIT_USAGE, f"the owner token file {self.token_file} is not readable; run "
                                           f"`mira tokens install`, or pass --offline to work on the settings file "
                                           f"directly") from None
        if not token:
            raise TokenMissing(EXIT_USAGE, f"the owner token file {self.token_file} is empty; run "
                                           f"`mira tokens install`, or pass --offline to work on the settings file "
                                           f"directly")
        return token

    def request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urlrequest.Request(self.base + self.prefix + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self._token())
        if data is not None:
            req.add_header("Content-Type", "application/json")
        timeout = self.timeout_s + float((body or {}).get("wait_s") or 0)
        try:
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urlerror.HTTPError as exc:
            try:
                parsed = json.loads(exc.read() or b"{}")
            except ValueError:
                parsed = {}
            raise ApiError(exc.code, parsed if isinstance(parsed, dict) else {}) from None
        except (urlerror.URLError, OSError, TimeoutError, ConnectionError) as exc:
            raise Unreachable(str(getattr(exc, "reason", exc))) from None
        return json.loads(raw) if raw else None


class OfflineBackend:
    """The control dispatcher on a Core opened from the file, owner scope, no server."""

    def __init__(self, core: Any) -> None:
        from mirakit import control
        import logging
        self.core = core
        self.api = control._Api(core, None, core.machine, logging.getLogger("mira.cli"))
        self._control = control

    def request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        body = dict(body or {})
        if method in ("PUT", "POST") and path.startswith(("/v1/config/", "/v1/undo", "/v1/confirm")):
            body.setdefault("wait_s", 0)               # nothing is running to apply it; the file is the result
        try:
            status, payload = self.api.dispatch(method, path, body, "owner")
        except Exception as exc:
            status, payload = self._control.error_response(exc, self.api.revision())
        if status >= 400:
            raise ApiError(status, payload if isinstance(payload, dict) else {})
        return payload

    def close(self) -> None:
        try:
            self.core.shutdown()
        except Exception:
            pass


def open_offline(ctx: Ctx) -> OfflineBackend:
    if ctx.offline_opener is not None:
        return ctx.offline_opener(ctx)
    try:
        from mirakit.core import Core
        from mirakit.docsgen import load_specs
    except ImportError as exc:
        raise CliError(EXIT_USAGE, f"offline mode needs the core module: {exc}") from None
    specs, _ = load_specs(ctx.root)
    specs = [s for s in specs if s.machine == ctx.machine]
    if not specs:
        raise CliError(EXIT_USAGE, f"no {ctx.machine} plugins are importable from {ctx.root}")
    parser = None
    if ctx.machine == "listener":
        try:
            import mira_listen
            parser = mira_listen.build_parser()
        except Exception as exc:
            raise CliError(EXIT_USAGE, f"cannot build the listener argument parser: {type(exc).__name__}") from None
    try:
        core = Core.from_file(ctx.cfg_path, argv=[], machine=ctx.machine, specs=specs, parser=parser,
                              mirror=(ctx.machine == "listener"))
    except SystemExit as exc:
        raise CliError(EXIT_USAGE, f"the settings file {ctx.cfg_path} is not usable (exit {exc.code})") from None
    return OfflineBackend(core)


def backend_of(ctx: Ctx, *, fallback_offline: bool = False) -> Any:
    """Online unless --offline. With fallback_offline (rollback) an unreachable process falls back to the file."""
    if ctx.backend is not None:
        return ctx.backend
    if ctx.opt("offline"):
        ctx.backend = open_offline(ctx)
        return ctx.backend
    base, prefix, token_file = resolve_target(ctx)
    online = OnlineBackend(base, prefix, token_file)
    if fallback_offline:
        try:
            online.request("GET", "/v1/health")
        except Unreachable:
            ctx.err(f"mira: no process answers at {base}; working on the file instead")
            ctx.backend = open_offline(ctx)
            return ctx.backend
        except TokenMissing:
            # the safety net must not depend on a token that may never have been installed
            ctx.err(f"mira: no usable owner token at {token_file}; working on the file instead (offline)")
            ctx.backend = open_offline(ctx)
            return ctx.backend
    ctx.backend = online
    return ctx.backend


def call(ctx: Ctx, method: str, path: str, body: Optional[dict] = None, **kw: Any) -> Any:
    backend = backend_of(ctx, **kw)
    try:
        return backend.request(method, path, body)
    except Unreachable as exc:
        raise CliError(EXIT_UNREACHABLE, f"nothing answers at {resolve_target(ctx)[0]} ({exc}); "
                                         f"pass --offline to work on the settings file directly") from None


# --------------------------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------------------------

def fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    if value is None:
        return "-"
    if isinstance(value, dict) and set(value) == {"set"}:
        return "(set)" if value["set"] else "(not set)"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[fmt(c) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    line = lambda row: "  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip()
    return "\n".join([line(headers)] + [line(r) for r in cells])


def emit(ctx: Ctx, payload: Any) -> None:
    ctx.out(json.dumps(payload, indent=1, ensure_ascii=False))


def age(ts: str, now: Optional[float] = None) -> str:
    try:
        from datetime import datetime
        then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except (ValueError, TypeError):
        return "?"
    secs = max(0, int((now if now is not None else time.time()) - then))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def change_exit(changes: Sequence[Mapping[str, Any]], offline: bool = False) -> int:
    codes = {STATUS_EXIT.get(str(c.get("status")), EXIT_OK) for c in changes}
    if offline:
        codes.discard(EXIT_QUEUED)
    for worst in SEVERITY:
        if worst in codes:
            return worst
    return EXIT_OK


def print_change(ctx: Ctx, change: Mapping[str, Any]) -> None:
    tag = f"{change.get('status')} ({change.get('id')})"
    edits = change.get("edits") or []
    if not edits:
        ctx.out(f"{tag} {change.get('announce', '')}".rstrip())
    for e in edits:
        ctx.out(f"{e['key']} {fmt(e.get('old'))} -> {fmt(e.get('new'))} {tag}")
    if change.get("error"):
        ctx.out(f"  error: {change['error']}")
    if change.get("trial_expires"):
        ctx.out(f"  trial until {change['trial_expires']}")


def report_api_error(ctx: Ctx, err: ApiError) -> int:
    body = err.body
    code = str(body.get("error", ""))
    ctx.err(f"mira: {body.get('message') or 'the request failed'} [{code or err.status}]")
    for label in ("field", "allowed", "owner_command", "near", "current"):
        if body.get(label) not in (None, "", [], {}):
            ctx.err(f"  {label}: {fmt(body[label])}")
    return EXIT_BY_CODE.get(code, EXIT_USAGE if err.status < 500 else EXIT_FAILED)


# --------------------------------------------------------------------------------------------
# small file helpers
# --------------------------------------------------------------------------------------------

def write_planned(ctx: Ctx) -> Path:
    """The planned-restart marker of 2.5.6: the launcher consumes it so a deliberate restart is not counted as a crash."""
    path = ctx.cfgdir / "state" / "planned"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{int(time.time())}\n")
    return path


def make_runner(ctx: Ctx, **kw: Any) -> Any:
    if ctx.runner_factory is not None:
        return ctx.runner_factory(**kw)
    from mirakit.deploy import Runner
    return Runner(**kw)


def split_key(key: str) -> tuple[str, str]:
    plugin, dot, name = key.rpartition(".")
    if not dot or not plugin or not name:
        raise CliError(EXIT_USAGE, f"{key!r} is not <plugin>.<key>, for example agc.mic.target")
    return plugin, name


# --------------------------------------------------------------------------------------------
# reading commands
# --------------------------------------------------------------------------------------------

def cmd_status(ctx: Ctx) -> int:
    st = call(ctx, "GET", "/v1/status")
    if ctx.json:
        emit(ctx, st)
        return EXIT_OK
    ctx.out(f"{st.get('machine')} {st.get('engine')} v{st.get('version')} revision {st.get('revision')} "
            f"up {fmt(st.get('uptime_s'))}s")
    rows = [("loop", st.get("loop")), ("turn", st.get("turn")), ("mic", st.get("mic")), ("failed", st.get("failed")),
            ("pending restart", st.get("pending_restart")), ("session", (st.get("session") or {}).get("id")),
            ("agents locked", st.get("agents_locked")), ("config error", st.get("config_error")),
            ("speech", st.get("speech"))]
    ctx.out(table(["field", "value"], [r for r in rows if r[1] not in (None, [], {}) or r[0] in ("agents locked",)]))
    return EXIT_OK


def cmd_inventory(ctx: Ctx) -> int:
    machine = ctx.opt("machine") or ""
    rows = call(ctx, "GET", "/v1/inventory" + (f"?machine={machine}" if machine else ""))
    if ctx.json:
        emit(ctx, rows)
        return EXIT_OK
    ctx.out(table(["plugin", "state", "on", "ver", "machine", "pending restart", "missing", "last error"],
                  [[r["name"], str(r["state"]).upper() if r["state"] != "active" else "active", r["enabled"],
                    r["version"], r["machine"], ",".join(r.get("pending_restart") or []) or "-",
                    ",".join(r.get("missing") or []) or "-", r.get("last_error") or "-"] for r in rows]))
    return EXIT_OK


def knob_policy(row: Mapping[str, Any]) -> str:
    if row.get("agent") != "bounded":
        return "deny"
    if row.get("agent_min") is None:
        return "voice"
    return f"{fmt(row['agent_min'])}..{fmt(row['agent_max'])} step {fmt(row.get('agent_step'))}"


def cmd_describe(ctx: Ctx) -> int:
    body = call(ctx, "GET", f"/v1/plugins/{ctx.args.plugin}")
    if ctx.json:
        emit(ctx, body)
        return EXIT_OK
    spec = body["spec"]
    ctx.out(f"{spec['name']} v{spec.get('version')}: {spec.get('summary')}")
    ctx.out(f"provides {','.join(spec.get('provides') or []) or '-'}; needs {','.join(spec.get('needs') or []) or '-'}")
    ctx.out(table(["key", "value", "default", "source", "bounds", "unit", "applies", "agent"],
                  [[k["key"], k["value"], k["default"], k.get("source"),
                    f"{fmt(k.get('min'))}..{fmt(k.get('max'))}" if k.get("min") is not None or k.get("max") is not None else "-",
                    k.get("unit") or "-", k["applies"], knob_policy(k)] for k in body["knobs"]]))
    return EXIT_OK


def flatten_config(doc: Mapping[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in (doc.get("core") or {}).items():
        flat[f"core.{key}"] = value
    for name, body in (doc.get("plugins") or {}).items():
        for key, value in (body.get("config") or {}).items():
            flat[f"{name}.{key}"] = value
    return flat


def cmd_get(ctx: Ctx) -> int:
    effective = bool(ctx.opt("effective"))
    doc = call(ctx, "GET", "/v1/config" + ("?effective=1" if effective else ""))
    if ctx.json:
        emit(ctx, doc)
        return EXIT_OK
    want = ctx.opt("target") or ""
    flat = flatten_config(doc)
    rows = []
    for key, value in flat.items():
        if want and key != want and not key.startswith(want + "."):
            continue
        if isinstance(value, dict) and set(value) == {"value", "source"}:
            rows.append((key, value["value"], value["source"]))
        else:
            rows.append((key, value, ""))
    if want and not rows:
        raise CliError(EXIT_UNKNOWN, f"nothing matches {want!r}")
    for key, value, source in rows:
        ctx.out(f"{key} = {fmt(value)}" + (f"  ({source})" if source else ""))
    return EXIT_OK


def cmd_changes(ctx: Ctx) -> int:
    q = [f"limit={ctx.opt('n', 20)}"]
    for name in ("session", "actor"):
        if ctx.opt(name):
            q.append(f"{name}={ctx.opt(name)}")
    if ctx.opt("machine"):
        q.append(f"machine={ctx.opt('machine')}")
    rows = call(ctx, "GET", "/v1/changes?" + "&".join(q))
    if ctx.json:
        emit(ctx, rows)
        return EXIT_OK
    for c in rows:
        what = c.get("announce") or "; ".join(f"{e['key']} {fmt(e['old'])} -> {fmt(e['new'])}" for e in c.get("edits", []))
        ctx.out(f"{c['id']}  {age(c['ts']):>4} ago  {c['actor']:<8} {c['status']:<15} {what}")
    return EXIT_OK


def cmd_session(ctx: Ctx) -> int:
    action = ctx.opt("action")
    if action:
        sess = call(ctx, "POST", "/v1/session", {"action": action, "topic": ctx.opt("topic") or ""})
    else:
        sess = call(ctx, "GET", "/v1/session")
    if ctx.json:
        emit(ctx, sess)
        return EXIT_OK
    if sess is None:
        ctx.out("no voice session is open")
        return EXIT_OK
    ctx.out(f"{sess['id']} {sess['state']} topic {sess.get('topic') or '-'} changes {','.join(sess.get('changes') or []) or '-'} "
            f"expires {sess.get('expires')}")
    return EXIT_OK


def cmd_schema(ctx: Ctx) -> int:
    doc = call(ctx, "GET", "/v1/schema")
    plugin = ctx.opt("plugin")
    if plugin:
        match = [p for p in doc.get("plugins", []) if p["name"] == plugin]
        if not match:
            raise CliError(EXIT_UNKNOWN, f"no plugin named {plugin}")
        if ctx.json:
            emit(ctx, match[0])
            return EXIT_OK
        ctx.out(f"{plugin}: {match[0].get('summary')}")
        ctx.out(table(["key", "type", "default", "bounds", "unit", "applies", "agent"],
                      [[k["name"], k["type"], k.get("default"), f"{fmt(k.get('min'))}..{fmt(k.get('max'))}"
                        if k.get("min") is not None or k.get("max") is not None else "-", k.get("unit") or "-",
                        k.get("applies"), knob_policy({"agent": k.get("agent"), "agent_min": k.get("agent_min"),
                                                       "agent_max": k.get("agent_max"), "agent_step": k.get("agent_step")})]
                       for k in match[0].get("knobs", [])]))
        return EXIT_OK
    if ctx.json:
        emit(ctx, doc)
        return EXIT_OK
    ctx.out(f"{doc.get('machine')} contract {doc.get('contract_version')}")
    ctx.out(table(["plugin", "kind", "version", "provides", "needs", "knobs", "summary"],
                  [[p["name"], p["kind"], p["version"], ",".join(p.get("provides") or []) or "-",
                    ",".join(p.get("needs") or []) or "-", len(p.get("knobs", [])), p.get("summary", "")]
                   for p in doc.get("plugins", [])]))
    return EXIT_OK


def cmd_tuning_guide(ctx: Ctx) -> int:
    guide = call(ctx, "GET", f"/v1/tuning_guide?topic={ctx.args.topic}")
    if ctx.json:
        emit(ctx, guide)
        return EXIT_OK
    ctx.out(f"topic {guide['topic']}")
    ctx.out(table(["key", "says", "now", "by voice", "steps", "applies"],
                  [[k["key"], (k.get("phrases") or [k.get("spoken")])[0], k["value"],
                    f"{fmt(k.get('agent_min'))}..{fmt(k.get('agent_max'))}", " ".join(fmt(v) for v in k.get("ladder") or []),
                    k.get("applies")] for k in guide.get("knobs", [])]))
    for o in guide.get("owner_only", []):
        ctx.out(f"owner only: {o['key']} ({o['spoken']}): {o['owner_command']}")
    return EXIT_OK


def cmd_voices(ctx: Ctx) -> int:
    action = ctx.opt("action")
    if action in ("index", "bench"):
        return cmd_voices_offline(ctx, action)
    q = []
    if ctx.opt("lang"):
        q.append(f"lang={ctx.opt('lang')}")
    if ctx.opt("all"):
        q += ["include_unusable=1", "limit=200"]
    body = call(ctx, "GET", "/v1/voices" + ("?" + "&".join(q) if q else ""))
    if ctx.json:
        emit(ctx, body)
        return EXIT_OK
    cur = body.get("current") or {}
    ctx.out(f"current: {cur.get('alias') or '-'} ({cur.get('id') or '-'})")
    ctx.out(table(["alias", "label", "lang", "gender", "engine", "usable", "flags", "rtf"],
                  [[v.get("alias"), v.get("label"), v.get("lang"), v.get("gender") or "-", v.get("engine"),
                    v.get("usable"), ",".join(f for f in ("current", "featured", "loaded") if v.get(f)) or "-",
                    v.get("rtf")] for v in body.get("voices", [])]))
    for fam in body.get("families", []):
        ctx.out(f"family {fam['alias_prefix']}: {fam['speakers']} speakers in {fam['model_dir']} (use {fam['alias_prefix']}:<name>)")
    return EXIT_OK


def cmd_voices_offline(ctx: Ctx, action: str) -> int:
    if ctx.machine != "speech":
        raise CliError(EXIT_USAGE, f"`mira voices {action}` runs on the speech machine")
    try:
        from speech_plugins import voices_catalog
    except ImportError as exc:
        raise CliError(EXIT_USAGE, f"speech plugins are not importable: {exc}") from None
    doc = read_json(ctx.cfg_path)
    srv = _plugin_cfg(doc, "server.http")
    base = Path(os.path.expanduser(str(srv.get("base_dir") or "~/Library/Application Support/mira-speech")))
    models = base / "models"
    if action == "index":
        out_dir = ctx.root / "speech_plugins" / "voices"
        result = voices_catalog.index_main(models, out_dir)
        for name, count in sorted(result.items()):
            ctx.out(f"{name}: {count} voices -> {out_dir / (name + '.json')}")
        return EXIT_OK
    cat = _plugin_cfg(doc, "voices.catalog")
    rtf_file = Path(os.path.expanduser(str(cat.get("rtf_file") or ctx.home / ".config" / "mira" / "voices.rtf.json")))
    threads = int(_plugin_cfg(doc, "tts.engine").get("threads", 4))
    results = voices_catalog.bench_main(models, rtf_file, threads=threads, only=ctx.opt("voice") or "", log=ctx.out)
    ctx.out(f"measured {len(results)} voices into {rtf_file}")
    return EXIT_OK


# --------------------------------------------------------------------------------------------
# writing commands
# --------------------------------------------------------------------------------------------

def write_body(ctx: Ctx) -> dict:
    body: dict[str, Any] = {"reason": ctx.opt("reason") or ""}
    if ctx.opt("trial") is not None:
        body["trial_s"] = ctx.opt("trial")
    if ctx.opt("expect_rev") is not None:
        body["expect_revision"] = ctx.opt("expect_rev")
    if ctx.opt("dry_run"):
        body["dry_run"] = True
    if ctx.opt("wait") is not None:
        body["wait_s"] = ctx.opt("wait")
    return body


def submit_edits(ctx: Ctx, edits: list[tuple[str, Any]]) -> int:
    body = write_body(ctx)
    if len(edits) == 1:
        plugin, name = split_key(edits[0][0])
        body["value"] = edits[0][1]
        change = call(ctx, "PUT", f"/v1/config/{plugin}/{name}", body)
    else:
        body["edits"] = [{"key": k, "value": v} for k, v in edits]
        change = call(ctx, "POST", "/v1/config/batch", body)
    if ctx.json:
        emit(ctx, change)
    else:
        print_change(ctx, change)
    return change_exit([change], offline=isinstance(ctx.backend, OfflineBackend))


def cmd_set(ctx: Ctx) -> int:
    edits = []
    for item in ctx.args.assignments:
        key, eq, value = item.partition("=")
        if not eq:
            raise CliError(EXIT_USAGE, f"{item!r} is not <plugin.key>=<value>")
        split_key(key)
        edits.append((key, value))
    return submit_edits(ctx, edits)


def cmd_reset(ctx: Ctx) -> int:
    edits = []
    cache: dict[str, Any] = {}
    for key in ctx.args.keys:
        plugin, name = split_key(key)
        if plugin not in cache:
            cache[plugin] = call(ctx, "GET", f"/v1/plugins/{plugin}")
        row = next((k for k in cache[plugin]["knobs"] if k["key"] == name), None)
        if row is None:
            raise CliError(EXIT_UNKNOWN, f"{plugin} has no key {name}")
        if isinstance(row["default"], dict) and set(row["default"]) == {"set"}:
            raise CliError(EXIT_USAGE, f"{key} is a secret path; set it to a path instead of resetting it")
        edits.append((key, row["default"]))
    return submit_edits(ctx, edits)


def cmd_undo_confirm(ctx: Ctx, verb: str) -> int:
    body: dict[str, Any] = {}
    if ctx.opt("change_id"):
        body["change_id"] = ctx.args.change_id
    if ctx.opt("session"):
        body["session"] = ctx.args.session
    if verb == "undo" and ctx.opt("force"):
        body["force"] = True
    changes = call(ctx, "POST", f"/v1/{verb}", body)
    if ctx.json:
        emit(ctx, changes)
    elif not changes:
        ctx.out(f"nothing to {verb}")
    else:
        for c in changes:
            print_change(ctx, c)
    return change_exit(changes or [], offline=isinstance(ctx.backend, OfflineBackend))


def cmd_toggle(ctx: Ctx, verb: str) -> int:
    change = call(ctx, "POST", f"/v1/plugins/{ctx.args.plugin}/{verb}", {})
    if ctx.json:
        emit(ctx, change)
    else:
        print_change(ctx, change)
    return change_exit([change], offline=isinstance(ctx.backend, OfflineBackend))


def cmd_agents(ctx: Ctx) -> int:
    return submit_edits(ctx, [("control.http.agents_locked", ctx.args.state == "lock")])


def cmd_preview(ctx: Ctx) -> int:
    body: dict[str, Any] = {}
    for name in ("voice", "text", "speed", "peak"):
        if ctx.opt(name) is not None:
            body[name] = ctx.opt(name)
    out = call(ctx, "POST", "/v1/preview", body)
    if ctx.json:
        emit(ctx, out)
    else:
        ctx.out(("played" if out.get("played") else "queued") + f" voice {out.get('voice', '-')}"
                + (f" in {out['ms']} ms" if out.get("ms") is not None else ""))
    return EXIT_OK


def run_service_restart(ctx: Ctx) -> int:
    runner = make_runner(ctx)
    if ctx.machine == "listener":
        res = runner.sh('XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" systemctl --user restart "$1"',
                        MACHINE_UNITS["listener"])
    else:
        res = runner.sh('launchctl kickstart -k "gui/$(id -u)/$1"', MACHINE_UNITS["speech"])
    if not res.ok:
        raise CliError(EXIT_FAILED, f"the restart command failed: {res.err.strip()[:200]}")
    return EXIT_OK


def cmd_restart(ctx: Ctx) -> int:
    write_planned(ctx)
    if ctx.machine == "listener" and not ctx.opt("offline"):
        try:
            base, prefix, token_file = resolve_target(ctx)
            out = OnlineBackend(base, prefix, token_file).request("POST", "/v1/restart", {})
            ctx.out(f"restart requested at {out.get('at')}; the unit starts again after the next safe point")
            return EXIT_OK
        except Unreachable:
            ctx.err("mira: the listener does not answer; restarting the unit directly")
        except ApiError as err:
            return report_api_error(ctx, err)
    code = run_service_restart(ctx)
    ctx.out("restarted " + MACHINE_UNITS[ctx.machine])
    return code


def cmd_rollback(ctx: Ctx) -> int:
    """Engine back to legacy. Works on the file when the process is down (that is when it is needed most)."""
    backend = backend_of(ctx, fallback_offline=True)
    lost: list[str] = []
    try:
        schema = backend.request("GET", "/v1/schema")
        config = backend.request("GET", "/v1/config?effective=1")
        values = flatten_config(config)
        for plugin in schema.get("plugins", []):
            for knob in plugin.get("knobs", []):
                key = f"{plugin['name']}.{knob['name']}"
                value = values.get(key)
                value = value.get("value") if isinstance(value, dict) and "value" in value else value
                if knob.get("secret") or key not in values:
                    continue
                if ctx.machine == "listener":
                    from mirakit import envmap
                    known = envmap.v1_flags()
                    flag = "--" + str(knob.get("arg") or "").replace("_", "-")
                    representable = bool(knob.get("env")) or (bool(knob.get("arg")) and (known is None or flag in known))
                else:
                    representable = key not in ("tts.engine.voice", "tts.engine.speed", "tts.engine.peak")
                if not representable and value != knob.get("default"):
                    lost.append(f"{key} = {fmt(value)} (legacy default {fmt(knob.get('default'))})")
    except ApiError:
        pass
    write_planned(ctx)
    try:
        change = backend.request("PUT", "/v1/config/core/engine", {"value": "legacy", "reason": "mira rollback", "wait_s": 0})
    except ApiError as err:
        return report_api_error(ctx, err)
    if ctx.json:
        emit(ctx, {"change": change, "legacy_cannot_represent": sorted(lost)})
    else:
        print_change(ctx, change)
        if lost:
            ctx.out("legacy cannot represent these tuned values (they stay in the file for when you switch back):")
            for line in sorted(lost):
                ctx.out(f"  {line}")
    dropped = sanitize_legacy_args(ctx)
    if dropped and not ctx.json:
        ctx.err("mira: dropped from legacy.args (the v1 listener has no such flag): " + " ".join(dropped))
    code = run_service_restart(ctx)
    if not ctx.json:
        ctx.out("restarted " + MACHINE_UNITS[ctx.machine] + " on the legacy engine")
    return code


# --------------------------------------------------------------------------------------------
# config subcommands
# --------------------------------------------------------------------------------------------

def full_config_doc(ctx: Ctx, merge: bool) -> dict:
    cur = call(ctx, "GET", "/v1/config")
    doc: dict[str, Any] = {"format": 1, "machine": ctx.machine, "revision": cur.get("revision", 0),
                           "core": cur.get("core", {}), "plugins": cur.get("plugins", {})}
    if merge:
        existing = read_json(ctx.cfg_path)
        head = {k: v for k, v in existing.items() if k.startswith("$")}
        doc = {**head, **doc}
    return doc


def cmd_config(ctx: Ctx) -> int:
    action = ctx.args.config_action
    if action == "validate":
        return config_validate(ctx)
    if action == "render":
        if not ctx.opt("full"):
            raise CliError(EXIT_USAGE, "render needs --full")
        ctx.out(json.dumps(full_config_doc(ctx, bool(ctx.opt("merge"))), indent=1, ensure_ascii=False))
        return EXIT_OK
    if action == "reload":
        change = call(ctx, "POST", "/v1/config/reload", {})
        if ctx.json:
            emit(ctx, change)
        else:
            print_change(ctx, change)
        return change_exit([change], offline=isinstance(ctx.backend, OfflineBackend))
    if action == "diff":
        return config_diff(ctx)
    return config_edit(ctx)


def config_validate(ctx: Ctx) -> int:
    path = Path(ctx.opt("file") or ctx.cfg_path)
    try:
        data = json.loads(path.read_text())
    except OSError as exc:
        raise CliError(EXIT_USAGE, f"cannot read {path}: {exc.strerror}") from None
    except ValueError as exc:
        raise CliError(EXIT_USAGE, f"{path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != 1 or data.get("machine") not in ("listener", "speech"):
        raise CliError(EXIT_USAGE, f'{path} needs "format": 1 and "machine": "listener" or "speech"')
    machine = data["machine"]
    try:
        from mirakit.core import Core
        from mirakit.docsgen import load_specs
    except ImportError as exc:
        raise CliError(EXIT_USAGE, f"validation needs the core module: {exc}") from None
    specs = [s for s in load_specs(ctx.root)[0] if s.machine == machine]
    with tempfile.TemporaryDirectory(prefix="mira-validate-") as tmp:
        copy = Path(tmp) / ("mira.json" if machine == "listener" else "speech.json")
        copy.write_text(json.dumps(data))
        os.chmod(copy, 0o600)
        try:
            core = Core.from_file(copy, argv=[], machine=machine, specs=specs, parser=None, mirror=False, changelog=False)
        except SystemExit:
            raise CliError(EXIT_USAGE, f"{path} is not valid (the core refused it; run with the journal for the reason)") from None
        except Exception as exc:
            raise CliError(EXIT_USAGE, f"{path} is not valid: {getattr(exc, 'message', None) or type(exc).__name__}") from None
        try:
            if getattr(core, "status", None) and core.status().get("config_error"):
                raise CliError(EXIT_USAGE, f"{path} is not valid: {core.status()['config_error']}")
        finally:
            core.shutdown()
    ctx.out(f"ok: {path} is valid for the {machine}")
    return EXIT_OK


def config_diff(ctx: Ctx) -> int:
    hist = ctx.cfgdir / "history"
    rev = ctx.opt("rev")
    names = sorted(hist.glob(f"{ctx.machine}.*.json"), key=lambda p: int(re.findall(r"\.(\d+)\.json$", p.name)[0])
                   if re.findall(r"\.(\d+)\.json$", p.name) else -1) if hist.is_dir() else []
    if rev is not None:
        names = [p for p in names if p.name == f"{ctx.machine}.{rev}.json"]
    if not names:
        raise CliError(EXIT_USAGE, "no history file to compare with")
    old = names[-1]
    a = old.read_text().splitlines(keepends=True)
    b = ctx.cfg_path.read_text().splitlines(keepends=True) if ctx.cfg_path.exists() else []
    text = "".join(difflib.unified_diff(a, b, old.name, ctx.cfg_path.name))
    ctx.out(text.rstrip("\n") if text else f"no difference from {old.name}")
    return EXIT_OK


def config_edit(ctx: Ctx) -> int:
    cur = call(ctx, "GET", "/v1/config")
    doc = {"format": 1, "machine": ctx.machine, "revision": cur.get("revision", 0), "core": cur.get("core", {}),
           "plugins": cur.get("plugins", {})}
    editor = ctx.env.get("EDITOR") or ctx.env.get("VISUAL") or "vi"
    with tempfile.TemporaryDirectory(prefix="mira-edit-") as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
        os.chmod(path, 0o600)
        rc = ctx.editor([editor, str(path)]) if ctx.editor else __import__("subprocess").call([*shlex.split(editor), str(path)])
        if rc != 0:
            raise CliError(EXIT_USAGE, f"the editor exited {rc}; nothing changed")
        try:
            new = json.loads(path.read_text())
        except ValueError as exc:
            raise CliError(EXIT_USAGE, f"the edited file is not valid JSON: {exc}; nothing changed") from None
    if not isinstance(new, dict):
        raise CliError(EXIT_USAGE, "the edited file must be a JSON object; nothing changed")
    old_flat, new_flat = flatten_config(doc), flatten_config(new)
    edits = [{"key": k, "value": v} for k, v in new_flat.items() if k not in old_flat or old_flat[k] != v]
    for key in set(old_flat) - set(new_flat):
        ctx.out(f"note: {key} was removed from the text; removing a key does not reset it (use mira reset)")
    toggles = [(p, b["enabled"]) for p, b in (new.get("plugins") or {}).items()
               if isinstance(b, dict) and isinstance(b.get("enabled"), bool)
               and (cur.get("plugins", {}).get(p) or {}).get("enabled") != b["enabled"]]
    if not edits and not toggles:
        ctx.out("no changes")
        return EXIT_OK
    codes = []
    if edits:
        body = {"edits": edits, "reason": "mira config edit", "dry_run": True}
        call(ctx, "POST", "/v1/config/batch", body)                 # validates the whole batch before anything moves
        body["dry_run"] = False
        change = call(ctx, "POST", "/v1/config/batch", body)
        print_change(ctx, change)
        codes.append(change)
    for plugin, enabled in toggles:
        change = call(ctx, "POST", f"/v1/plugins/{plugin}/{'enable' if enabled else 'disable'}", {})
        print_change(ctx, change)
        codes.append(change)
    return change_exit(codes, offline=isinstance(ctx.backend, OfflineBackend))


# --------------------------------------------------------------------------------------------
# adopt, unit, deploy, tokens
# --------------------------------------------------------------------------------------------

def read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key] = value
    return env


def execstart_script(unit_text: str, home: Path) -> Optional[Path]:
    """The script the unit's ExecStart runs (the listener, or the launcher once the drop-in is in place)."""
    joined = re.sub(r"\\\n\s*", " ", unit_text)
    for line in joined.splitlines():
        if line.startswith("ExecStart=") and line != "ExecStart=":
            for t in shlex.split(line[len("ExecStart="):].replace("%h", str(home))):
                if t.endswith(".py"):
                    return Path(t)
    return None


def live_install_dir(unit_text: str, home: Path, root: Path) -> Path:
    """The install root that holds the data files (persona, chime, models) beside the code: the folder of the
    listener script the unit runs, minus a `current` or `releases/<id>` step when it is a release. Without a unit
    it is the folder this code runs from, treated the same way."""
    script = execstart_script(unit_text, home) if unit_text else None
    base = script.parent if script is not None else Path(root)
    if base.name == "current" or base.parent.name == "releases":
        base = base.parent if base.name == "current" else base.parent.parent
    return base


def execstart_argv(unit_text: str, home: Path) -> list[str]:
    """Arguments after the script on the unit's ExecStart (line continuations joined, %h expanded)."""
    joined = re.sub(r"\\\n\s*", " ", unit_text)
    for line in joined.splitlines():
        if line.startswith("ExecStart=") and line != "ExecStart=":
            tokens = shlex.split(line[len("ExecStart="):].replace("%h", str(home)))
            for i, t in enumerate(tokens):
                if t.endswith(".py"):
                    return tokens[i + 1:]
            return []
    return []


def sanitize_legacy_args(ctx: Ctx) -> list[str]:
    """Drop tokens from legacy.args that the frozen v1 listener parser does not define. `mira rollback` falls back
    to that file, and argparse exits 2 on an unknown flag, which would leave the wall without a listener. Returns the
    dropped flags."""
    path = ctx.cfgdir / "legacy.args"
    if ctx.machine != "listener" or not path.exists():
        return []
    try:
        import mira_listen_v1
        known = set(mira_listen_v1.build_parser()._option_string_actions)
    except Exception:
        return []
    tokens = path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    dropped: list[str] = []
    skipping = False
    for tok in tokens:
        if tok.startswith("--"):
            skipping = tok not in known
            if skipping:
                dropped.append(tok)
                continue
        elif skipping:
            continue
        kept.append(tok)
    if dropped:
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept) + ("\n" if kept else ""))
        os.replace(tmp, path)
    return dropped


def cmd_adopt(ctx: Ctx) -> int:
    try:
        from mirakit import envmap
        from mirakit.docsgen import load_specs
    except ImportError as exc:
        raise CliError(EXIT_USAGE, f"adopt needs the core modules: {exc}") from None
    specs = [s for s in load_specs(ctx.root)[0] if s.machine == ctx.machine]
    layers: dict[str, str] = {}
    if ctx.machine == "listener":
        parser = None
        try:
            import mira_listen
            parser = mira_listen.build_parser()
        except Exception as exc:
            raise CliError(EXIT_USAGE, f"cannot build the listener argument parser: {type(exc).__name__}") from None
        unit = ctx.home / ".config" / "systemd" / "user" / "mira-listen.service"
        argv = execstart_argv(unit.read_text(), ctx.home) if unit.exists() else []
        env = read_env_file(ctx.home / ".config" / "mira" / "listen.env")
        doc = envmap.adopt_listener(argv, env, specs, parser)
        install = live_install_dir(unit.read_text() if unit.exists() else "", ctx.home, ctx.root)
        for key, _old, new in envmap.freeze_here_paths(doc, specs, install):
            layers[key] = "install"
        for key in envmap.unfrozen_here_paths(doc, specs):
            ctx.err(f"mira: {key} still points into the release (${{HERE}}); no such file beside {install}")
        explicit = envmap.argv_layer(envmap.explicit_from_argv(parser, argv), specs) if argv else {}
        for key in envmap.env_layer(env, specs):
            layers[key] = "env"
        for key in explicit:
            layers[key] = "flag"
    else:
        plist = Path(os.path.expanduser(ctx.opt("from_plist") or "~/Library/LaunchAgents/org.aryavora.mira-speech.plist"))
        if not plist.exists():
            raise CliError(EXIT_USAGE, f"no LaunchAgent plist at {plist}; pass --from-plist")
        doc = envmap.adopt_speech(plist, specs)
        import plistlib
        with open(plist, "rb") as fh:
            env_keys = {str(k) for k in (plistlib.load(fh).get("EnvironmentVariables") or {})}
        for spec in specs:
            for k in spec.knobs:
                if k.env and k.env in env_keys:
                    layers[f"{spec.name}.{k.name}"] = "plist"
    current = flatten_config(read_json(ctx.cfg_path)) if ctx.cfg_path.exists() else {}
    flat = flatten_config(doc)
    defaults = {f"{s.name}.{k.name}": k.default for s in specs for k in s.knobs}
    rows = []
    for key, value in flat.items():
        interesting = key in layers or (key in defaults and value != defaults[key]) or (key in current and current[key] != value)
        if interesting or ctx.opt("all"):
            rows.append([key, value, current.get(key, "(no file)") if ctx.cfg_path.exists() else "(no file)",
                         layers.get(key, "code")])
    if ctx.json:
        emit(ctx, {"doc": doc, "rows": [dict(zip(("key", "legacy", "file", "source"), r)) for r in rows]})
    else:
        ctx.out(table(["key", "legacy effective", "file value", "source"], rows))
    if not ctx.opt("write"):
        ctx.out(f"dry run: {len(rows)} values shown; pass --write to freeze them into {ctx.cfg_path}")
        return EXIT_OK
    if ctx.cfg_path.exists():
        # adopt rebuilds the file from the legacy layers, so on a live file it would reset engine and tuned values
        if not ctx.opt("force"):
            raise CliError(EXIT_USAGE, f"{ctx.cfg_path} already exists; adopt would overwrite it. Pass --force to "
                                       "replace it (the old file is copied to history/ first)")
        try:
            old_rev = int(read_json(ctx.cfg_path).get("revision", 0))
        except (CliError, ValueError, TypeError, OSError):
            old_rev = 0
        hist = ctx.cfg_path.parent / "history"
        hist.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ctx.cfg_path, hist / f"{ctx.machine}.{old_rev}.json")
        doc["revision"] = old_rev + 1
    ctx.cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = ctx.cfg_path.with_name(ctx.cfg_path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, ctx.cfg_path)
    ctx.out(f"wrote {ctx.cfg_path} (core.engine stays {doc['core'].get('engine')}, core.legacy_env is ignore)")
    if ctx.machine == "listener":
        try:
            from mirakit.core import Core
            core = Core.from_file(ctx.cfg_path, argv=[], machine="listener", specs=specs, parser=None, mirror=True)
            try:
                writer = getattr(core, "write_mirror", None) or getattr(core, "_write_mirror", None)
                if writer:
                    writer()
                    sanitize_legacy_args(ctx)
                    ctx.out(f"wrote the legacy mirror next to it (listen.env, legacy.args)")
            finally:
                core.shutdown()
        except (ImportError, SystemExit) as exc:
            ctx.err(f"mira: the legacy mirror was not written ({type(exc).__name__}); run `mira config reload`")
    return EXIT_OK


def cmd_unit(ctx: Ctx) -> int:
    from mirakit import unitgen
    values = unitgen.values_from_file(ctx.cfg_path, ctx.machine)
    action = ctx.args.unit_action
    if action == "apply":
        try:
            lines = unitgen.apply(ctx.machine, values, ctx.home, make_runner(ctx), release=ctx.opt("release"))
        except ValueError as exc:
            raise CliError(EXIT_USAGE, str(exc)) from None
        for line in lines:
            ctx.out(line)
        return EXIT_OK
    if ctx.opt("check"):
        ok, diff = unitgen.check(ctx.machine, values, ctx.home)
        ctx.out("the installed unit matches the settings" if ok else diff.rstrip("\n"))
        return EXIT_OK if ok else EXIT_USAGE
    dest, data = unitgen.render(ctx.machine, values, ctx.home)
    ctx.out(f"# would install {dest}")
    ctx.out(data.decode("utf-8", "replace").rstrip("\n"))
    return EXIT_OK


def deployer_for(ctx: Ctx, machine: str, dry: bool) -> Any:
    from mirakit import deploy
    manifest = deploy.load_manifest(ctx.root)
    target = ctx.env.get("MIRA_DEPLOY_PI_SSH" if machine == "pi" else "MIRA_DEPLOY_SPEECH_SSH") or manifest[machine]["ssh"]
    runner = make_runner(ctx, target=target, dry_run=dry)
    extra = {"local": ctx.local_runner} if ctx.local_runner is not None else {}
    return deploy.Deployer(machine, ctx.root, runner, out=ctx.out, **extra), runner


def cmd_deploy(ctx: Ctx) -> int:
    from mirakit.deploy import DeployError
    machine = ctx.args.target
    dry = bool(ctx.opt("dry_run"))
    allow = bool(ctx.opt("allow_dirty"))
    try:
        dep, runner = deployer_for(ctx, machine, dry)
        if machine == "pi":
            if ctx.opt("rollback") is not None:
                dep.rollback_pi(ctx.opt("rollback") or None)
            elif ctx.opt("activate"):
                dep.activate_pi(ctx.args.activate)
            elif ctx.opt("stage") or dry:
                if dry:
                    for line in dep.dry_run_pi(allow):
                        ctx.out(line)
                name = dep.stage_pi(allow, run_check=not dry)
                if dry:
                    dep.activate_pi(name)
            else:
                raise CliError(EXIT_USAGE, "choose one of --stage, --activate SHA, --rollback [SHA] or --dry-run")
        else:
            if ctx.opt("rollback") is not None:
                dep.rollback_speech(ctx.opt("rollback") or None)
            elif dry:
                for line in dep.dry_run_speech(allow):
                    ctx.out(line)
                dep.deploy_speech(allow)
            else:
                dep.deploy_speech(allow)
        if dry:
            ctx.out("planned commands:")
            for line in runner.log:
                if line.startswith("+"):
                    ctx.out("  " + line)
    except DeployError as exc:
        raise CliError(EXIT_FAILED, str(exc)) from None
    return EXIT_OK


def cmd_tokens(ctx: Ctx) -> int:
    from mirakit import tokens
    machine = ctx.opt("machine") or ctx.machine
    reports = tokens.install(machine, ctx.home, make_runner(ctx))
    if ctx.json:
        emit(ctx, [r.__dict__ for r in reports])
    else:
        ctx.out(table(["token", "path", "state", "mode", "note"], [[r.name, r.path, r.state, r.mode or "-", r.detail] for r in reports]))
    return EXIT_FAILED if any(r.state == "failed" for r in reports) else EXIT_OK


# --------------------------------------------------------------------------------------------
# doctor, check, docs, plugin
# --------------------------------------------------------------------------------------------

def check_row(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status, "detail": detail}


def doctor_checks(ctx: Ctx, port_probe: Optional[Callable[[str, int], bool]] = None) -> list[dict]:
    rows: list[dict] = []
    doc = read_json(ctx.cfg_path)
    cfgdir = ctx.cfgdir
    if not ctx.cfg_path.exists():
        rows.append(check_row("config", "fail", f"{ctx.cfg_path} does not exist"))
    elif not doc:
        rows.append(check_row("config", "fail", f"{ctx.cfg_path} is not a JSON object"))
    else:
        mode = ctx.cfg_path.stat().st_mode & 0o777
        rows.append(check_row("config", "ok" if mode & 0o077 == 0 else "warn",
                              f"revision {doc.get('revision')}, engine {(doc.get('core') or {}).get('engine', 'legacy')}, "
                              f"mode {mode:04o}"))
    core = doc.get("core") or {}
    # unit versus config
    try:
        from mirakit import unitgen
        values = unitgen.values_from_file(ctx.cfg_path, ctx.machine)
        ok, diff = unitgen.check(ctx.machine, values, ctx.home)
        rows.append(check_row("unit", "ok" if ok else "warn",
                              "installed unit matches core.unit_*" if ok else "installed unit differs from core.unit_* (mira unit render --check)"))
    except Exception as exc:
        rows.append(check_row("unit", "warn", f"could not compare: {type(exc).__name__}"))
    # argv overrides
    if ctx.machine == "listener":
        found: set[str] = set()
        for unit in (ctx.home / ".config" / "systemd" / "user" / "mira-listen.service",
                     ctx.home / ".config" / "systemd" / "user" / "mira-listen.service.d" / "mira.conf"):
            if unit.exists():
                found |= {t for t in execstart_argv(unit.read_text(), ctx.home) if t.startswith("--")} - PATH_FLAGS
        rows.append(check_row("argv overrides", "warn" if found and core.get("engine") == "plugins" else "ok",
                              ("flags on ExecStart beat the file: " + ", ".join(sorted(found))) if found else "none"))
    else:
        plist = ctx.home / "Library" / "LaunchAgents" / "org.aryavora.mira-speech.plist"
        extra: list[str] = []
        if plist.exists():
            import plistlib
            with open(plist, "rb") as fh:
                args = plistlib.load(fh).get("ProgramArguments") or []
            extra = [str(a) for a in args[2:]]
        rows.append(check_row("argv overrides", "warn" if extra else "ok", ", ".join(extra) if extra else "none"))
    # env set but ignored
    legacy_env = str(core.get("legacy_env", "fallback"))
    names: list[str] = []
    if ctx.machine == "listener":
        env_file = read_env_file(cfgdir / "listen.env")
        gen = (cfgdir / "listen.env").exists() and (cfgdir / "listen.env").read_text().startswith("# generated by mirakit")
        names = [k for k in env_file if k.startswith("MIRA_")] if not gen else []
    else:
        plist = ctx.home / "Library" / "LaunchAgents" / "org.aryavora.mira-speech.plist"
        if plist.exists():
            import plistlib
            with open(plist, "rb") as fh:
                names = [k for k in (plistlib.load(fh).get("EnvironmentVariables") or {}) if k.startswith("MIRA_")]
    rows.append(check_row("env ignored", "warn" if names and legacy_env == "ignore" else "ok",
                          ("set but ignored while core.legacy_env is ignore: " + ", ".join(sorted(names))) if names and legacy_env == "ignore"
                          else f"core.legacy_env is {legacy_env}"))
    # token modes
    ctl = _plugin_cfg(doc, "control.http")
    token_paths = [("owner", ctl.get("owner_token_file") or str(cfgdir / "control.owner.token"))]
    token_paths.append(("pi" if ctx.machine == "speech" else "agent",
                        (ctl.get("pi_token_file") if ctx.machine == "speech" else ctl.get("agent_token_file"))
                        or (str(cfgdir / "control.pi.token") if ctx.machine == "speech" else "/etc/mira/control.agent.token")))
    bad: list[str] = []
    missing: list[str] = []
    for label, p in token_paths:
        path = Path(os.path.expanduser(str(p).replace("${HOME}", str(ctx.home))))
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            missing.append(label)
            continue
        if mode & 0o007:
            bad.append(f"{label} {mode:04o}")
        elif label != "agent" and mode & 0o077:
            bad.append(f"{label} {mode:04o}")
    rows.append(check_row("token modes", "fail" if bad else ("warn" if missing else "ok"),
                          "too open: " + ", ".join(bad) if bad else ("missing: " + ", ".join(missing) if missing else "files present with safe modes")))
    # binds and port
    if ctx.machine == "listener":
        bind, port = str(ctl.get("bind", "127.0.0.1")), int(ctl.get("port", 8650))
        enabled = bool((doc.get("plugins", {}).get("control.http") or {}).get("enabled", False))
        binds_ok = bind.startswith("127.") or bind == "::1"
        rows.append(check_row("binds", "ok" if binds_ok else "fail", f"control.http binds {bind}" + ("" if binds_ok else " (loopback only)")))
    else:
        srv = _plugin_cfg(doc, "server.http")
        binds = srv.get("bind", ["127.0.0.1"])
        binds = binds if isinstance(binds, list) else [binds]
        bad_binds = [b for b in binds if b in ("0.0.0.0", "", "::")]
        port, enabled = int(srv.get("port", 8765)), True
        rows.append(check_row("binds", "fail" if bad_binds else "ok", ("wildcard bind: " + ", ".join(bad_binds)) if bad_binds else ", ".join(map(str, binds))))
    probe = port_probe or _port_in_use
    in_use = probe("127.0.0.1", port)
    if ctx.machine == "listener" and not enabled:
        rows.append(check_row("port", "ok", f"control.http is disabled; port {port} {'is in use' if in_use else 'is free'}"))
    else:
        rows.append(check_row("port", "ok" if in_use else "warn", f"port {port} {'is answering' if in_use else 'is free: the process is not serving it'}"))
    # pins
    try:
        from importlib import metadata
        version = metadata.version("sherpa-onnx")
        rows.append(check_row("pins", "ok" if version == "1.12.40" else "fail", f"sherpa-onnx {version} (pinned 1.12.40)"))
    except Exception:
        rows.append(check_row("pins", "warn", "sherpa-onnx is not installed in this interpreter"))
    # last good
    lastgood = ctx.cfg_path.with_name(ctx.cfg_path.name + ".lastgood")
    if lastgood.exists():
        days = (time.time() - lastgood.stat().st_mtime) / 86400
        rows.append(check_row("last good", "ok", f"{lastgood.name} is {days:.1f} days old"))
    else:
        rows.append(check_row("last good", "warn", f"{lastgood.name} does not exist yet (written after the first clean boot)"))
    return rows


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def cmd_doctor(ctx: Ctx) -> int:
    rows = doctor_checks(ctx)
    if ctx.json:
        emit(ctx, rows)
    else:
        ctx.out(table(["check", "status", "detail"], [[r["check"], r["status"], r["detail"]] for r in rows]))
    return EXIT_USAGE if any(r["status"] == "fail" for r in rows) else EXIT_OK


def cmd_check(ctx: Ctx) -> int:
    from mirakit import lint
    from mirakit.docsgen import load_specs
    specs, warnings = load_specs(ctx.root)
    if not specs:
        for w in warnings:
            ctx.err(f"mira: {w}")
        raise CliError(EXIT_USAGE, "no plugin specs could be loaded, so nothing was checked")
    findings = lint.lint(specs, ctx.root, fast=bool(ctx.opt("fast")))
    for f in findings:
        ctx.out(str(f))
    if findings:
        ctx.err(f"mira check: {len(findings)} finding(s)")
        return EXIT_USAGE
    ctx.out(f"mira check: ok ({len(specs)} plugins{', fast' if ctx.opt('fast') else ''})")
    return EXIT_OK


def cmd_docs(ctx: Ctx) -> int:
    from mirakit import docsgen
    specs, warnings = docsgen.load_specs(ctx.root)
    if not specs:
        raise CliError(EXIT_USAGE, "no plugin specs could be loaded: " + "; ".join(warnings))
    skill = Path(ctx.opt("skill")) if ctx.opt("skill") else None
    vault = Path(ctx.opt("vault")) if ctx.opt("vault") else None
    for path in docsgen.write_all(ctx.root, specs, skill, vault):
        try:
            ctx.out(f"wrote {path.relative_to(ctx.root)}")
        except ValueError:
            ctx.out(f"wrote {path}")
    return EXIT_OK


def cmd_plugin(ctx: Ctx) -> int:
    from mirakit import scaffold
    try:
        written, line = scaffold.new_plugin(ctx.root, ctx.args.kind, ctx.args.variant, ctx.opt("machine") or "listener")
    except scaffold.ScaffoldError as exc:
        raise CliError(EXIT_USAGE, str(exc)) from None
    for path in written:
        ctx.out(f"created {path.relative_to(ctx.root)}")
    ctx.out("add this line to specs():")
    ctx.out(line)
    return EXIT_OK


# --------------------------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    for flags, kw in (
            (("--offline",), dict(action="store_true", help="work on the settings file directly (no running process)")),
            (("--json",), dict(action="store_true", help="print the API shapes unchanged")),
            (("--url",), dict(help="control API base url (default from the settings file, else loopback)")),
            (("--token-file",), dict(help="owner token file (default control.http.owner_token_file)")),
            (("--config",), dict(help="settings file (default ~/.config/mira/mira.json or speech.json)"))):
        common.add_argument(*flags, default=argparse.SUPPRESS, **kw)
    ap = argparse.ArgumentParser(
        prog="mira", parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Tune and operate Mira's wall voice (listener on the Pi, speech server on the Mac mini).",
        epilog="exit codes: 0 applied or ok, 1 invalid value or usage, 2 unknown key or plugin, 3 queued, 4 policy refused,\n"
               "5 conflict, 6 failed and rolled back, 7 pending restart, 8 process unreachable and --offline not given,\n"
               "9 speech unreachable.")
    sub = ap.add_subparsers(dest="command", metavar="<command>", title="commands")

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], help=help_, description=help_)

    add("status", "machine, engine, revision, loop state, failed plugins, session")
    p = add("inventory", "plugins, state, missing services, pending restarts, last error")
    p.add_argument("--machine", choices=("listener", "speech"))
    p = add("describe", "every knob of a plugin: value, default, source, bounds, unit, applies, agent policy")
    p.add_argument("plugin")
    p = add("get", "read settings: get [<plugin>[.<key>]] [--effective]")
    p.add_argument("target", nargs="?")
    p.add_argument("--effective", action="store_true", help="show where each value comes from")
    p = add("set", "set <plugin.key>=<value> [...]; prints <key> <old> -> <new> <status> (<id>)")
    p.add_argument("assignments", nargs="+", metavar="plugin.key=value")
    for flags, kw in ((("--reason",), dict(default="")), (("--trial",), dict(type=float, help="seconds; reverts unless confirmed")),
                      (("--expect-rev",), dict(type=int, dest="expect_rev")), (("--dry-run",), dict(action="store_true")),
                      (("--wait",), dict(type=float, help="seconds to wait for the change to apply"))):
        p.add_argument(*flags, **kw)
    p = add("reset", "reset <plugin.key> [...] to the schema default")
    p.add_argument("keys", nargs="+", metavar="plugin.key")
    for flags, kw in ((("--reason",), dict(default="")), (("--dry-run",), dict(action="store_true"))):
        p.add_argument(*flags, **kw)
    for verb in ("undo", "confirm"):
        p = add(verb, f"{verb} [<change_id>] [--session V-n]" + (" [--force]" if verb == "undo" else ""))
        p.add_argument("change_id", nargs="?")
        p.add_argument("--session")
        if verb == "undo":
            p.add_argument("--force", action="store_true", help="undo past a conflict (owner)")
    p = add("changes", "recent changes, newest first")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--session")
    p.add_argument("--actor")
    p.add_argument("--machine", choices=("listener", "speech", "all"))
    p = add("session", "session [start|keep|revert|end]: the open voice tuning session")
    p.add_argument("action", nargs="?", choices=("start", "keep", "revert", "end"))
    p.add_argument("--topic", default="")
    for verb in ("enable", "disable", "reload"):
        p = add(verb, f"{verb} <plugin>")
        p.add_argument("plugin")
    add("restart", "planned restart of the unit (Pi) or kickstart (mini)")
    p = add("agents", "agents lock|unlock: the kill switch for voice changes")
    p.add_argument("state", choices=("lock", "unlock"))
    p = add("preview", "play a sample on the wall (owner window only)")
    for flags, kw in ((("--voice",), {}), (("--text",), {}), (("--speed",), dict(type=float)), (("--peak",), dict(type=float))):
        p.add_argument(*flags, **kw)
    p = add("voices", "voices [--lang en] [--all] | voices index | voices bench [--voice V]")
    p.add_argument("action", nargs="?", choices=("index", "bench"))
    p.add_argument("--lang")
    p.add_argument("--all", action="store_true", help="include voices that cannot be used")
    p.add_argument("--voice", help="bench: one voice only")
    p = add("schema", "plugins and knobs as the code defines them: schema [<plugin>] [--json]")
    p.add_argument("plugin", nargs="?")
    p = add("tuning-guide", "what can be tuned for a topic: voice, hearing, timing, wake, brain")
    p.add_argument("topic", choices=("voice", "hearing", "timing", "wake", "brain"))
    p = add("config", "config validate [FILE] | render --full [--merge] | edit | reload | diff [REV]")
    cs = p.add_subparsers(dest="config_action", metavar="<action>", required=True)
    q = cs.add_parser("validate", parents=[common], help="check a settings file")
    q.add_argument("file", nargs="?")
    q = cs.add_parser("render", parents=[common], help="print the effective settings as a full file")
    q.add_argument("--full", action="store_true")
    q.add_argument("--merge", action="store_true", help="keep the file's own $comment keys")
    cs.add_parser("edit", parents=[common], help="edit in $EDITOR, validate, apply the difference as one batch")
    cs.add_parser("reload", parents=[common], help="apply the file on disk as one batch (owner)")
    q = cs.add_parser("diff", parents=[common], help="difference between the file and a history revision")
    q.add_argument("rev", nargs="?", type=int)
    p = add("adopt", "freeze the effective legacy values into the settings file (dry run unless --write)")
    p.add_argument("--write", action="store_true")
    p.add_argument("--from-plist", dest="from_plist")
    p.add_argument("--force", action="store_true", help="replace an existing settings file (history keeps the old one)")
    p.add_argument("--all", action="store_true", help="show every key, not only those that differ")
    p = add("unit", "unit render [--check] | unit apply: systemd drop-in (Pi) or plist keys (mini) from core.unit_*")
    us = p.add_subparsers(dest="unit_action", metavar="<action>", required=True)
    q = us.add_parser("render", parents=[common], help="print what would be installed")
    q.add_argument("--check", action="store_true", help="diff against the installed file")
    q = us.add_parser("apply", parents=[common], help="write it, point current at a release, daemon-reload (Pi); never restarts")
    q.add_argument("--release", metavar="SHA", help="point current at this staged release (default: keep it, or the newest)")
    add("rollback", "engine back to legacy, planned restart, prints what legacy cannot represent")
    p = add("deploy", "deploy pi|speech [--stage | --activate SHA | --rollback [SHA] | --dry-run]")
    p.add_argument("target", choices=("pi", "speech"))
    p.add_argument("--stage", action="store_true")
    p.add_argument("--activate", metavar="SHA")
    p.add_argument("--rollback", nargs="?", const="", default=None, metavar="SHA")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--allow-dirty", action="store_true")
    p = add("tokens", "tokens install [--machine listener|speech]: create missing token files, never print a value")
    p.add_argument("token_action", choices=("install",))
    p.add_argument("--machine", choices=("listener", "speech"))
    add("doctor", "drift: unit vs config, argv overrides, ignored env, token modes, binds, port, pins, last good")
    p = add("check", "contract lint (spec 2.11) plus the plugin test files exist")
    p.add_argument("--fast", action="store_true", help="code-only rules (what deploy --stage runs in a release)")
    p = add("docs", "regenerate docs/PLUGINS.md, docs/KNOBS.md, the schema JSON, skill and vault references")
    p.add_argument("--skill", metavar="DIR")
    p.add_argument("--vault", metavar="DIR")
    p = add("plugin", "plugin new <kind> <variant> [--machine listener|speech]: scaffold a disabled plugin")
    ps = p.add_subparsers(dest="plugin_action", metavar="<action>", required=True)
    q = ps.add_parser("new", parents=[common], help="write the module and its test")
    q.add_argument("kind")
    q.add_argument("variant")
    q.add_argument("--machine", choices=("listener", "speech"), default="listener")
    return ap


HANDLERS: dict[str, Callable[[Ctx], int]] = {
    "status": cmd_status, "inventory": cmd_inventory, "describe": cmd_describe, "get": cmd_get, "set": cmd_set,
    "reset": cmd_reset, "undo": lambda c: cmd_undo_confirm(c, "undo"), "confirm": lambda c: cmd_undo_confirm(c, "confirm"),
    "changes": cmd_changes, "session": cmd_session, "enable": lambda c: cmd_toggle(c, "enable"),
    "disable": lambda c: cmd_toggle(c, "disable"), "reload": lambda c: cmd_toggle(c, "reload"), "restart": cmd_restart,
    "agents": cmd_agents, "preview": cmd_preview, "voices": cmd_voices, "schema": cmd_schema,
    "tuning-guide": cmd_tuning_guide, "config": cmd_config, "adopt": cmd_adopt, "unit": cmd_unit,
    "rollback": cmd_rollback, "deploy": cmd_deploy, "tokens": cmd_tokens, "doctor": cmd_doctor, "check": cmd_check,
    "docs": cmd_docs, "plugin": cmd_plugin,
}


def main(argv: Optional[Sequence[str]] = None, *, env: Optional[Mapping[str, str]] = None, home: Optional[Path] = None,
         out: Optional[Callable[[str], None]] = None, err: Optional[Callable[[str], None]] = None,
         root: Optional[Path] = None, **hooks: Any) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE      # argparse exits 2; the contract says 1
    if not args.command:
        parser.print_help()
        return EXIT_USAGE
    env = os.environ if env is None else env
    home = home or Path(env.get("HOME") or os.path.expanduser("~"))
    ctx = Ctx(args=args, env=env, home=home, out=out or print,
              err=err or (lambda s: print(s, file=sys.stderr)), root=root or Path(__file__).resolve().parent.parent,
              runner_factory=hooks.get("runner_factory"), editor=hooks.get("editor"),
              offline_opener=hooks.get("offline_opener"), local_runner=hooks.get("local_runner"))
    config_arg = getattr(args, "config", None) or env.get("MIRA_CONFIG", "")
    ctx.machine = hooks.get("machine") or detect_machine(env, home, config_arg)
    ctx.cfg_path = Path(os.path.expanduser(config_arg)) if config_arg else \
        home / ".config" / "mira" / ("mira.json" if ctx.machine == "listener" else "speech.json")
    try:
        return HANDLERS[args.command](ctx)
    except CliError as exc:
        ctx.err(f"mira: {exc.message}")
        return exc.code
    except ApiError as exc:
        return report_api_error(ctx, exc)
    finally:
        if isinstance(ctx.backend, OfflineBackend):
            ctx.backend.close()


if __name__ == "__main__":
    sys.exit(main())
