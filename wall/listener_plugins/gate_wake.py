"""gate.wake: the transcript gate. The module names WAKE_WORD, WAKE_OK, WAKE_PREFIX, has_wake, strip_wake and
HALLUCINATIONS are v1's (copied); the Gate class compiles the same patterns from the config.

FIXED (freeze list): the transcript is verified on wake clips, an empty transcript is a reject, and a reject opens
no follow-up window. The compiled patterns equal v1's byte for byte under the default config:
  WAKE_OK     = ^(?:(?:<prefixes joined by |>)\\W+)?(?:<wake_word>)\\b
  WAKE_PREFIX = ^\\s*(?:(?:<prefixes>)\\W+)?(?:<wake_word>)\\b[\\s,.!?:;-]*      both re.I
With no prefixes the prefix group is dropped, not left empty (an empty group would accept leading punctuation).
"""

import re

from mirakit.plugin import ConfigError, Knob, PluginBase, PluginSpec

LEGACY_CLASSES = ()


HALLUCINATIONS = {"you", "thank you", "thanks", "thanks for watching", "bye", "okay", "uh", "um", "hmm", "the"}


# The recognizer spells "Mira" many ways (Mira, Meera, Beerah, Miura...). This accepts up to two letters, an i/ee/y vowel, one or
# more r, then a/ah (Moonshine says Beera, Viera, Vera on short clips), and so rejects mirror, Maria, Mara, miracle, Miranda.
WAKE_WORD = r"[a-z]{0,2}(?:i|ee|ea|e|y|ie|iu)r+(?:a|ah|ar|uh)h?"
WAKE_OK = re.compile(rf"^(?:(?:hey|hi|ok|okay)\W+)?(?:{WAKE_WORD})\b", re.I)
WAKE_PREFIX = re.compile(rf"^\s*(?:(?:hey|hi|ok|okay)\W+)?(?:{WAKE_WORD})\b[\s,.!?:;-]*", re.I)


def has_wake(text):
    """True when the transcript opens with the wake word (the spotter's own verdict is not trusted alone)."""
    return bool(WAKE_OK.match(text.strip()))


def strip_wake(text):
    # The brain does not need the wake word; drop it from the front of the request.
    return WAKE_PREFIX.sub("", text, count=1).strip()


# The v1 set in a fixed order, for the schema default and the example file.
DEFAULT_HALLUCINATIONS = ("you", "thank you", "thanks", "thanks for watching", "bye", "okay", "uh", "um", "hmm", "the")

# The test_unit.py table, plus the spellings spec 2.4.2 names. A candidate wake_word that breaks one is refused.
GATE_CASES = [
    ("Mira, what's on my calendar?", True), ("Hey Mira pause the music", True), ("Beerah paused the music.", True),
    ("Meera what time is it", True), ("Miura, what's on my calendar today?", True), ("Myra, play", True),
    ("Viera paused the music", True), ("Vera paused the music.", True),
    ("Mirror, what's on my calendar?", False), ("Maria, what's on my calendar", False), ("Mara, hi", False),
    ("Miranda is coming at six", False), ("The miracle store", False), ("what's on my calendar", False),
    ("Hey Siri", False), ("Mia", False),
    ("Mira", True), ("Meera", True), ("Beerah", True), ("Miura", True), ("Myra", True), ("Viera", True), ("Vera", True),
    ("Mirror", False), ("Maria", False), ("Mara", False), ("Miranda", False), ("miracle", False), ("Siri", False),
]


def compile_gate(wake_word, prefixes):
    """(WAKE_OK, WAKE_PREFIX) for a wake_word regex and a list of prefix words, with the v1 templates."""
    alt = "|".join(re.escape(p) for p in prefixes)
    pre = f"(?:(?:{alt})\\W+)?" if alt else ""
    ok = re.compile(rf"^{pre}(?:{wake_word})\b", re.I)
    prefix = re.compile(rf"^\s*{pre}(?:{wake_word})\b[\s,.!?:;-]*", re.I)
    return ok, prefix


class Gate:
    """The `gate` service (spec 2.2)."""

    def __init__(self, wake_word=WAKE_WORD, prefixes=("hey", "hi", "ok", "okay"), hallucinations=HALLUCINATIONS):
        self.configure(wake_word, prefixes, hallucinations)

    def configure(self, wake_word, prefixes, hallucinations):
        self.wake_word, self.prefixes = wake_word, list(prefixes)
        self.hallucinations = set(hallucinations)
        self.wake_ok, self.wake_prefix = compile_gate(wake_word, prefixes)

    def has_wake(self, text):
        """True when the transcript opens with the wake word (the spotter's own verdict is not trusted alone)."""
        return bool(self.wake_ok.match(text.strip()))

    def strip_wake(self, text):
        return self.wake_prefix.sub("", text, count=1).strip()

    def normalize(self, text):
        return " ".join(re.sub(r"[^a-z0-9' ]", "", text.lower()).split())

    def is_empty(self, norm):
        return not norm or norm in self.hallucinations


class GateWake(PluginBase):
    def start(self):
        c = self.cfg
        self.gate = Gate(c.wake_word, c.wake_prefixes, c.hallucinations)
        self.ctx.provide("gate", self.gate)

    def apply(self, changed, cfg):
        self.cfg = cfg
        self.gate.configure(cfg.wake_word, cfg.wake_prefixes, cfg.hallucinations)

    def status(self):
        return {"prefixes": len(self.gate.prefixes), "hallucinations": len(self.gate.hallucinations)}


def validate(values):
    try:
        gate = Gate(values["wake_word"], values["wake_prefixes"], values["hallucinations"])
    except re.error as e:
        raise ConfigError(f"wake_word is not a valid pattern: {e}", "wake_word") from None
    for text, want in GATE_CASES:
        if gate.has_wake(text) is not want:
            verb = "accept" if want else "reject"
            raise ConfigError(f"the gate must {verb} {text!r}", "wake_word")


SPEC = PluginSpec(
    name="gate.wake", kind="gate", version="1", machine="listener", provides=("gate",),
    summary="Transcript gate that confirms the spotter's wake word.",
    create=lambda ctx, cfg: GateWake(ctx, cfg), order=90, validate=validate,
    knobs=(
        Knob("wake_word", "regex", WAKE_WORD, "The spellings of her name the transcript gate accepts.", topic="wake",
             spoken="the names I answer to", phrases=("you answer to the wrong name",)),
        Knob("wake_prefixes", "list[str]", ["hey", "hi", "ok", "okay"], "Words allowed before her name.", unit="items",
             min=0, max=10),
        Knob("hallucinations", "list[str]", list(DEFAULT_HALLUCINATIONS),
             "Transcripts treated as empty.", unit="items", min=0, max=50),
    ),
    tests=("wall/tests/plugins/test_lp_gate_wake.py",),
)
