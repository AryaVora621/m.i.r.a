# Repair pass (2026-11-10 night): the eight defects from the cold verifier's stage run

Commit: 73de1da on main (pushed). Suite: 1447 passed, 8 skipped (up from 1422 passed, 8 skipped). Frozen files (v1 listener, v1 server, the legacy tests, conftest, room_eval) untouched (git diff --exit-code is 0); golden parity green; `mira check` ok (29 plugins, no stale generated docs, no dashes); `mira --help` and `--help` of all 29 subcommands exit 0. Nothing on the Pi, the mini or any live service was touched: all work is in the repo and on this Mac.

## Per defect: root cause, fix, test

1. legacy.args regained six flags v1 rejects
   - Root cause: `envmap.legacy_mirror` wrote a flag for every knob with an `arg`, including the loop.turns names that exist only on the new engine. Only `adopt` (cli) sanitised the file afterwards; `Core._write_mirror`, which runs after every `mira set`, did not.
   - Fix: `envmap.v1_flags()` reads the option strings of `mira_listen_v1.build_parser()`; `legacy_mirror(..., known_flags)` writes only those, and a knob whose flag v1 lacks and whose value differs from its default goes to the "lost" list (which `mira rollback` prints). `Core` passes the set in both the writer and the report; `cmd_rollback` uses the same rule. If v1 cannot be imported a warning is logged. The old sanitizer stays as a cleaner for files written earlier.
   - Tests: `test_the_mirror_never_holds_a_flag_the_v1_parser_rejects_after_boot_and_after_sets` and `test_what_the_mirror_cannot_carry_is_reported_as_lost` (tests/integration/test_int_mirror.py, parse the generated file with v1's real parser after boot, after sets of v1-representable and v1-unrepresentable knobs, and after an undo); `test_legacy_mirror_drops_flags_the_v1_parser_does_not_define_and_reports_changed_ones` (test_kit_envmap.py). The first two fail with core.py reverted (checked).

2. Online CLI commands and rollback need tokens
   - Root cause: `OnlineBackend._token` raised a plain usage error; `backend_of(fallback_offline=True)` only caught "unreachable".
   - Fix: `TokenMissing` (a CliError). Rollback catches it, says "no usable owner token at ...; working on the file instead (offline)" and runs the offline path, with or without a server. Every other online command fails with one line that names `tokens install` and `--offline`.
   - Tests: `test_rollback_with_no_tokens_and_no_server_still_works_offline_and_says_so`, `test_rollback_with_no_tokens_but_a_running_server_falls_back_to_the_file`, `test_every_online_command_without_tokens_fails_with_one_line_naming_offline` (status, inventory, restart, enable, set), and the tightened `test_unreadable_token_file_is_a_usage_error_not_a_crash` (tests/kit/test_kit_cli.py). All fail on the old cli.py (checked).

3. `mira unit render|apply` failed inside a release
   - Root cause: `deploy/mira-listen.dropin.tmpl` was not in the pi manifest.
   - Fix: added to the manifest (with `bin/mira`).
   - Tests (new file tests/kit/test_kit_release_manifest.py): `test_every_repo_file_the_runtime_code_names_is_in_the_pi_manifest` (AST walk of every string literal in the runtime modules, matched against the repo's data files, so a new reference fails without a hand list; the only exclusions are install-root data and unit names that live under HOME, each with a stated reason; dev-only modules deploy, docsgen, lint, scaffold, testkit are out of scope), `test_the_walk_sees_the_dropin_template` (guards the walker), `test_unit_render_runs_from_a_release_built_from_the_manifest_alone` (builds a release from the manifest only and runs `python -m mirakit.cli unit render`). Both real tests fail with the template removed from the manifest (checked).

4. `mira` wrapper not shipped or installed
   - Root cause: `bin/mira` was in no manifest and nothing installed a command.
   - Fix: manifest ships `bin/mira` (Pi release folder, mini flat install `bin/mira`); `build_tar` gives `bin/` files mode 0755; the manifest's new `command` entry plus `Deployer.install_command` link `~/.local/bin/mira` for the install user: on the Pi to `<root>/current/bin/mira` (to the staged release's copy while `current` has no wrapper, so it never dangles), on the mini to `<root>/bin/mira`. Called at stage, activation (warn-only there, so a refusal cannot leave an activation half done) and speech deploy. It never replaces a regular file or a link to another tool, and keeps the existing link when no wrapper is executable (a rollback to a pre-wrapper release). Nothing running is touched; `--dry-run` lists the `ln` command.
   - Tests (tests/kit/test_kit_deploy.py, with a HOME override so no test touches the real home): `test_the_wrapper_ships_executable_and_the_pi_command_follows_current`, `test_the_command_install_refuses_to_replace_a_foreign_file`, `test_a_foreign_file_at_the_command_path_never_leaves_an_activation_half_done`, `test_a_release_without_the_wrapper_keeps_the_existing_command_link`, `test_the_mini_command_points_at_the_flat_install`, `test_dry_run_shows_the_command_link_and_creates_none`; the manifest list test and the stage and tar tests were extended, not loosened.

5. control.http cannot be enabled live
   - Root cause: `mira enable` is an online command and control.http is the API it would call, so a live toggle can never be requested; offline it only edits the file.
   - Decision: the smaller and safer option, restart class. `toggle="restart"` in listener_plugins/control_http.py. Enabling is `mira enable control.http --offline` plus one restart; disabling likewise. Said in: SPEC.md and spec-parts (10-plugin 5., 30-config, 31-examples, 80-migration M8), contracts (plugin, services, config-format), ADDENDUM.md, CUTOVER_RUNBOOK.md (section 4 intro, 4.4, known issue 5), docs/PLUGINS.md (regenerated).
   - Tests: `test_spec_flags` (test_lp_control_http.py) and the toggle set in `test_lp_contract.py` now assert restart class and that only intent.music is live.

6. Race in speech_plugins/server_http.py
   - Root cause: `note_busy()` ran in the `finally` after the reply was written.
   - Fix: `_note_busy_once()` runs inside `_send` before the status line is written (a failed request is noted before its error reply too; the `finally` only covers a request that never produced a reply; auth failures and control calls are still not noted).
   - Tests: `test_the_busy_note_is_made_before_the_reply_leaves[/stt, /tts]` (a deliberately slow note_busy; fails 2 of 2 on the old file, deterministic, no loosening) and `test_a_failed_request_is_still_noted_busy_before_its_error_reply`. The two racy asserts stay as they were.

7. adopt left persona and chime as `${HERE}` paths
   - Root cause: adopt wrote schema defaults; `${HERE}` is `releases/<id>` after the switch, where those files do not exist, and the mirror expanded the dead paths.
   - Fix: `envmap.freeze_here_paths` rewrites every path knob still holding `${HERE}` to the file beside the live install when it exists (install dir = folder of the script the unit runs, minus `current` or `releases/<id>`); `unfrozen_here_paths` reports the ones with no such file, left as `${HERE}`, with a warning. The dry run shows them with source `install`. Covers persona, chime and the VAD model.
   - Tests: `test_adopt_writes_the_true_persona_and_chime_paths_from_the_live_install` (fake install layout; checks mira.json, legacy.args `--identity` and `--chime`, listen.env MIRA_IDENTITY_FILE and the dry run), `test_adopt_warns_and_keeps_the_default_when_the_install_has_no_such_file` (test_kit_cli.py), `test_freeze_here_paths_writes_the_file_beside_the_install_or_leaves_the_default` (test_kit_envmap.py). Fail on the old cli.py (checked).

8. wake.kws rebuild time
   - Fix: all four rebuild knobs (kws_dir, keywords, threshold, score) are `applies="restart"`; SPEC 2.3.3 records the result; config tables and M3 text updated; docs/KNOBS.md, PLUGINS.md and schema.generated.json regenerated.
   - Tests: `test_knob_classes_and_owner_only` (test_lp_wake_kws.py) now asserts restart for every knob. The three integration tests that exercise the core's rebuild machinery (test_int_safe_point.py) keep their full coverage through a rig-local `rebuild_class_kws` fixture, which also asserts that the real specs are restart class. Nothing removed or weakened.

## Other tooling faults fixed
- SPEC 6.2 and spec-parts/80-migration Pi test commands: private 0700 folder under /home/mira (no fixed /tmp name), wall/ complete with bin/ and persona, `voice/models/kws` beside it, pytest copied into wall/ for the scaffold subprocess test, `nice -n 10` instead of the invalid `-p Nice=10` on a scope, `--basetemp` inside the folder, cleanup including /tmp/pytest-of-mira.
- cutover/expected-mira.json.md rows for persona and chime updated to what adopt now writes; runbook Gap B, 3.4, rollback note and a repair-pass note under section 6.

## Counts
Before: 1422 passed, 8 skipped. After: 1447 passed, 8 skipped (up from 1422 passed, 8 skipped). No test removed; four test bodies were adjusted only where the shipped behaviour changed on purpose (manifest list, stage file set, control.http toggle, wake.kws classes).

## Not done here, outside this repo (for the orchestrator)
- Release 71dcc7c34598 on the Pi predates the repair: restage the new commit before the cutover (it still lacks the wrapper, the template and the fixes). The first `mira deploy pi --stage` of the repaired commit also creates `/home/mira/.local/bin/mira`; if a hand copy exists there, delete it first (the tool refuses to overwrite a plain file).
- A rollback to a release from before the wrapper leaves `~/.local/bin/mira` pointing at `current/bin/mira`, which then does not exist; use the python -m route from the runbook in that case.
- Stray copies of the old knob classes live outside the repo: the Hermes skill references (knobs.md, glossary.md), the vault notes, and the agentic-os mock control API (`pi/mira-mcp/mock_control.py`) may still show wake.kws as rebuild and control.http as live; regenerate/update them in their own places.
- Rung 5 (room recordings) is still not run; the live cutover steps, the owner's window and any restart remain the orchestrator's.
- Housekeeping slip: I restored one test file with `git checkout -- <file>` after my own sed edit to it (the file ended identical to HEAD); no other git state was touched. The `/tmp/pytest-of-mira` leftover on the Pi from the stage run is still there (the runbook step 0.1 removes it).
