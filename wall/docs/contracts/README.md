# Mira contracts

These files are the contract that every plugin package, the core, the control API and the Hermes tools build against. They are normative copies of sections of `SPEC.md`; **if a file here and SPEC.md differ, SPEC.md wins** (and the difference is a bug to report).

| file | what it is | spec sections |
|---|---|---|
| `plugin.md` | Knob, PluginSpec, State, errors, PluginConfig, PluginContext, PluginBase, and the provider rules | 2.1, rules of 2.2 |
| `services.md` | the `typing.Protocol` of every service, multi services, required services per machine | 2.2 |
| `config-format.md` | the one config file per machine: shape, layers, editing rule, every knob with bounds | 2.4.1, 2.4.2 |
| `control-api.md` | routes, errors, response shapes, Change wire form, voice sessions, previews, voice listing | 2.7, 2.5.1, 2.5.4, 2.6, 2.9.3 |
| `control-api.schema.json` | JSON Schema (2020-12) of Change, Error, InventoryRow, KnobRow, Status, Session, TuningGuide, VoiceEntry, Voices, Health | 2.7.3, 2.5.1, 2.5.4, 2.9.3 |
| `config.schema.json` | JSON Schema (2020-12) of the file shape of 2.4.1 | 2.4.1 |

The code side lives in `wall/mirakit/`: `plugin.py` (types and the Core API names), `services.py` (Protocols) and `testkit.py` (FakeCtx, FakeCore, FakeSource, FakeSpotter, cfg_for, run_plugin_contract). `wall/tests/kit/test_kit_contracts.py` checks that the code, the schemas and the two example config files agree.
