"""intent.printers: are the printers running and how long is left, from the Agent OS quick route.

Payload shape: quick._printers over services.printer.fetch_all (state is Klipper's, eta_seconds is None below 2 percent).
"""
import pytest

import test_lp_common as H
import test_lp_quick_common as Q
from listener_plugins import intent_printers as ip
from listener_plugins import quick_common as qc

SPEC = ip.SPEC


def pr(**over):
    base = {"id": "sv08", "name": "Sovol SV08", "enabled": True, "connected": True, "state": "printing", "progress": 0.42,
            "eta_seconds": 4200}
    base.update(over)
    return base


def say(printers, **over):
    return ip.compose("status", {"printers": printers}, Q.NOW, Q.cfg(ip, **over))


@pytest.mark.parametrize("text,key", [
    ("are the printers running", "status"), ("Are the printers running?", "status"), ("are my printers printing", "status"),
    ("are the printers busy", "status"), ("are the printers done", "status"), ("is the printer running", "status"),
    ("is the printer done", "status"), ("is my printer printing", "status"), ("is the print done", "status"),
    ("is the print finished", "status"), ("are the prints done", "status"), ("how's the print going", "status"),
    ("how is the print", "status"), ("how are the prints going", "status"), ("printer status", "status"), ("print status", "status"),
    ("what are the printers doing", "status"), ("what's the printer doing", "status"),
    ("how long is left on the print", "status"), ("how much time is left on the print", "status"),
    ("how long is left on the printer", "status"), ("how long until the print is done", "status"),
    ("how long till the printer finishes", "status"), ("when will the print be done", "status"), ("when is the print done", "status"),
    ("how much time is remaining on the prints", "status"),
    ("start a print", None), ("cancel the print", None), ("pause the print", None), ("pause the printer", None),
    ("stop the print", None), ("print the document", None), ("how long does a benchy take to print", None),
    ("how do i print a file", None), ("is the printer out of filament", None), ("why did the print fail", None),
    ("what filament is loaded", None), ("what's the bed temperature", None), ("show me the printers", None),
    ("is the printer plugged in", None), ("how long is left on my homework", None), ("how long until my test", None),
    ("when is the print going to fail", None), ("pause", None), ("stop", None), ("what's playing", None),
])
def test_phrasings(text, key):
    assert qc.phrase_match(text, ip.DEFAULT_PATTERNS) == key


def test_answers_for_realistic_printer_rows():
    assert say([pr()]) == "Sovol SV08 is printing, 42 percent done, about 1 hour and 10 minutes left."
    assert say([pr(), pr(id="v", name="Voron", state="standby", progress=0.0, eta_seconds=None)]) == (
        "Sovol SV08 is printing, 42 percent done, about 1 hour and 10 minutes left. The other printer is idle.")
    assert say([pr(state="standby", progress=0.0, eta_seconds=None)]) == "Sovol SV08 is idle."
    assert say([pr(state="standby"), pr(id="v", name="Voron", state="cancelled")]) == "Both printers are idle."
    assert say([pr(state="standby"), pr(id="v", name="Voron", state="complete")]) == (
        "Nothing is printing right now. Sovol SV08 is idle. Voron is done printing.")
    assert say([pr(state="complete")]) == "Sovol SV08 is done printing."
    assert say([pr(state="paused")]) == "Sovol SV08 is paused."
    assert say([pr(state="error")]) == "Sovol SV08 is reporting an error."
    assert say([pr(state="offline", connected=False)]) == "Sovol SV08 is offline."


def test_early_in_a_print_there_is_no_estimate_and_it_says_so():
    assert say([pr(progress=0.01, eta_seconds=None)]) == "Sovol SV08 is printing, 1 percent done, no time estimate yet."
    assert say([pr(progress=0.0, eta_seconds=None)]) == "Sovol SV08 is printing, no time estimate yet."


def test_short_remaining_times():
    assert "under a minute left" in say([pr(eta_seconds=20)])
    assert "about 5 minutes left" in say([pr(eta_seconds=300)])
    assert "about 2 hours left" in say([pr(eta_seconds=7200)])


def test_two_printing_and_mixed_fleets():
    two = say([pr(), pr(id="p", name="Prusa", eta_seconds=9000, progress=0.1)])
    assert two == ("Sovol SV08 is printing, about 1 hour and 10 minutes left. "
                   "Prusa is printing, about 2 hours and 30 minutes left.")      # the percents would pass 25 words
    assert Q.words(two) <= 25
    assert "42 percent" in say([pr(), pr(id="p", name="Prusa", eta_seconds=9000, progress=0.1)], max_words=40)
    three = say([pr(), pr(id="p", name="Prusa", eta_seconds=9000, progress=0.1),
                 pr(id="o", name="Old Ender", state="offline", connected=False)])
    assert three.endswith("Old Ender is offline.") and Q.words(three) <= 25
    assert say([pr(state="standby"), pr(id="v", name="Voron", state="offline", connected=False)]) == (
        "Nothing is printing right now. Sovol SV08 is idle. Voron is offline.")
    assert say([pr(state="standby"), pr(id="b", name="B", state="standby"), pr(id="c", name="C", state="standby")]) == (
        "All three printers are idle.")


def test_disabled_and_unconfigured_printers_are_not_mentioned():
    rows = [pr(), pr(id="b", name="Bambu", enabled=False, state="not_configured"), pr(id="c", name="Ghost", state="not_configured")]
    assert say(rows) == "Sovol SV08 is printing, 42 percent done, about 1 hour and 10 minutes left."
    assert say([pr(id="b", enabled=False, state="not_configured")]) is None


def test_word_budget_drops_the_percent_then_the_extras():
    big = [pr(name="Sovol SV08 Max Left Bay", id=str(n), eta_seconds=4200 + n * 600) for n in range(4)]
    for budget in (25, 18, 12):
        text = say(big, max_words=budget)
        assert text and Q.words(text) <= max(budget, 12) + 5, (budget, text)
    assert "percent" not in say(big, max_words=25)


def test_empty_or_junk_payloads_fall_through():
    assert ip.compose("status", {"printers": []}, Q.NOW, Q.cfg(ip)) is None
    assert ip.compose("status", {}, Q.NOW, Q.cfg(ip)) is None
    assert ip.compose("status", {"printers": None}, Q.NOW, Q.cfg(ip)) is None
    assert ip.compose("status", {"printers": ["x", 5, None]}, Q.NOW, Q.cfg(ip)) is None
    assert say([pr(state=None)]) is None


def _started(tmp_path, server, **over):
    return H.started("intent.printers", url=server.url, token_file=Q.token_file(tmp_path), **over)


def test_service_end_to_end_and_default_timeout_covers_a_slow_printer(tmp_path):
    assert next(k for k in SPEC.knobs if k.name == "timeout_s").default == 4.0     # Agent OS waits up to 3 s on a dead printer
    s = Q.quick_server({"printers": {"printers": [pr()]}}, delay=0.3)
    try:
        plugin, ctx, cfg = _started(tmp_path, s)
        i = ctx.get("intent")
        assert i.match("are the printers running?") == "status"
        assert i.handle("status").startswith("Sovol SV08 is printing")
        assert [c["path"] for c in s.calls] == ["/api/hooks/quick/printers"]
    finally:
        s.stop()


@pytest.mark.parametrize("status", [401, 500, 503])
def test_errors_and_refusals_go_to_hermes(tmp_path, status):
    s = Q.quick_server({"printers": {"printers": [pr()]}}, status=status)
    try:
        plugin, ctx, cfg = _started(tmp_path, s)
        assert ctx.get("intent").match("are the printers running") is None
    finally:
        s.stop()
    plugin, ctx, cfg = H.started("intent.printers", url=H.refused_url(), token_file=Q.token_file(tmp_path))
    assert ctx.get("intent").match("are the printers running") is None


def test_live_knobs_apply():
    plugin, ctx, cfg = H.started("intent.printers")
    i = ctx.get("intent")
    pats = dict(ip.DEFAULT_PATTERNS, status=r"gimme printers")
    H.assert_live_knobs(SPEC, plugin, {
        "priority": (7, lambda p: i.priority), "url": ("http://127.0.0.1:1/", lambda p: i.client.url, "http://127.0.0.1:1"),
        "token_file": ("/t/quick", lambda p: i.client.token_file), "timeout_s": (1.5, lambda p: i.client.timeout_s),
        "timezone": ("Asia/Tokyo", lambda p: i.cfg.timezone), "patterns": (pats, lambda p: i.patterns),
        "max_words": (30, lambda p: i.cfg.max_words)})


def test_spec_shape():
    assert SPEC.name == "intent.printers" and SPEC.default_enabled is False and ip.LEGACY_CLASSES == ()
