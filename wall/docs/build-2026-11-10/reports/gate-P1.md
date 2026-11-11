# Gate review of P1 (contracts, test kit, mock control server)

Verdict: PASS (after 5 small repairs). Not blocking: no contract is wrong enough to waste wave 2.

## What was diffed
- `mirakit/plugin.py` against 10-plugin.md block 1 and 20-core.md (Knob, PluginSpec, State, ConfigError, PolicyError, ConflictError, PluginConfig, PluginContext, Paths, PluginBase, Scope, RESET, RESET_STREAM, Edit, Change, CoreAPI, RemoteClient): every name, field, default, enum value, error code and signature matches. Extras (Knob.__post_init__ rules, RESET type, PluginConfig.__reduce__, Change.to_dict/from_dict) are P1's accepted deviations.
- `mirakit/services.py` against 10-plugin.md block 2: identical apart from the module docstring.
- `docs/contracts/{plugin,services,config-format,control-api}.md` against the spec parts by diff: faithful slices (only headers and pointers added).
- `control-api.schema.json` against 2.7.1 to 2.7.3, 2.5.1, 2.5.4, 2.9.3: field names, required lists and enums match; Error enum lists every 2.7.1 code. `config.schema.json` matches 2.4.1.
- Both example configs in the kit test equal the JSON blocks of 31-examples.md (checked by script).
- Mock knob rows against 2.4.2: all ten agent knobs (hard bounds, agent min/max/step, units, applies, topics, spoken, effect, phrases), wake.kws.threshold (default 0.15), gate.wake.wake_word, voices.catalog.agent_max_rtf match. Policy rule table 2.5.5, trial clamp 60..1800, session cap, voice-turn rule, undo window, error codes and HTTP statuses match.

## Mismatches found and repaired (P1 files only, each with a test)
1. `preview_quiet` default: addendum says "". Fixed in `docs/contracts/config-format.md` and in the PI_EXAMPLE test data. Test: `test_preview_quiet_default_is_empty_everywhere`.
2. `playback.miraaudio.night_window` and `night_gain` (addendum decision 1) were missing from `config-format.md`; added with the addendum's type, default, bounds, class and agent deny. Test: `test_night_knobs_are_listed_in_the_config_table`.
3. Mock quiet_hours message said "I don't play samples at night." which contradicts the owner rule that she is never described as refusing at night. Now "I can't play samples during the quiet hours that are set." The code and state flag stay. Tests: `test_quiet_hours_message_never_says_night` and the old assertion relaxed.
4. Mock counted every edit of a batch as one write against the 12 per 10 minutes. 2.5.1 says a batch is one Change and FakeCore already counted one per Change. Mock now records one entry per Change (per-knob cap still counts each touched key). Test: `test_batch_counts_as_one_write_in_the_rate_window`.
5. `FakeCore` skipped 2.5.1 step 3 for regex, url and time_window values. It now rejects an uncompilable regex, a non http(s) url and a time_window not shaped `HH:MM-HH:MM` or empty, with ConfigError code invalid and the field set. Test: `test_fakecore_checks_regex_url_and_time_window` (4 cases).

## Gates (exact last lines, run after the repairs)
- Kit: `79 passed in 2.25s` (was 73; +6)
- Mock: `46 passed in 1.43s` (was 44; +2)
- G-legacy: `65 passed, 8 skipped in 23.85s`
- G-frozen: exit 0, no output
- G-dash over 4 mirakit files, 6 docs/contracts files (md and json), the kit test, mock_control.py and mock_test.py: `dash files: []`
- Only P1-owned files were edited.

## Open (spec ambiguous or wrong; recommendation, nothing guessed)
- O1: 2.1 rule 5 says every numeric knob has min, max and unit, yet 2.4.2 lists no unit for `intent.music.priority`, `tts.engine.peak`, `agc.mic.floor_rate_*` and others. `run_plugin_contract` does not enforce it (P1 deviation 8). Recommend: `mira check` (P6) requires min and max, and accepts unit "" for dimensionless knobs, with the spec text amended.
- O2: 6.4 asks the contract suite to check that `rebuild()` frees the old object (weakref) and that a live knob changes the service attribute. `run_plugin_contract` checks only that apply() does not raise. Recommend: each plugin package asserts those two in its own `test_lp_*` and `test_sp_*` files; the kit stays generic.
- O3: 2.5.1 puts hard bounds before policy; the mock and the dialogue test (e) need policy first for the agent scope (accepted deviation 1, in the addendum). The spec text should be amended so P3's core does the same, or the skill will see 400 instead of `allowed`. P3 must follow the addendum.
- O4: the mock omits routes the brief did not ask for: GET /v1/schema, GET /v1/config, POST /v1/config/reload, enable, disable, reload, restart, the 202 queued answer for wait_s, and the 429 `busy` preview slot. Fine for the MCP tests; P6 tests must not use the mock for those.
- O5: spec 31-examples.md (and the 2.4.3 text) still shows `preview_quiet "23:00-06:00"`; P4's `deploy/mira.example.json` must use "" per the addendum, and the generated KNOBS.md follows the schema.
- O6: wire timestamps: the mock and FakeCore emit UTC (`+0000`), the spec example shows `-0500`; both parse with `%Y-%m-%dT%H:%M:%S%z` (accepted deviation 9).
