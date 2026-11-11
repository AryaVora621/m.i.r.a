# Reconcile (2026-11-10)

Commit 71dcc7c on main of mira, pushed. Suite last line: "1422 passed, 8 skipped" (full wall suite, 9 min).

## Changed
- wall/MIRA_WALL_IDENTITY.md = cutover/MIRA_WALL_IDENTITY.merged.md. Checked against the Pi's live file (md5 cf5d457f..., 2460 bytes, unchanged since pre-flight): the merged text is the live text plus one inserted "Where you are" section, nothing removed. It is NOT installed on the Pi (that is M7).
- deploy/listen.env.example and deploy/mira.example.json: speech url now http://127.0.0.1:8766 (splitter), with a comment.
- mirakit/lint.py DEPLOYMENT_DIFFS and tests/test_knob_coverage.py EXPECTED_DIFFS: speech.link.url added as a documented deployment value (same pattern as night_window), else mira check and two tests fail.
- tests/golden/persona_fixture.md (old 1743 byte persona, from git) used by golden/record.py and test_characterize.py, because the stored recordings embed the persona text; recordings untouched, frozen v1 files untouched.
- deploy/README.md: persona path note (absolute paths, not ${HERE}), a step 5 on the splitter and say-volume / mira-audio md5.

## Pre-flight
No checksum mismatch. Gate 2.3.3 (spotter build 12 to 13 s) not fixed: needs spec change, wake.kws knobs as restart class. Other blockers are owner-side.

## Left for the orchestrator
- B1 runbook line (mira set with absolute persona and chime paths before core.engine=plugins) and install of merged persona on the Pi at M7 (cp -p backup, install -o mira -m 644).
- Owner decisions: night level (0.12 x 0.5 = 6 percent), voice tuning to aiserver or dropped.
- Real split of speech address / tts fallback in the engine; splitter ordering drop-in; music repo mira-audio patch.
- Hermes knowledge fixes (voice_volume mapping, SOUL.md last paragraph, architecture notes), wall-AGENTS.md line.
- Night sentence in persona is true only once night_window is set in the plugin engine.
