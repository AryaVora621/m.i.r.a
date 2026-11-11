# Addendum: owner decisions and wave 1 outcomes (2026-11-10 17:05). This overrides the spec and the briefs where they differ.

## Owner decisions
1. Voice at night is allowed, but quieter. There are no silent hours for previews.
   - `control.http.preview_quiet` keeps existing in the schema, but its default is "" (no window) everywhere it appears (schema, examples, mock defaults, docs). The `quiet_hours` error code stays defined; nothing raises it unless the owner sets a window.
   - New knobs on the Pi plugin `playback.miraaudio` (P4 owns them): `night_window` (type time_window, schema default "" meaning off, class live, agent deny) and `night_gain` (float, default 0.5, hard bounds 0.1..1.0, class live, agent deny, description "how loud she is inside the night window, as a share of normal").
   - Behaviour: while local time is inside `night_window`, every sound the plugin plays for Mira (replies, previews and chimes) is scaled by `night_gain` before it goes to `mira-audio`. With `night_window` "" nothing changes at all: no extra file writes, no resampling, byte-identical audio (parity; the golden tests must stay green). The Pi machine file example sets `"night_window": "23:00-06:00"`.
   - Tests (P4): outside the window the bytes handed to the fake `mira-audio` equal the input; inside it the peak is scaled by `night_gain` within 1 percent and the WAV header stays valid; a window that wraps midnight works; "" is off.
   - Knowledge and dialogue texts (P7): never say previews are refused at night; say she plays them more quietly at night.
2. The switch to the plugin engine happens tonight with the owner present. The 24 hour shadow run (M5) is not a gate any more; the shadow runner stays in scope as a tool.
3. The ten voice-tunable knobs and their agent ranges in 2.4.2 are approved as written. Wake sensitivity, models, URLs and tokens stay owner-only.

## Wave 1 outcomes that bind wave 2
- P1's ten deviations (reports/P1.md) are accepted. In particular: for the agent scope the policy check runs before the hard-bounds check (so "talk twice as fast" is refused with the agent range, not a 400); responses that are bare arrays or null carry no `revision` key; `RemoteError` belongs to `mirakit/control.py` (P6), not `plugin.py`.
- Use `FakeCtx.with_specs(spec)` in plugin tests; pass `start=False` to `run_plugin_contract` when `start()` would load real models.
- P2's golden and characterization tests import `mira_listen` by name and must keep passing unchanged (reports/P2.md lists the names they use).
- Other workers are running tests on this Mac at the same time. If a test times out under load and passes alone, say so in the report instead of weakening it.
- Report files under the scratchpad (`reports/<ID>.md`) are required and allowed.
