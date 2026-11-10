"""gate.wake: the v1 names copied, and the Gate class compiling the same patterns from the config."""
import re

import pytest

import test_lp_common as H
from listener_plugins import gate_wake as gw
from mirakit.plugin import ConfigError

SPEC = gw.SPEC

HAS_WAKE = [
    ("Mira, what's on my calendar?", True), ("Hey Mira pause the music", True), ("Beerah paused the music.", True),
    ("Meera what time is it", True), ("Miura, what's on my calendar today?", True), ("Myra, play", True),
    ("Viera paused the music", True), ("Vera paused the music.", True),
    ("Mirror, what's on my calendar?", False), ("Maria, what's on my calendar", False), ("Mara, hi", False),
    ("Miranda is coming at six", False), ("The miracle store", False), ("what's on my calendar", False),
    ("Hey Siri", False), ("Mia", False),
]
STRIP = [("Mira, what's on my calendar?", "what's on my calendar?"), ("Hey Mira what time is it", "what time is it"),
         ("What did Mira say", "What did Mira say"), ("ok Meera... play it", "play it"), ("   Vera   ", ""),
         ("hi, Myra! next song", "next song")]
NORMALIZE = ["Thank you!", "  Um,  ", "Okay.", "what's up?", "", "!!!", "The", "Thanks for watching!", "Mira"]


def test_module_constants_equal_v1():
    assert gw.WAKE_WORD == H.v1.WAKE_WORD
    assert gw.WAKE_OK.pattern == H.v1.WAKE_OK.pattern and gw.WAKE_OK.flags == H.v1.WAKE_OK.flags
    assert gw.WAKE_PREFIX.pattern == H.v1.WAKE_PREFIX.pattern and gw.WAKE_PREFIX.flags == H.v1.WAKE_PREFIX.flags
    assert gw.HALLUCINATIONS == H.v1.HALLUCINATIONS and set(gw.DEFAULT_HALLUCINATIONS) == H.v1.HALLUCINATIONS
    assert len(gw.DEFAULT_HALLUCINATIONS) == 10


@pytest.mark.parametrize("text,ok", HAS_WAKE)
def test_has_wake_table(text, ok):
    assert gw.has_wake(text) is ok and H.v1.has_wake(text) is ok
    assert gw.Gate().has_wake(text) is ok


@pytest.mark.parametrize("raw,out", STRIP)
def test_strip_wake_table_equals_v1(raw, out):
    assert gw.Gate().strip_wake(raw) == H.v1.strip_wake(raw) == gw.strip_wake(raw)


def test_default_gate_compiles_the_v1_patterns_byte_for_byte():
    g = gw.Gate()
    assert g.wake_ok.pattern == H.v1.WAKE_OK.pattern and g.wake_ok.flags == H.v1.WAKE_OK.flags
    assert g.wake_prefix.pattern == H.v1.WAKE_PREFIX.pattern and g.wake_prefix.flags == H.v1.WAKE_PREFIX.flags
    ok, prefix = gw.compile_gate(H.v1.WAKE_WORD, ["hey", "hi", "ok", "okay"])
    assert (ok.pattern, prefix.pattern) == (H.v1.WAKE_OK.pattern, H.v1.WAKE_PREFIX.pattern)


@pytest.mark.parametrize("text", NORMALIZE)
def test_normalize_and_is_empty_equal_the_v1_expressions(text):
    g = gw.Gate()
    v1_norm = " ".join(re.sub(r"[^a-z0-9' ]", "", text.lower()).split())
    assert g.normalize(text) == v1_norm
    assert g.is_empty(v1_norm) is (not v1_norm or v1_norm in H.v1.HALLUCINATIONS)


def test_custom_prefixes_and_empty_prefix_list():
    g = gw.Gate(prefixes=["yo"])
    assert g.has_wake("yo Mira what's up") and not g.has_wake("hey Mira what's up") and g.has_wake("Mira")
    bare = gw.Gate(prefixes=[])
    assert bare.wake_ok.pattern == rf"^(?:{gw.WAKE_WORD})\b"
    assert bare.has_wake("Mira") and not bare.has_wake("hey Mira") and not bare.has_wake(", Mira")
    assert bare.strip_wake("Mira, play") == "play"


def test_prefix_words_are_escaped():
    g = gw.Gate(prefixes=["a.b"])
    assert g.has_wake("a.b Mira") and not g.has_wake("axb Mira")


def test_plugin_provides_gate_and_apply_recompiles():
    plugin, ctx, cfg = H.started("gate.wake")
    g = ctx.get("gate")
    assert g.has_wake("Hey Mira") and g.is_empty("thank you")
    H.assert_live_knobs(SPEC, plugin, {
        "wake_word": (r"[a-z]{0,2}(?:i|ee)r+a", lambda p: g.wake_word),
        "wake_prefixes": (["yo"], lambda p: g.prefixes), "hallucinations": (["zzz"], lambda p: sorted(g.hallucinations))})
    plugin.apply({"wake_prefixes": ["yo"]}, cfg.replace(wake_prefixes=["yo"]))
    assert g.has_wake("yo Mira") and not g.has_wake("hey Mira")
    plugin.apply({"hallucinations": ["zzz"]}, cfg.replace(hallucinations=["zzz"]))
    assert g.is_empty("zzz") and not g.is_empty("thank you")


def _values(**over):
    v = {k.name: (list(k.default) if isinstance(k.default, list) else k.default) for k in SPEC.knobs}
    v.update(over)
    return v


def test_validate_accepts_the_default_and_refuses_a_gate_that_breaks_the_table():
    SPEC.validate(_values())
    with pytest.raises(ConfigError) as e:
        SPEC.validate(_values(wake_word=r"mira"))  # rejects Meera, Vera...
    assert e.value.field == "wake_word" and "accept" in e.value.message
    with pytest.raises(ConfigError) as e:
        SPEC.validate(_values(wake_word=r"[a-z]*"))  # accepts Mirror
    assert "reject" in e.value.message
    with pytest.raises(ConfigError):
        SPEC.validate(_values(wake_word=r"(unclosed"))


def test_gate_cases_cover_the_spec_names_and_the_unit_table():
    cases = dict(gw.GATE_CASES)
    for name in ("Mira", "Meera", "Beerah", "Miura", "Myra", "Viera", "Vera"):
        assert cases[name] is True
    for name in ("Mirror", "Maria", "Mara", "Miranda", "miracle", "Mia", "Siri"):
        assert cases[name] is False
    assert all(dict(gw.GATE_CASES)[t] is ok for t, ok in HAS_WAKE)


def test_owner_only_and_spec_shape():
    assert all(k.agent == "deny" for k in SPEC.knobs) and SPEC.provides == ("gate",)
    assert {k.name for k in SPEC.knobs} == {"wake_word", "wake_prefixes", "hallucinations"}
