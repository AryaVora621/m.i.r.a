"""brain.hermes: the Hermes API client. The class is v1's Hermes (copied) with the three timeout and title
constructor parameters and the `fail_reply` and `empty_reply` attributes of spec 4.4.

FIXED: POST /api/sessions then POST /api/sessions/{id}/chat/stream {message, instructions} answered as SSE
(assistant.delta, assistant.completed, error), the retry with a suffixed title on a 400, bearer read per call.
"""

import json
import socket
import threading
import time
import urllib.error
import urllib.request

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginSpec

from .common import expand_path, host_is_loopback, http, log, read_secret

LEGACY_CLASSES = ("Hermes", "HermesTimeout", "HermesCancelled", "_Call")


class HermesTimeout(Exception):
    """The whole answer took longer than the deadline (a stalled SSE stream keeps every socket read alive)."""


class HermesCancelled(Exception):
    """The listener cancelled the turn (barge-in): nobody wants this answer any more."""


class _Call:
    """One ask() in flight. The request runs on its own thread so the caller can stop waiting at any moment, even
    when a socket read or connect never returns; the thread is then abandoned and its result is dropped."""

    def __init__(self):
        self.sid = None
        self.result = None
        self.error = None
        self.resp = None
        self.abandoned = False
        self.done = threading.Event()


def _hang_up(call):
    """Best effort: shut the answer's socket down so the blocked read returns now. Closing a response from another
    thread alone does not wake a recv that is already waiting; shutdown does."""
    call.abandoned = True
    r = call.resp
    if r is None:
        return
    try:
        sock = r.fp.raw._sock
        sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        r.close()
    except Exception:
        pass


class Hermes:
    """The Hermes API as Agent OS's chat.py uses it: POST /api/sessions, then
    POST /api/sessions/{id}/chat/stream {message, instructions} answered as server-sent events."""

    fail_reply = "I cannot reach Hermes right now."  # spoken when ask() raises (the v1 literal)
    empty_reply = "I have nothing to add."  # spoken when the answer is empty (the v1 literal)
    timeout_reply = "Hermes is taking too long."  # spoken when the total deadline passes

    def __init__(self, url, key_file, persona, session_timeout_s=15, stream_timeout_s=150, title_prefix="Mira wall",
                 deadline_s=35.0, stream_speech=True, speak_max_sentences=0):
        self.url, self.key_file, self.persona = url.rstrip("/"), key_file, persona
        self.session_timeout_s, self.stream_timeout_s, self.title_prefix = session_timeout_s, stream_timeout_s, title_prefix
        self.deadline_s = deadline_s
        # Read by the listener, not by this class: speak while the answer is still arriving, and how many sentences of it.
        self.stream_speech, self.speak_max_sentences = stream_speech, speak_max_sentences
        self.session_id = None
        self.last_used = 0.0

    def _hdr(self):
        return {"Authorization": f"Bearer {read_secret(self.key_file)}", "Content-Type": "application/json"}

    def new_session(self):
        title = f"{self.title_prefix} {time.strftime('%Y-%m-%d %H:%M:%S')}"
        for attempt in range(2):
            try:
                _, body = http("POST", f"{self.url}/api/sessions", json.dumps({"title": title}).encode(),
                               self._hdr(), timeout=self.session_timeout_s)
                obj = json.loads(body)
                sess = obj.get("session") or obj.get("data") or obj
                return sess["id"]
            except urllib.error.HTTPError as e:
                # Hermes titles are unique; a second conversation in the same second gets a suffix.
                if e.code == 400 and attempt == 0:
                    title += f" ({hex(int(time.time()))[2:]})"
                    continue
                raise

    def ask(self, text, session_ttl, now, cancel=None, on_text=None):
        """The answer text. Raises HermesTimeout past `deadline_s` in all (0 turns the deadline off) and
        HermesCancelled as soon as the `cancel` event is set; both leave Hermes finishing on its own side, and the
        next ask starts a fresh session so it never queues behind a message Hermes is still working on.
        `on_text(piece)` is called from the stream's thread with every assistant.delta as it arrives (never after the
        ask was abandoned), so the caller can start speaking before the answer is complete."""
        call = _Call()
        threading.Thread(target=self._ask_inner, args=(call, text, session_ttl, now, on_text), daemon=True).start()
        end = time.monotonic() + self.deadline_s if self.deadline_s > 0 else None
        while not call.done.wait(0.05):
            if cancel is not None and cancel.is_set():
                _hang_up(call)
                self.session_id = None
                raise HermesCancelled()
            if end is not None and time.monotonic() >= end:
                _hang_up(call)
                self.session_id = None
                raise HermesTimeout()
        if call.sid is not None and not call.abandoned:
            self.session_id = call.sid
        if call.error is not None:
            raise call.error
        self.last_used = now
        return call.result

    def _ask_inner(self, call, text, session_ttl, now, on_text=None):
        try:
            sid = self.session_id
            if sid is None or now - self.last_used > session_ttl:
                sid = self.new_session()
            call.sid = sid
            call.result = self._stream(call, sid, text, on_text)
        except BaseException as e:  # handed to the waiting ask(); a thread must not die with it unseen
            call.error = e
        finally:
            call.done.set()

    def _stream(self, call, sid, text, on_text=None):
        payload = {"message": text, "instructions": self.persona}
        req = urllib.request.Request(f"{self.url}/api/sessions/{sid}/chat/stream",
                                     data=json.dumps(payload).encode(), method="POST", headers=self._hdr())
        reply, deltas, event = None, [], None
        with urllib.request.urlopen(req, timeout=self.stream_timeout_s) as r:
            call.resp = r
            if call.abandoned:
                return ""
            for raw in r:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    try:
                        d = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if event == "assistant.delta":
                        piece = d.get("delta", "")
                        deltas.append(piece)
                        if on_text is not None and piece and not call.abandoned:
                            try:
                                on_text(piece)
                            except Exception as e:  # speaking is never worth losing the answer for
                                log(f"stream callback failed: {type(e).__name__}")
                    elif event == "assistant.completed":
                        reply = d.get("content", "")
                    elif event == "error":
                        raise RuntimeError(str(d.get("error") or d.get("message") or "hermes error")[:200])
                elif not line:
                    event = None
        return (reply if reply is not None else "".join(deltas)).strip()


class BrainHermes(PluginBase):
    def start(self):
        c = self.cfg
        persona = self.ctx.get("persona").text()
        h = Hermes(c.url, expand_path(c.key_file, self.ctx.paths), persona, session_timeout_s=c.session_timeout_s,
                   stream_timeout_s=c.stream_timeout_s, title_prefix=c.title_prefix, deadline_s=c.deadline_s,
                   stream_speech=c.stream_speech, speak_max_sentences=c.speak_max_sentences)
        h.fail_reply, h.empty_reply, h.timeout_reply = c.fail_reply, c.empty_reply, c.timeout_reply
        h.session_ttl = c.session_ttl  # the loop passes the live value on every ask(); kept here for status
        self.hermes = h
        self.ctx.provide("brain", h)

    def apply(self, changed, cfg):
        self.cfg = cfg
        h = self.hermes
        h.url, h.key_file = cfg.url.rstrip("/"), expand_path(cfg.key_file, self.ctx.paths)
        h.session_ttl = cfg.session_ttl
        h.session_timeout_s, h.stream_timeout_s, h.title_prefix = cfg.session_timeout_s, cfg.stream_timeout_s, cfg.title_prefix
        h.fail_reply, h.empty_reply, h.timeout_reply = cfg.fail_reply, cfg.empty_reply, cfg.timeout_reply
        h.deadline_s = cfg.deadline_s
        h.stream_speech, h.speak_max_sentences = cfg.stream_speech, cfg.speak_max_sentences

    def status(self):
        return {"session": self.hermes.session_id is not None}


def validate(values):
    if not host_is_loopback(values["url"]):
        raise ConfigError("url must point at this machine (127.0.0.1 or localhost)", "url")


SPEC = PluginSpec(
    name="brain.hermes", kind="brain", version="1", machine="listener", provides=("brain",), needs=("persona",),
    summary="Hermes through Agent OS's session API.",
    create=lambda ctx, cfg: BrainHermes(ctx, cfg), order=120, validate=validate,
    knobs=(
        Knob("url", "url", "http://127.0.0.1:8642", "Hermes api_server address.", arg="hermes_url",
             env="MIRA_HERMES_URL"),
        Knob("key_file", "path", "", "Bearer key file, read per call.", secret=True, arg="hermes_key_file",
             env="MIRA_HERMES_KEY_FILE"),
        Knob("session_ttl", "float", 180.0, "Idle time before she starts a fresh conversation.", unit="s", min=30,
             max=1800, agent="bounded", agent_min=60, agent_max=600, agent_step=60, arg="session_ttl", topic="brain",
             spoken="how long I remember our conversation", effect="remembers longer between questions",
             phrases=("you forget what we were talking about",)),
        Knob("session_timeout_s", "float", 15.0, "Seconds creating a session may take.", unit="s", min=3, max=60),
        Knob("stream_timeout_s", "float", 150.0, "Seconds one socket read of the answer may wait.", unit="s", min=10,
             max=600),
        Knob("deadline_s", "float", 35.0, "Longest one answer may take in all, however the stream behaves; past it she "
             "stops waiting.", unit="s", min=5, max=120),
        Knob("stream_speech", "bool", True, "Speak the first sentence as soon as it arrives while the rest of the answer is "
             "still coming, instead of waiting for the whole answer."),
        Knob("speak_max_sentences", "int", 2, "She speaks only this many sentences of a Hermes answer, since it can run past "
             "the persona's 25 words; the wall still gets all of it. 0 speaks it all.",
             unit="sentences", min=0, max=10),
        Knob("title_prefix", "str", "Mira wall", "Start of every session title.", unit="chars", min=1, max=60),
        Knob("fail_reply", "str", "I cannot reach Hermes right now.", "Spoken when Hermes cannot be reached.",
             unit="chars", min=1, max=200),
        Knob("timeout_reply", "str", "Hermes is taking too long.", "Spoken when the answer deadline passes.",
             unit="chars", min=1, max=200),
        Knob("empty_reply", "str", "I have nothing to add.", "Spoken when Hermes answers with nothing.", unit="chars",
             min=1, max=200),
    ),
    tests=("wall/tests/plugins/test_lp_brain_hermes.py",),
)
