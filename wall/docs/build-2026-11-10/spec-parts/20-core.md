
### 2.3 Core API, threading and safe points (`mirakit/core.py`, normative)

`Scope`, `RESET`, `RESET_STREAM`, `Edit`, `Change`, `CoreAPI` and `RemoteClient` are defined in P1's `mirakit/plugin.py` (wave 1) so wave 2 can import them; `Core` itself is P3's `mirakit/core.py`. The Core registers itself as the service `core` (a `CoreAPI`), which is how the `control.http` plugins reach it.

```python
from typing import Any, Literal, Optional, Sequence
Scope = Literal["owner", "agent", "system"]
RESET = object()                          # Edit.value sentinel: reset the key to its schema default
RESET_STREAM = 1                          # safe_point() flag: the loop must create a new spotter stream and clear pre-roll

@dataclass(frozen=True)
class Edit:
    key: str                              # "plugin.key", e.g. "agc.mic.target", "core.engine", "loop.turns.followup_s"
    value: Any

@dataclass
class Change:                             # wire form in section 2.5.1
    id: str; ts: str; machine: str; actor: str; scope: Scope; session: Optional[str]; reason: str
    edits: list[dict]                     # [{"key", "old", "new"}]
    applies: str                          # strongest class among the edits: live < rebuild < restart
    status: str                           # queued | applied | pending_restart | failed | reverted | confirmed
    rev_before: int; rev_after: int
    trial_expires: Optional[str]; undo_of: Optional[str]; error: Optional[str]; announce: str

class CoreAPI(Protocol):                  # what control.py and the CLI may call (any thread). Lives in mirakit/plugin.py.
    machine: str
    def status(self) -> dict: ...
    def inventory(self) -> list[dict]: ...
    def describe(self, plugin: str, *, scope: Scope) -> dict: ...
    def schema(self) -> dict: ...
    def config(self, *, effective: bool = True) -> dict: ...
    def tuning_guide(self, topic: str, *, scope: Scope) -> dict: ...
    def submit(self, edits: Sequence[Edit], *, actor: str, scope: Scope, reason: str = "",
               trial_s: Optional[float] = None, expect_revision: Optional[int] = None,
               dry_run: bool = False, wait_s: float = 0.0, session: Optional[str] = None) -> Change: ...
    def undo(self, change_id: Optional[str] = None, *, actor: str, scope: Scope,
             session: Optional[str] = None, force: bool = False) -> list[Change]: ...
    def confirm(self, change_id: Optional[str] = None, *, actor: str, scope: Scope,
                session: Optional[str] = None) -> list[Change]: ...
    def changes(self, *, limit: int = 20, since: Optional[str] = None, session: Optional[str] = None,
                actor: Optional[str] = None) -> list[dict]: ...
    def session(self, action: Literal["start", "get", "keep", "revert", "end"], *, actor: str, scope: Scope,
                topic: str = "") -> Optional[dict]: ...
    def preview(self, *, actor: str, scope: Scope, text: str = "", voice: str = "", speed: float = 0.0,
                peak: float = 0.0, wait_s: float = 15.0) -> dict: ...
    def set_enabled(self, plugin: str, enabled: bool, *, actor: str, scope: Scope) -> Change: ...
    def reload_plugin(self, plugin: str, *, actor: str, scope: Scope) -> Change: ...
    def reload_file(self, *, actor: str, scope: Scope) -> Change: ...
    def request_restart(self, *, actor: str, scope: Scope) -> dict: ...
    def register_remote(self, machine: str, client: "RemoteClient") -> None: ...
    def attach_remote_change(self, change: dict) -> None: ...   # a change made on the mini inside the open session

class RemoteClient(Protocol):             # implemented by control.py (HTTP to the mini /ctl/v1)
    def request(self, method: str, path: str, body: Optional[dict] = None) -> dict: ...   # raises RemoteError

class Core(CoreAPI):
    @classmethod
    def from_args(cls, args, *, specs: Sequence[PluginSpec], source=None, machine: str = "listener") -> "Core": ...
        # Test and legacy-compatible mode. Values = vars(args) mapped through Knob.arg (argparse already read env);
        # for a knob with Knob.env whose arg is None or absent from the namespace, the env var is read, else the default
        # (this is exactly what v1 did, e.g. MicAGC(target=None) read MIRA_AGC_TARGET; release, decay, min_rms, ceiling,
        # floor_init have env only).
        # In-memory store. MUST NOT read or write ~/.config/mira, bind a port, start control.http, write a change log
        # or the legacy mirror. The source, when given, is registered as the "source" service and both source
        # plugins are disabled in memory, so no pw-record starts in a test.
    @classmethod
    def from_file(cls, path, *, argv: Sequence[str], machine: str, specs: Sequence[PluginSpec],
                  parser: "argparse.ArgumentParser | None" = None, mirror: bool = False,
                  changelog: bool = True) -> "Core": ...
        # Production. `parser` is mira_listen.build_parser() on the listener (None on the mini); envmap parses argv
        # twice (normal, and with every default replaced by argparse.SUPPRESS) to find the flags explicitly given.
        # Layers (highest first): flags explicitly present in argv, the file, MIRA_* env
        # (only while core.legacy_env == "fallback"), schema default.
    def boot(self) -> None: ...           # replay trials (2.5.3), resolve providers, start plugins in order; exit 78 on missing required
    def bind_args(self, ns) -> None: ...  # the listener's args namespace; knobs with Knob.arg are kept in sync on the loop thread
    def service(self, name: str, default: Any = ...) -> Any: ...
    def services(self, name: str) -> list[Any]: ...
    def cfg(self, plugin: str) -> PluginConfig: ...
    # loop thread only:
    def safe_point(self, armed: bool) -> int: ...
    def turn_begin(self) -> None: ...     # right before Brain.ask; phase := "brain"
    def turn_end(self) -> None: ...       # right after Brain.ask returns or raises; phase := None; live-only drain
    def note_busy(self) -> None: ...      # speech only: server.http calls it after each /stt or /tts (busy_defer_s)
    def note(self, kind: str) -> None: ...   # from Listener._mark: records "wake", "followup", "wake_rejected",
                                             # "no_speech", "offline", "empty" times; no text kept
    def shutdown(self) -> None: ...       # stop plugins in reverse order; idempotent
```

#### 2.3.1 Threading rule (contract)
1. The loop thread owns every plugin object on the listener. Only the loop thread calls `start`, `stop`, `apply`, `rebuild`, and assigns attributes on plugin objects or on the args namespace.
2. A control thread (HTTP handler or CLI in-process) may only: validate, check policy, persist the file, append the change log, put the change on the apply queue, and read snapshots (`status`, `inventory`, `describe`, `config`, `changes`). It reads `core.turn` (one reference read) and nothing else of the loop's state.
3. The one exception is the inline preview of section 2.6, which runs on the control thread during the brain phase, under `playback.lock`, while the loop thread is blocked inside `Brain.ask`.
4. The per-chunk hot path takes no lock and allocates nothing new for the core. `safe_point()` with an empty queue and no due timer is one `queue.empty()` check and one float compare.
5. The mini has no loop: one apply worker thread runs every apply and rebuild serially; request threads take one reference per request (engine object and an immutable settings snapshot) and never see a half-applied change.

#### 2.3.2 The safe points (exact)
`safe_point(armed)` is called as the first statement of every `Listener.run` iteration, with `armed = self.src.clock() >= self.followup_until`. It runs, in order:
1. Timers: expire trials and sessions that are due (2.5.3, 2.5.4). A due revert becomes a queued Change with `actor="expiry"`, `scope="system"`.
2. Drain the apply queue in FIFO order. For each change:
   - live edits: apply now, armed or not: owning plugin `apply()`, then every plugin whose `spec.reads` lists the key, then `setattr(args, knob.arg, value)` for knobs with `arg`.
   - rebuild edits: apply only when `armed` and `now - last_turn_event >= control.http.rebuild_quiet_s` (default 10 s; `last_turn_event` is the latest `note()` time) and no voice session is open. Otherwise the change stays queued, and every later change touching the same plugin waits behind it; changes to other plugins proceed.
   - restart edits: status `pending_restart` (already persisted).
3. Deferred previews (owner previews requested outside a turn): only when `armed`; played on the loop thread (2.6).
4. If a rebuild or a preview ran in this call, or `pending_flush` is set by an inline preview: call `source.flush()` (the backlog that queued during the rebuild or preview is dropped, never processed), clear `pending_flush`, and return `RESET_STREAM`. Otherwise return 0.

The loop handles the flag (two added lines, section 4):
```python
flags = self.core.safe_point(self.src.clock() >= self.followup_until)
if flags & RESET_STREAM:
    stream = self.kws.create_stream(); pre.clear(); fvad_on = False
```

`turn_end()` is the second safe point, on the loop thread right after `Brain.ask` returns or raises (the mic is not being read and no audio stage is running). It applies queued live edits only (no rebuilds, no previews) and applies the voice-session follow-up overlay, so a session opened during this turn already gives the 15 s window when `handle_clip` computes `followup_until`.

Why the backlog is flushed: while the loop thread is busy, `PwSource` keeps queuing up to 60 s of audio. Processing it after a rebuild would feed seconds of old audio to the mute counter, AGC and spotter in a burst. Dropping it costs only the same deafness the rebuild already caused; the mute detector counts samples, so a flush only delays a mute verdict.

#### 2.3.3 Measured deaf window gate
Before any Pi knob is shipped as `rebuild`, pre-flight P0.5 (section 5) measures on the Pi: spotter build time, peak RSS during dispose-then-build, VAD build time, and cold start (import plus model load to the first chunk). If a spotter rebuild takes more than 2.0 s, or peak RSS of the listener during a rebuild exceeds 170 MB, `wake.kws` knobs are shipped as `restart` instead. Cold start counts as deaf time inside `RestartSec=3`; it is recorded in the report and in the vault note.

#### 2.3.4 Turn phase
`core.turn` is `None` or a frozen `Turn(id: str, phase: "brain", started: float)`. `turn_begin()` sets it, `turn_end()` clears it. `id` is `T-<monotonic counter>`. Only the brain phase is tracked: it is the only time Hermes can call tools for this turn.
