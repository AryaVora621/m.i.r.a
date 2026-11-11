#!/usr/bin/env python3
"""shadow_compare: the PASS rule of the shadow run (spec 6.6), over the live listener's journal and the shadow log.

    journalctl --user -u mira-listen -o short-unix > live.txt      (short-iso lines work too)
    python tests/tools/shadow_compare.py --journal live.txt --shadow shadow.txt [--followup-s 8]

Only the periods in which the live listener was ARMED count. A busy window runs from a live `wake` line to its closing
`followup`, `wake_rejected`, `no_speech`, `offline` or `empty` line, plus followup_s; it starts one match tolerance
after the wake line so the shadow's own hit for that wake is still compared. PASS when
  - at least 95 percent of live spotter hits have a shadow hit within 1.0 s,
  - shadow-only hits are at most 5 percent of the live hits (at most 3 when there are fewer than 60),
  - the mute transitions match one to one within 2 s (same verdict),
  - the AGC gain at matched hits differs by at most 0.5x.
Exit code 0 on PASS, 1 on FAIL, 2 on unusable input. Stdlib only.
"""

import argparse
import datetime
import json
import re
import sys

HIT_TOL_S = 1.0
MUTE_TOL_S = 2.0
LIVE_HIT_SHARE = 0.95
SHADOW_ONLY_SHARE = 0.05
SHADOW_ONLY_SMALL = 3
SMALL_RUN = 60
GAIN_DIFF = 0.5
CLOSERS = ("followup", "wake_rejected", "no_speech", "offline", "empty")

_UNIX = re.compile(r"^(\d{9,11}(?:\.\d+)?)\s+")
_ISO = re.compile(r"^(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:[+-]\d\d:?\d\d|Z)?)\s+")
_BODY = re.compile(r"^(?:.*?\]:\s+)?(?:\d\d:\d\d:\d\d\s+)?(\w+)\s*(.*)$")
_AGC = re.compile(r"agc\s+([0-9.]+)x")
_SHADOW_HIT = re.compile(r"shadow hit t=([0-9.]+) agc=([0-9.]+)")
_SHADOW_MIC = re.compile(r"shadow mic t=([0-9.]+) (muted|live)")


def _epoch(text):
    m = _UNIX.match(text)
    if m:
        return float(m.group(1)), text[m.end():]
    m = _ISO.match(text)
    if m:
        raw = m.group(1).replace("Z", "+00:00").replace(" ", "T", 1)
        if re.search(r"[+-]\d{4}$", raw):
            raw = raw[:-2] + ":" + raw[-2:]
        try:
            dt = datetime.datetime.fromisoformat(raw)
        except ValueError:
            return None, text
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.timestamp(), text[m.end():]
    return None, text


def parse_journal(text):
    """[(epoch, kind, detail)] for every line with a timestamp and a loop mark."""
    out = []
    for line in text.splitlines():
        t, rest = _epoch(line.strip())
        if t is None:
            continue
        m = _BODY.match(rest)
        if m:
            out.append((t, m.group(1), m.group(2).strip()))
    return out


def parse_shadow(text):
    hits, mics = [], []
    for line in text.splitlines():
        m = _SHADOW_HIT.search(line)
        if m:
            hits.append((float(m.group(1)), float(m.group(2))))
            continue
        m = _SHADOW_MIC.search(line)
        if m:
            mics.append((float(m.group(1)), m.group(2)))
    return sorted(hits), sorted(mics)


def busy_windows(events, followup_s):
    wins, start, last_t = [], None, None
    for t, kind, detail in events:
        last_t = t
        if kind == "wake":
            if start is not None:        # the previous one never closed: it ended where this one began
                wins.append((start + HIT_TOL_S, t - HIT_TOL_S))
            start = t
        elif kind == "followup_capture" and start is None:
            start = t
        elif kind in CLOSERS and start is not None:
            wins.append((start + HIT_TOL_S, t + followup_s))
            start = None
    if start is not None and last_t is not None:
        wins.append((start + HIT_TOL_S, last_t + followup_s))
    return wins


def _inside(t, wins):
    return any(a <= t < b for a, b in wins)


def _match(live, shadow, tol):
    """Greedy nearest matching of two sorted time lists. Returns (pairs [(i, j)], unmatched live, unmatched shadow)."""
    used, pairs = set(), []
    for i, lt in enumerate(live):
        best, bd = None, None
        for j, st in enumerate(shadow):
            if j in used:
                continue
            d = abs(st - lt)
            if d <= tol and (bd is None or d < bd):
                best, bd = j, d
        if best is not None:
            used.add(best)
            pairs.append((i, best))
    mi = {i for i, _ in pairs}
    return pairs, [i for i in range(len(live)) if i not in mi], [j for j in range(len(shadow)) if j not in used]


def compare(journal_text, shadow_text, followup_s=8.0):
    events = parse_journal(journal_text)
    shadow_hits, shadow_mics = parse_shadow(shadow_text)
    wins = busy_windows(events, followup_s)
    live_hits = [(t, float(_AGC.search(d).group(1)) if _AGC.search(d) else None)
                 for t, k, d in events if k == "wake"]
    s_hits = [h for h in shadow_hits if not _inside(h[0], wins)]
    pairs, live_miss, shadow_only = _match([h[0] for h in live_hits], [h[0] for h in s_hits], HIT_TOL_S)
    n_live = len(live_hits)
    matched_share = (len(pairs) / n_live) if n_live else 1.0
    allowed_only = SHADOW_ONLY_SMALL if n_live < SMALL_RUN else SHADOW_ONLY_SHARE * n_live
    gain_bad = [(live_hits[i][0], live_hits[i][1], s_hits[j][1]) for i, j in pairs
                if live_hits[i][1] is not None and abs(live_hits[i][1] - s_hits[j][1]) > GAIN_DIFF]
    live_mics = [(t, d.split()[0]) for t, k, d in events if k == "mic" and d.split() and d.split()[0] in ("muted", "live")]
    mp, m_live_miss, m_shadow_only = _match([t for t, _ in live_mics], [t for t, _ in shadow_mics], MUTE_TOL_S)
    mic_bad = [i for i, j in mp if live_mics[i][1] != shadow_mics[j][1]]
    checks = {
        "live_hits_matched": matched_share >= LIVE_HIT_SHARE,
        "shadow_only_hits": len(shadow_only) <= allowed_only,
        "mute_transitions": not m_live_miss and not m_shadow_only and not mic_bad,
        "agc_gain": not gain_bad,
    }
    return {
        "pass": all(checks.values()), "checks": checks,
        "live_hits": n_live, "matched": len(pairs), "matched_share": round(matched_share, 4),
        "live_unmatched": [round(live_hits[i][0], 3) for i in live_miss],
        "shadow_only": [round(s_hits[j][0], 3) for j in shadow_only], "shadow_only_allowed": allowed_only,
        "shadow_hits_total": len(shadow_hits), "shadow_hits_counted": len(s_hits),
        "busy_windows": len(wins), "gain_mismatches": gain_bad,
        "mute_live": len(live_mics), "mute_shadow": len(shadow_mics),
        "mute_live_unmatched": [round(live_mics[i][0], 3) for i in m_live_miss],
        "mute_shadow_only": [round(shadow_mics[j][0], 3) for j in m_shadow_only], "mute_wrong_verdict": len(mic_bad),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--shadow", required=True)
    ap.add_argument("--followup-s", type=float, default=8.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        journal, shadow = open(a.journal, encoding="utf-8", errors="replace").read(), open(a.shadow, encoding="utf-8", errors="replace").read()
    except OSError as e:
        print(f"cannot read input: {e.strerror}", file=sys.stderr)
        return 2
    r = compare(journal, shadow, a.followup_s)
    if a.json:
        print(json.dumps(r, indent=1))
    else:
        for k, v in r["checks"].items():
            print(f"{'ok  ' if v else 'FAIL'} {k}")
        print(f"live hits {r['live_hits']} matched {r['matched']} ({r['matched_share']:.1%}); shadow-only {len(r['shadow_only'])} "
              f"(allowed {r['shadow_only_allowed']:g}); busy windows {r['busy_windows']}; mute live {r['mute_live']} "
              f"shadow {r['mute_shadow']}")
    print("PASS" if r["pass"] else "FAIL")
    return 0 if r["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
