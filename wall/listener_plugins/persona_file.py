"""persona.file: the identity text sent to Hermes as per-turn instructions. v1 read the file once at start (and used
"You are Mira." when it was missing); `reread = "turn"` rereads it when its mtime changed, before each ask.

FIXED: the persona is the whole file text, unmodified.
"""

from pathlib import Path

from mirakit.plugin import Knob, PluginBase, PluginSpec

from .common import expand_path

LEGACY_CLASSES = ()


class Persona:
    """The `persona` service (spec 2.2)."""

    def __init__(self, path, fallback="You are Mira.", reread="start"):
        self.path, self.fallback, self.reread = path, fallback, reread
        self._text, self._mtime = None, None
        self._load()

    def _load(self):
        p = Path(self.path)
        # The same test v1 used: a missing file means the fallback, an unreadable one raises as it did.
        if p.exists():
            self._text = p.read_text()
            self._mtime = p.stat().st_mtime_ns
        else:
            self._text, self._mtime = self.fallback, None

    def text(self):
        if self.reread == "turn":
            p = Path(self.path)
            try:
                mtime = p.stat().st_mtime_ns
            except OSError:
                mtime = None
            if mtime != self._mtime:
                self._load()
        return self._text


class PersonaFile(PluginBase):
    def start(self):
        c = self.cfg
        self.persona = Persona(expand_path(c.path, self.ctx.paths), c.fallback, c.reread)
        self.ctx.provide("persona", self.persona)

    def apply(self, changed, cfg):
        self.cfg = cfg
        p = self.persona
        p.reread = cfg.reread
        if cfg.fallback != p.fallback:
            p.fallback = cfg.fallback
            if p._mtime is None:  # still on the fallback: show the new one
                p._text = cfg.fallback

    def status(self):
        return {"from_file": self.persona._mtime is not None, "chars": len(self.persona._text or "")}


SPEC = PluginSpec(
    name="persona.file", kind="persona", version="1", machine="listener", provides=("persona",),
    summary="Persona text from a file, read once or on change.",
    create=lambda ctx, cfg: PersonaFile(ctx, cfg), order=110,
    knobs=(
        Knob("path", "path", "${HERE}/MIRA_WALL_IDENTITY.md", "Persona file sent as per-turn instructions.",
             applies="restart", arg="identity", env="MIRA_IDENTITY_FILE"),
        Knob("fallback", "str", "You are Mira.", "Used when the file is missing.", unit="chars", min=1, max=200),
        Knob("reread", "enum", "start", "start reads the file once (v1); turn rereads it when it changed.",
             choices=("start", "turn")),
    ),
    tests=("wall/tests/plugins/test_lp_persona_file.py",),
)
