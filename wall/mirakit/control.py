"""Control API of Mira (spec 2.7): JSON over HTTP/1.1, served by both machines.

One dispatch table maps the routes onto `CoreAPI` calls. The Pi serves it from its own loopback
`ThreadingHTTPServer` (`start_server`); the mini mounts it on the speech server's existing server through
`handle()`. A control thread only ever calls CoreAPI methods (threading rule 2.3.1), never plugin objects.
The Pi also proxies a small allow-list of paths to the mini over `HttpRemote` (rule of 2.7.2).

Stdlib only: this module must import on both machines without numpy or sherpa-onnx.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal, Mapping, Optional, Sequence
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, urlsplit

from mirakit.plugin import (ConfigError, ConflictError, CoreAPI, Edit, PluginConfig, PolicyError, RemoteClient,
                            Scope)

MAX_BODY = 64 * 1024
# Spoken-safe fixed text: an unexpected exception must never leak request text into the answer.
INTERNAL_MESSAGE = "Something went wrong inside the control service."
SPEECH_DOWN_MESSAGE = "My speech server isn't answering, so I changed nothing."

_STATUS_BY_CODE = {
    "invalid": 400, "unknown_key": 400, "unknown_plugin": 400, "unknown_voice": 400, "missing_service": 400,
    "unauthorized": 401,
    "policy": 403, "not_in_voice_turn": 403, "quiet_hours": 403, "session_cap": 403,
    "stale_revision": 409, "undo_conflict": 409, "file_changed": 409, "provider_conflict": 409,
    "agents_locked": 423,
    "rate_limited": 429, "busy": 429,
    "speech_unreachable": 502,
    "internal": 500,
}
# missing_service is not an API code; it is reported as invalid so clients only see the 2.7.1 table
_PUBLIC_CODE = {"missing_service": "invalid"}

_ACTOR_OK = re.compile(r"[a-z][a-z0-9_]{0,23}\Z")
OWNER_ONLY_NOTE = "That is a setting only the owner changes."


class RemoteError(RuntimeError):
    """The other machine failed. `connection` True means it never answered (becomes 502 speech_unreachable);
    otherwise `status`, `code` and `body` are the mini's own error and pass through unchanged."""

    def __init__(self, code: str, message: str = "", status: int = 0, body: Optional[dict] = None,
                 connection: bool = False) -> None:
        super().__init__(message or code)
        self.code, self.message, self.status = code, message or code, status
        self.body = body if isinstance(body, dict) else {}
        self.connection = connection


class HttpRemote(RemoteClient):
    """HTTP client for the mini's `/ctl/v1` family. `base_url` is the bare speech URL (no path); a path given to
    `request` is the Pi style `/v1/...` (or already `/ctl/v1/...`) and is mapped under `/ctl`. The token file is
    re-read on every call and its contents never leave this object."""

    def __init__(self, base_url: str, token_file: str, timeout_s: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file
        self.timeout_s = timeout_s

    def _token(self) -> str:
        try:
            with open(os.path.expanduser(self.token_file), "rb") as fh:
                return fh.read().decode("utf-8", "replace").strip()
        except OSError as exc:
            raise RemoteError("speech_unreachable", "The control token file is not readable.", 502,
                              connection=True) from exc

    def request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        if path.startswith("/v1/") or path == "/v1":
            path = "/ctl" + path
        elif not path.startswith("/ctl/"):
            path = "/ctl/v1/" + path.lstrip("/")
        data = None if body is None else json.dumps(body).encode()
        req = urlrequest.Request(self.base_url + path, data=data, method=method.upper())
        req.add_header("Authorization", "Bearer " + self._token())
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlrequest.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read(8 * 1024 * 1024)
        except urlerror.HTTPError as exc:
            parsed: dict = {}
            try:
                value = json.loads(exc.read(MAX_BODY) or b"{}")
                parsed = value if isinstance(value, dict) else {}
            except ValueError:
                pass
            raise RemoteError(str(parsed.get("error") or "internal"), str(parsed.get("message") or ""),
                              exc.code, parsed) from None
        except (urlerror.URLError, OSError, TimeoutError, ConnectionError) as exc:
            raise RemoteError("speech_unreachable", SPEECH_DOWN_MESSAGE, 502, connection=True) from exc
        try:
            return json.loads(raw) if raw else None
        except ValueError as exc:
            raise RemoteError("speech_unreachable", "The speech server sent something unreadable.", 502,
                              connection=True) from exc


# --------------------------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------------------------

def _cfg_get(cfg: Any, key: str, default: Any) -> Any:
    if cfg is None:
        return default
    try:
        value = cfg.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def error_response(exc: BaseException, revision: int, log: Optional[logging.Logger] = None) -> tuple[int, dict]:
    """(HTTP status, error JSON) for any exception. Mapped by `.code`, not by class: provider_conflict exists
    on both ConfigError and ConflictError."""
    if isinstance(exc, RemoteError):
        if exc.connection:
            return 502, {"error": "speech_unreachable", "message": SPEECH_DOWN_MESSAGE, "revision": revision}
        body = dict(exc.body) if exc.body else {"error": exc.code, "message": exc.message}
        body.setdefault("error", exc.code)
        body.setdefault("message", exc.message or INTERNAL_MESSAGE)
        body.setdefault("revision", revision)
        return (exc.status if 400 <= exc.status < 600 else _STATUS_BY_CODE.get(exc.code, 502)), body
    if isinstance(exc, (ConfigError, PolicyError, ConflictError)):
        code = getattr(exc, "code", "invalid")
        body: dict[str, Any] = {"error": _PUBLIC_CODE.get(code, code), "message": getattr(exc, "message", str(exc))}
        for attr in ("field",):
            value = getattr(exc, attr, "")
            if value:
                body[attr] = value
        allowed = getattr(exc, "allowed", None)
        if allowed:
            body["allowed"] = allowed
        owner_command = getattr(exc, "owner_command", "")
        if owner_command:
            body["owner_command"] = owner_command
        if isinstance(exc, ConflictError):
            if exc.current is not None:
                body["current"] = exc.current
            if exc.revision is not None:
                revision = exc.revision
        near = getattr(exc, "near", None)
        if near:
            body["near"] = list(near)[:3]
        effective = getattr(exc, "effective", None)
        if effective:
            body["effective"] = str(effective)
        body["revision"] = revision
        return _STATUS_BY_CODE.get(code, 400 if isinstance(exc, ConfigError) else 403), body
    if log is not None:
        log.error("control: unexpected %s", type(exc).__name__)   # the class only, never the message
    return 500, {"error": "internal", "message": INTERNAL_MESSAGE, "revision": revision}


class _BadRequest(ConfigError):
    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message, field, "invalid")


# --------------------------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------------------------

def _read_token(path: str) -> bytes:
    if not path:
        return b""
    try:
        with open(os.path.expanduser(str(path)), "rb") as fh:
            return fh.read().strip()
    except OSError:
        return b""


def authenticate(cfg: Any, authorization: str, machine: str = "listener") -> Optional[Scope]:
    """Scope of a bearer header: owner or agent, from WHICH file matched (never from a header). The Pi's agent
    file is `agent_token_file`; the mini's is `pi_token_file`. Token files are re-read on every call. When
    the same secret sits in both files the lower scope (agent) wins. The speech data-plane token is not
    accepted here."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    given = parts[1].strip().encode()
    if not given:
        return None
    owner = _read_token(str(_cfg_get(cfg, "owner_token_file", "")))
    agent_key = "pi_token_file" if machine == "speech" else "agent_token_file"
    agent = _read_token(str(_cfg_get(cfg, agent_key, "")))
    is_agent = bool(agent) and hmac.compare_digest(given, agent)
    is_owner = bool(owner) and hmac.compare_digest(given, owner)
    if is_agent:
        return "agent"
    if is_owner:
        return "owner"
    return None


# --------------------------------------------------------------------------------------------
# the dispatcher
# --------------------------------------------------------------------------------------------

def _num(body: Mapping[str, Any], name: str, integer: bool = False) -> Optional[float]:
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _BadRequest(f"{name} must be a number.", name)
    if integer and int(value) != value:
        raise _BadRequest(f"{name} must be a whole number.", name)
    return int(value) if integer else float(value)


def _as_dict(obj: Any) -> Any:
    return obj.to_dict() if hasattr(obj, "to_dict") else obj


class _Api:
    def __init__(self, core: CoreAPI, cfg: Any, machine: str, log: logging.Logger) -> None:
        self.core, self.cfg, self.machine, self.log = core, cfg, machine, log
        self.started = time.time()
        self._schema_lock = threading.Lock()
        self._speech_schema: Optional[tuple[set[str], Optional[int], float]] = None
        self._fallback_remote: Optional[RemoteClient] = None

    # ---- small helpers
    def revision(self) -> int:
        try:
            return int(self.core.status().get("revision", 0))
        except Exception:
            return 0

    def actor_of(self, scope: Scope, body: Mapping[str, Any]) -> str:
        if scope == "owner":
            return "owner"
        if self.machine == "speech":
            named = body.get("actor")
            return named if isinstance(named, str) and _ACTOR_OK.match(named) else "pi"
        return "hermes"

    def session_of(self, scope: Scope, body: Mapping[str, Any]) -> Optional[str]:
        if self.machine == "speech" and scope == "agent":
            value = body.get("session")
            return value if isinstance(value, str) and re.fullmatch(r"V-\d{1,8}", value) else None
        return None

    def remote(self) -> Optional[RemoteClient]:
        """The speech remote registered on the core. The registry has no getter in the Protocol, so a few
        names are tried; a listener whose core exposes none builds its own from the control.http knobs."""
        if self.machine != "listener":
            return None
        core: Any = self.core
        getter = getattr(core, "remote", None)
        if callable(getter):
            try:
                found = getter("speech")
                if found is not None:
                    return found
            except Exception:
                pass
        for attr in ("remotes", "_remotes"):
            table = getattr(core, attr, None)
            if isinstance(table, Mapping) and table.get("speech") is not None:
                return table["speech"]
        url, token_file = _cfg_get(self.cfg, "speech_url", ""), _cfg_get(self.cfg, "speech_token_file", "")
        if url and token_file:
            if self._fallback_remote is None:
                self._fallback_remote = HttpRemote(str(url), str(token_file))
            return self._fallback_remote
        return None

    def need_owner(self, scope: Scope, what: str = "") -> None:
        if scope != "owner":
            raise PolicyError("policy", OWNER_ONLY_NOTE, field=what)

    def respond(self, payload: Any, status: int = 200) -> tuple[int, Any]:
        if isinstance(payload, dict) and "revision" not in payload:
            payload = dict(payload)
            if payload.get("status") == "dry_run" or "rev_after" not in payload:
                payload["revision"] = self.revision()
            else:
                payload["revision"] = payload["rev_after"]
        return status, payload

    # ---- dispatch
    def dispatch(self, method: str, raw_path: str, body: Optional[dict], scope: Optional[Scope]) -> tuple[int, Any]:
        url = urlsplit(raw_path)
        path = url.path
        if path.startswith("/ctl/"):
            path = path[4:]
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        body = body or {}
        if path == "/v1/health" and method == "GET":
            st = self.core.status()
            return 200, {"ok": bool(st.get("ok", True)), "machine": self.machine, "version": str(st.get("version", "")),
                         "engine": str(st.get("engine", "")), "uptime_s": float(st.get("uptime_s", 0.0)),
                         "revision": int(st.get("revision", 0))}
        if scope is None:
            raise PolicyError("unauthorized", "Missing or wrong token.")
        if not path.startswith("/v1/") and path != "/v1":
            return self._not_found()
        route = path[len("/v1"):]
        actor = self.actor_of(scope, body)
        if method == "GET":
            return self._get(route, query, scope, actor)
        if method == "PUT" and route.startswith("/config/") and route != "/config/batch":
            plugin, _, key = route[len("/config/"):].partition("/")
            if not plugin or not key or "/" in key:
                raise _BadRequest("Use /v1/config/<plugin>/<key>.")
            if "value" not in body:
                raise _BadRequest("The request needs a value.", f"{plugin}.{key}")
            return self._write([{"key": f"{plugin}.{key}", "value": body["value"]}],
                               body, scope, actor, f"/v1/config/{plugin}/{key}", "PUT")
        if method == "POST":
            return self._post(route, query, body, scope, actor)
        return self._not_found()

    @staticmethod
    def _not_found() -> tuple[int, Any]:
        return 404, {"error": "invalid", "message": "No such route."}

    # ---- GET
    def _get(self, route: str, query: dict, scope: Scope, actor: str) -> tuple[int, Any]:
        core = self.core
        machine_q = query.get("machine", "")
        if route == "/status":
            return self.respond(core.status())
        if route == "/inventory":
            if self.machine == "listener" and machine_q == "speech":
                return 200, self._forward("GET", "/v1/inventory", None, query)
            rows = core.inventory()
            if machine_q == "all" and self.machine == "listener":
                try:
                    rows = list(rows) + list(self._forward("GET", "/v1/inventory", None, query))
                except RemoteError as exc:
                    if not exc.connection:
                        raise
            return 200, rows
        if route.startswith("/plugins/"):
            name = route[len("/plugins/"):]
            if not name or "/" in name:
                return self._not_found()
            if self.machine == "listener" and machine_q == "speech":
                return 200, self._forward("GET", f"/v1/plugins/{name}", None, query)
            try:
                return self.respond(core.describe(name, scope=scope))
            except ConfigError as exc:
                if exc.code != "unknown_plugin" or self.machine != "listener" or self.remote() is None:
                    raise
                return 200, self._forward("GET", f"/v1/plugins/{name}", None, query)
        if route == "/schema":
            if self.machine == "listener" and machine_q == "speech":
                return 200, self._forward("GET", "/v1/schema", None, query)
            return self.respond(core.schema())
        if route == "/config":
            if self.machine == "listener" and machine_q == "speech":
                return 200, self._forward("GET", "/v1/config", None, query)
            return self.respond(core.config(effective=query.get("effective", "") in ("1", "true", "yes")))
        if route == "/tuning_guide":
            topic = query.get("topic", "")
            if not topic:
                raise _BadRequest("topic is required.", "topic")
            return self.respond(core.tuning_guide(topic, scope=scope))
        if route == "/changes":
            return self._changes(query, machine_q)
        if route == "/session":
            return self._session_response(core.session("get", actor=actor, scope=scope))
        if route == "/voices":
            return self._voices(query)
        return self._not_found()

    def _limit(self, query: dict, default: int = 20) -> int:
        try:
            n = int(query.get("limit", default))
        except ValueError:
            raise _BadRequest("limit must be a whole number.", "limit") from None
        return min(max(n, 1), 500)

    def _changes(self, query: dict, machine_q: str) -> tuple[int, Any]:
        limit = self._limit(query)
        if self.machine == "listener" and machine_q == "speech":
            return 200, self._forward("GET", "/v1/changes", None, query)
        rows = list(self.core.changes(limit=limit, since=query.get("since") or None,
                                      session=query.get("session") or None, actor=query.get("actor") or None))
        if self.machine == "listener" and machine_q == "all" and self.remote() is not None:
            try:
                rows += list(self._forward("GET", "/v1/changes", None, query))
            except RemoteError as exc:
                if not exc.connection:
                    raise
            rows.sort(key=lambda c: (str(c.get("ts", "")), str(c.get("id", ""))), reverse=True)
            rows = rows[:limit]
        return 200, rows

    def _voices(self, query: dict) -> tuple[int, Any]:
        if self.machine == "listener":
            return 200, self._forward("GET", "/v1/voices", None, query)
        lang = query.get("lang", "")
        limit = self._limit(query, 12)
        include = query.get("include_unusable", "0") in ("1", "true", "yes")
        finder = getattr(self.core, "service", None)
        catalog = None
        if callable(finder):
            try:
                catalog = finder("voices", None)
            except Exception:
                catalog = None
        if catalog is None:
            raise _BadRequest("This machine has no voice catalog.")
        return self.respond(voices_listing(catalog, self.core, lang, limit, include))

    # ---- POST
    def _post(self, route: str, query: dict, body: dict, scope: Scope, actor: str) -> tuple[int, Any]:
        core = self.core
        if route == "/config/batch":
            edits = body.get("edits")
            if not isinstance(edits, list) or not edits or len(edits) > 50:
                raise _BadRequest("edits must be a list of 1 to 50 items.", "edits")
            for item in edits:
                if not isinstance(item, dict) or not isinstance(item.get("key"), str) or "value" not in item:
                    raise _BadRequest("Each edit needs a key and a value.", "edits")
            return self._write(edits, body, scope, actor, "/v1/config/batch", "POST")
        if route == "/config/reload":
            self.need_owner(scope, "config")
            return self.respond(_as_dict(core.reload_file(actor="file", scope=scope)))
        if route in ("/undo", "/confirm"):
            return self._undo_confirm(route[1:], body, scope, actor)
        if route == "/session":
            action = body.get("action")
            if action not in ("start", "keep", "revert", "end"):
                raise _BadRequest("action must be start, keep, revert or end.", "action")
            topic = str(body.get("topic") or "")[:40]
            return self._session_response(core.session(action, actor=actor, scope=scope, topic=topic))
        m = re.fullmatch(r"/plugins/([^/]+)/(enable|disable|reload)", route)
        if m:
            self.need_owner(scope, m.group(1))
            name, verb = m.group(1), m.group(2)
            if verb == "reload":
                change = core.reload_plugin(name, actor=actor, scope=scope)
            else:
                change = core.set_enabled(name, verb == "enable", actor=actor, scope=scope)
            return self.respond(_as_dict(change))
        if route == "/restart":
            self.need_owner(scope, "restart")
            return self.respond(core.request_restart(actor=actor, scope=scope))
        if route == "/preview":
            return self._preview(body, scope, actor)
        return self._not_found()

    def _session_response(self, session: Optional[dict]) -> tuple[int, Any]:
        if session is None:
            return 200, None
        return self.respond(dict(session))

    def _preview(self, body: dict, scope: Scope, actor: str) -> tuple[int, Any]:
        text, voice = body.get("text", ""), body.get("voice", "")
        if not isinstance(text, str) or not isinstance(voice, str):
            raise _BadRequest("text and voice must be text.", "text")
        speed, peak, wait_s = _num(body, "speed"), _num(body, "peak"), _num(body, "wait_s")
        out = self.core.preview(actor=actor, scope=scope, text=text, voice=voice, speed=speed or 0.0,
                                peak=peak or 0.0, wait_s=15.0 if wait_s is None else min(max(wait_s, 0.0), 60.0))
        return self.respond(out)

    def _undo_confirm(self, verb: str, body: dict, scope: Scope, actor: str) -> tuple[int, Any]:
        change_id = body.get("change_id") or None
        session = body.get("session") or None
        for name, value in (("change_id", change_id), ("session", session)):
            if value is not None and not isinstance(value, str):
                raise _BadRequest(f"{name} must be text.", name)
        # The mini keeps its own log: an S- id (or machine=speech) goes to it, everything else to this core.
        if self.machine == "listener" and (str(change_id or "").startswith("S-") or body.get("machine") == "speech"):
            fwd = {k: v for k, v in body.items() if k in ("change_id", "session", "force")}
            fwd["actor"] = actor
            return 200, self._forward("POST", f"/v1/{verb}", fwd, {})
        if verb == "undo":
            out = self.core.undo(change_id, actor=actor, scope=scope, session=session,
                                 force=bool(body.get("force")) and scope == "owner")
        else:
            out = self.core.confirm(change_id, actor=actor, scope=scope, session=session)
        return 200, [_as_dict(c) for c in out]

    # ---- writes (local, or proxied to the mini)
    def _write(self, edits: list, body: dict, scope: Scope, actor: str, path: str, method: str) -> tuple[int, Any]:
        plugins = [e["key"].rpartition(".")[0] for e in edits]
        trial_s = _num(body, "trial_s")
        expect = _num(body, "expect_revision", integer=True)
        wait_s = _num(body, "wait_s")
        reason = body.get("reason") or ""
        if not isinstance(reason, str):
            raise _BadRequest("reason must be text.", "reason")
        dry_run = bool(body.get("dry_run"))
        if wait_s is None:
            wait_s = float(_cfg_get(self.cfg, "wait_s_default", 5.0))
        wait_s = min(max(wait_s, 0.0), 30.0)

        remote_flags = [self._is_speech_plugin(p) for p in plugins]
        if any(remote_flags):
            if not all(remote_flags):
                raise _BadRequest("A batch must stay on one machine.", "edits")
            return self._write_speech(edits, body, scope, actor, path, method, trial_s, reason)

        change = self.core.submit([Edit(e["key"], e["value"]) for e in edits], actor=actor, scope=scope,
                                  reason=reason[:120], trial_s=trial_s, expect_revision=expect, dry_run=dry_run,
                                  wait_s=wait_s, session=self.session_of(scope, body))
        data = _as_dict(change)
        return self.respond(data, 202 if data.get("status") == "queued" else 200)

    def _is_speech_plugin(self, plugin: str) -> bool:
        if self.machine != "listener" or self.remote() is None:
            return False
        local = self._local_plugin_names()
        if plugin in local:
            return False
        names = self._speech_plugin_names()
        if names is None:
            raise RemoteError("speech_unreachable", SPEECH_DOWN_MESSAGE, 502, connection=True)
        return plugin in names

    def _local_plugin_names(self) -> set[str]:
        try:
            return {p["name"] for p in self.core.schema().get("plugins", [])}
        except Exception:
            return set()

    def _speech_plugin_names(self) -> Optional[set[str]]:
        """Names from the mini schema, cached; dropped when a forwarded answer shows a newer mini revision
        (see `_note_remote_revision`) or after a minute."""
        with self._schema_lock:
            cached = self._speech_schema
            if cached and time.monotonic() - cached[2] < 60:
                return cached[0]
        remote = self.remote()
        try:
            schema = remote.request("GET", "/v1/schema") if remote else None
        except RemoteError as exc:
            if not exc.connection:
                raise
            return cached[0] if cached else None
        if not isinstance(schema, dict):
            return cached[0] if cached else None
        names = {p["name"] for p in schema.get("plugins", []) if isinstance(p, dict) and "name" in p}
        with self._schema_lock:
            self._speech_schema = (names, schema.get("revision"), time.monotonic())
        return names

    def _note_remote_revision(self, payload: Any) -> None:
        if isinstance(payload, dict) and isinstance(payload.get("revision"), int):
            with self._schema_lock:
                cached = self._speech_schema
                if cached and cached[1] is not None and cached[1] != payload["revision"]:
                    self._speech_schema = None

    def _forward(self, method: str, path: str, body: Optional[dict], query: Mapping[str, str]) -> Any:
        remote = self.remote()
        if remote is None:
            raise RemoteError("speech_unreachable", SPEECH_DOWN_MESSAGE, 502, connection=True)
        q = {k: v for k, v in query.items() if k != "machine"}
        if q:
            from urllib.parse import urlencode
            path = path + "?" + urlencode(q)
        out = remote.request(method, path, body)
        self._note_remote_revision(out)
        return out

    def _write_speech(self, edits: list, body: dict, scope: Scope, actor: str, path: str, method: str,
                      trial_s: Optional[float], reason: str) -> tuple[int, Any]:
        core = self.core
        if scope == "agent":
            self._agent_precheck(edits)
        dry = bool(body.get("dry_run"))
        # A dry run must leave no state behind, and an owner write is a trial only when the request says so
        # (2.5.3), so only a real agent write opens, joins or extends a voice session.
        session = None
        if scope == "agent" and not dry:
            session = core.session("get", actor=actor, scope=scope)
            if session is None:
                session = core.session("start", actor=actor, scope=scope, topic="voice")
        fwd = {k: v for k, v in body.items() if k in ("reason", "trial_s", "expect_revision", "dry_run", "wait_s")}
        fwd["reason"] = reason[:120]
        fwd["actor"] = actor
        if session:
            fwd["session"] = session.get("id")
            # the mini trial must outlive the session (2.5.4)
            left = self._seconds_left(session)
            base = trial_s if trial_s is not None else float(_cfg_get(self.cfg, "trial_default_s", 600.0))
            fwd["trial_s"] = min(float(_cfg_get(self.cfg, "trial_max_s", 1800.0)), base + left)
        if method == "PUT":
            fwd["value"] = edits[0]["value"]
        else:
            fwd["edits"] = [{"key": e["key"], "value": e["value"]} for e in edits]
        out = self._forward(method, path, fwd, {})
        if (session and not dry and isinstance(out, dict) and out.get("id") and out.get("status") != "dry_run"):
            core.attach_remote_change(out)
        status = 202 if isinstance(out, dict) and out.get("status") == "queued" else 200
        return status, out

    @staticmethod
    def _seconds_left(session: Mapping[str, Any]) -> float:
        expires = session.get("expires")
        if not isinstance(expires, str):
            return 0.0
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
            try:
                from datetime import datetime
                return max(0.0, datetime.strptime(expires, fmt).timestamp() - time.time())
            except ValueError:
                continue
        return 0.0

    def _agent_precheck(self, edits: list) -> None:
        """Rule 4 of 2.5.5 on the Pi side: refuse before anything leaves this machine. The mini checks again."""
        st = self.core.status()
        if st.get("agents_locked") or _cfg_get(self.cfg, "agents_locked", False):
            raise PolicyError("agents_locked", "Settings changes are locked right now.")
        if _cfg_get(self.cfg, "agent_requires_voice_turn", True):
            turn = st.get("turn")
            if not (isinstance(turn, Mapping) and turn.get("phase") == "brain"):
                raise PolicyError("not_in_voice_turn",
                                  "I can only change my settings while we're talking on the wall.")
        remote = self.remote()
        rows: dict[str, dict] = {}
        for edit in edits:
            key = edit["key"]
            plugin, _, knob_name = key.rpartition(".")
            if plugin not in rows:
                rows[plugin] = self._forward("GET", f"/v1/plugins/{plugin}", None, {}) if remote else {}
            row = next((r for r in rows[plugin].get("knobs", []) if r.get("key") == knob_name), None)
            if row is None:
                raise ConfigError(f"{key} is not a setting I know.", key, "unknown_key")
            self._check_knob_policy(key, row, edit["value"])

    @staticmethod
    def _check_knob_policy(key: str, row: Mapping[str, Any], value: Any) -> None:
        spoken = row.get("spoken") or key
        if row.get("agent") != "bounded":
            raise PolicyError("policy", "I'm not allowed to change that one by voice.", field=key,
                              owner_command=f"mira set {key}={_fmt(value)} --trial 600")
        if row.get("type") not in ("int", "float"):
            return
        try:
            new = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{spoken} needs a number.", key, "invalid") from None
        lo, hi, step = row.get("agent_min"), row.get("agent_max"), row.get("agent_step")
        allowed = {"min": lo, "max": hi, "step": step}
        if lo is not None and hi is not None and not (lo - 1e-9 <= new <= hi + 1e-9):
            raise PolicyError("policy", f"By voice I may only set {spoken} between {_fmt(lo)} and {_fmt(hi)}.",
                              field=key, allowed=allowed)
        old = row.get("value")
        if step is not None and isinstance(old, (int, float)) and not isinstance(old, bool) \
                and abs(new - float(old)) > float(step) + 1e-9:
            raise PolicyError("policy", f"By voice I may move {spoken} by at most {_fmt(step)} at a time.",
                              field=key, allowed=allowed)


def _fmt(value: Any) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def voices_listing(catalog: Any, core: Any, lang: str, limit: int, include_unusable: bool) -> dict:
    """Voices response of 2.9.3 from a `voices` catalog service: current first, then featured, then by lang
    and alias; multi-speaker families listed once."""
    entries = [dict(e) for e in catalog.list(lang, include_unusable)]
    if lang:
        entries = [e for e in entries if str(e.get("lang", "")).startswith(lang)]
    families, singles = [], []
    for e in entries:
        if int(e.get("speakers") or 1) > 1 and ":" not in str(e.get("speaker", "")) and e.get("engine") != "kokoro":
            families.append({"model_dir": e.get("model_dir", ""), "speakers": int(e.get("speakers") or 0),
                             "alias_prefix": str(e.get("alias", "")).split(":")[0]})
        else:
            singles.append(e)
    singles.sort(key=lambda e: (not e.get("current"), not e.get("featured"), str(e.get("lang", "")),
                                str(e.get("alias", ""))))
    current = next((e for e in singles if e.get("current")), None)
    cur = {"id": (current or {}).get("id", ""), "alias": (current or {}).get("alias", ""),
           "label": (current or {}).get("label", "")}
    return {"current": cur, "voices": singles[:limit], "families": families}


# --------------------------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------------------------

def _send(handler: BaseHTTPRequestHandler, status: int, payload: Any, close: bool = False) -> None:
    data = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    if close or handler.close_connection:
        handler.send_header("Connection", "close")
        handler.close_connection = True
    handler.end_headers()
    handler.wfile.write(data)


def _read_body(handler: BaseHTTPRequestHandler) -> Optional[dict]:
    if handler.headers.get("Transfer-Encoding"):
        handler.close_connection = True
        raise _BadRequest("Chunked requests are not accepted.")
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        handler.close_connection = True
        raise _BadRequest("Content-Length must be a number.") from None
    if length > MAX_BODY:
        handler.close_connection = True      # the oversized body stays unread
        raise _BadRequest("The request is too large.")
    if length <= 0:
        return None
    raw = handler.rfile.read(length)
    try:
        value = json.loads(raw)
    except ValueError:
        raise _BadRequest("The request body is not valid JSON.") from None
    if not isinstance(value, dict):
        raise _BadRequest("The request body must be a JSON object.")
    return value


def serve_request(api: _Api, handler: BaseHTTPRequestHandler, path: str, scope: Optional[Scope]) -> None:
    started = time.monotonic()
    method = handler.command
    is_health = method == "GET" and urlsplit(path).path.rstrip("/").endswith("/v1/health")
    close = scope is None and not is_health      # a failed auth closes the connection and reads no body
    try:
        body = None if close else (_read_body(handler) if method in ("PUT", "POST") else None)
        status, payload = api.dispatch(method, path, body, scope)
    except Exception as exc:                     # every class is mapped; the message never carries request text
        status, payload = error_response(exc, api.revision(), api.log)
    api.log.info("%s %s %d %dms", method, urlsplit(path).path, status, int((time.monotonic() - started) * 1000))
    try:
        _send(handler, status, payload, close=close)
    except (BrokenPipeError, ConnectionError):
        handler.close_connection = True


_APIS_LOCK = threading.Lock()


def _api_for(core: Any, machine: str) -> _Api:
    api = getattr(core, "_mira_control_api", None)
    if isinstance(api, _Api):
        return api
    with _APIS_LOCK:
        api = _Api(core, None, machine, logging.getLogger("mira.control"))
        try:
            core._mira_control_api = api
        except Exception:
            pass
        return api


def handle(core: CoreAPI, handler: BaseHTTPRequestHandler, path: str, scope: Optional[Scope]) -> None:
    """Serve one `/ctl/v1/...` request on the speech server's own handler. The caller did its own token check
    (`authenticate`); `scope` None answers 401 and closes without reading the body."""
    machine = getattr(core, "machine", "speech") or "speech"
    serve_request(_api_for(core, machine), handler, path, scope)


class ServerHandle:
    def __init__(self, servers: list[ThreadingHTTPServer], threads: list[threading.Thread], api: _Api) -> None:
        self._servers, self._threads, self.api = servers, threads, api
        self.address: tuple[str, int] = servers[0].server_address[:2]
        self.addresses = [s.server_address[:2] for s in servers]
        self._stopped = False

    def set_config(self, cfg: PluginConfig) -> None:
        """Live knobs (tokens, limits) are re-read from the latest snapshot; binds and port need a restart."""
        self.api.cfg = cfg

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        for server in self._servers:
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=5)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _check_bind(address: str, machine: str) -> None:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise ValueError(f"control bind {address!r} must be an IP address") from None
    if ip.is_loopback:
        return
    if machine == "speech" and not ip.is_unspecified and ip.version == 4 \
            and ip in ipaddress.ip_network("100.64.0.0/10"):
        return
    raise ValueError(f"control bind {address} refused: "
                     + ("loopback or tailnet only" if machine == "speech" else "the listener binds loopback only"))


def start_server(core: CoreAPI, cfg: PluginConfig, *, machine: Literal["listener", "speech"], log: logging.Logger,
                 bind: Sequence[str] = (), port: int = 0) -> ServerHandle:
    """Serve the control API. `bind` and `port` override the `control.http` knobs; a zero port means "use the
    knob" and a knob of 0 means an ephemeral port (tests)."""
    addresses = list(bind) or [str(_cfg_get(cfg, "bind", "127.0.0.1"))]
    use_port = int(port or _cfg_get(cfg, "port", 0))
    for address in addresses:
        _check_bind(address, machine)
    api = _Api(core, cfg, machine, log)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "mira-control/1"

        def log_message(self, fmt: str, *args: Any) -> None:   # request lines are logged by serve_request
            pass

        def _serve(self) -> None:
            scope = authenticate(api.cfg, self.headers.get("Authorization", ""), machine)
            serve_request(api, self, self.path, scope)

        do_GET = do_PUT = do_POST = do_DELETE = _serve

    servers: list[ThreadingHTTPServer] = []
    threads: list[threading.Thread] = []
    try:
        for address in addresses:
            server = _Server((address, use_port), Handler)
            use_port = server.server_address[1] if len(addresses) > 1 else use_port
            servers.append(server)
    except Exception:
        for server in servers:
            server.server_close()
        raise
    for server in servers:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                  name=f"mira-control-{server.server_address[1]}", daemon=True)
        thread.start()
        threads.append(thread)
    log.info("control api listening on %s", ",".join(f"{h}:{p}" for h, p in (s.server_address[:2] for s in servers)))
    return ServerHandle(servers, threads, api)
