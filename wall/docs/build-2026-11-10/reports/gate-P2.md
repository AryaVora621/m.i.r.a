# Gate P2: do the wave 1 parity tests bite?

Method: throwaway copy of mira/wall per perturbation (voice symlinked), one change to the copy's mira_listen.py (v1 left intact, so live parity compares v1 against the perturbed module), run test_golden_parity.py and test_characterize.py with -x. Copies deleted.

| # | Perturbation | Original suite | Caught by (first failure) | After repair |
|---|---|---|---|---|
| 1a | AGC release 0.25 to 0.5 | caught | test_live_parity[s01] agc_digests | n/a |
| 1b | AGC decay 0.03 to 0.06 | caught | test_live_parity[s01] agc_digests | n/a |
| 1c | AGC target 0.05 to 0.055 | caught | test_live_parity[s01] agc_digests | n/a |
| 2a | WAKE_OK without the trailing \b (accepts Miracle, Miranda) | NOT caught (68 passed) | scripted transcripts never exercise the regex | test_wake_table_pinned[mira_listen], test_wake_patterns_equal_v1 |
| 2b | WAKE_OK drops "hey" prefix | NOT caught | same | test_wake_table_pinned |
| 2c | empty transcript counts as accepted | caught | test_live_parity[s10] events | n/a |
| 3a | follow-up window 8.0 to 7.0 | caught | test_live_parity[s01] events | n/a |
| 3b | spotter fed inside the window (stream.accept_waveform) | caught, but only by one scenario | test_live_parity[s09] events | n/a |
| 3c | spotter runs inside the window (no continue) | caught | test_live_parity[s02] events | n/a |
| 4a | VAD pre-roll before[-3:] to [-4:] | caught | test_live_parity[s01] captures | n/a |
| 4b | STT head before[:-3] to [:-4] | caught | test_live_parity[s01] stt_sha256 | n/a |
| 4c | pre deque maxlen 15 to 10 | caught | test_live_parity[s01] stt_sha256 | n/a |
| 4d | follow-up seed pre[-5:] to [-4:] | caught | test_live_parity[s02] events | n/a |
| 5a | reporter dedupe removed | NOT caught | normalization drops repeats; rapid duplicates are coalesced anyway | test_reporter_dedupes_and_spaces_state_posts |
| 5b | reporter MIN_GAP 0.5 to 0.2 | NOT caught | normalization drops arrival times | test_reporter_dedupes_and_spaces_state_posts |
| 6 | AGC then mute detector (swapped order) | NOT caught | order is unobservable in output: gain >= 1 never turns zero into non-zero or back, and level-only post differences are normalized away | test_mute_detector_sees_the_raw_chunk_before_the_agc |

All five misses were proven red in a copy with the new tests (each failed only in the [mira_listen] param, the v1 param stayed green), and 5a was re-proven after the test was tightened (a 0.3 s sleep so the first post has left the queue, otherwise coalescing hid the missing dedupe).

## Repair (repo, file owned by P2)
`wall/tests/test_characterize.py`, appended: WAKE_TABLE (33 texts, expected has_wake and strip_wake pinned from v1, including v1's quirks "Sierra" and "Kira" accepted), pattern equality of WAKE_WORD/WAKE_OK/WAKE_PREFIX against v1, a reporter test (dedupe of repeated keys, one post per state change, at least 0.45 s between state posts, MIN_GAP and LEVEL_GAP constants), and a spy test that mute.feed sees the exact raw chunk and runs before agc.process. All run against both v1 and the current module. Added `import os` and `import time`.

## Gates in the repo after repair (unmodified listener, mira_listen.py cmp v1 identical)
- P2 tests (golden_parity, characterize, pwsource): `87 passed in 174.98s` (was 80; plus 7 new)
- G-legacy: `65 passed, 8 skipped in 23.81s`
- G-frozen: exit 0, no output
- G-dash: `dash files: []`

## Remaining gaps (unchanged, not wave 1 regressions)
Level-only post rate (5 per second) and the 2 dp level rounding are still unobserved by scenarios. Perturbation 3b is caught by s09 only. The AGC constants other than the three tried (max_gain, rest, gate, floor rates, ceiling) were not perturbed; digests would change for any that affect fixture audio.
