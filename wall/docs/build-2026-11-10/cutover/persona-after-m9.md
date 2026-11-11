# Persona lines that wait for the later Hermes step (M9)

Not in `MIRA_WALL_IDENTITY.merged.md` because they are not true while the plugin engine alone is running. Add them to the live persona file only after the `mira` MCP is registered, the mira-self skill is installed, the smoke test and dialogue eval pass, and the owner has decided the voice tuning question below. With `persona.file.reread=turn` no listener restart is needed; back up the file first (`cp -p ... .bak-persona-<date>`) and keep it under 3000 bytes (merged is 2709, so about 290 bytes are free: drop or shorten something before adding all of this).

## Lines from the build's draft (knowledge/MIRA_WALL_IDENTITY.md)

1. Self-tuning paragraph (What you can do):
   "You can tune your own voice, hearing and timing with your mira tools, one small step at a time, and every change is a ten minute trial unless he says keep. Load the mira-self skill first."
   Adjust before use: she already has one loudness line ("You can make your own voice louder or quieter when he asks (this never changes the music)."). Keep that single line for loudness. Until the speech path decision below is made, name only hearing and timing here ("tune your own hearing and timing"), not "voice".
2. The by-voice boundary, replacing nothing in the live text but sitting next to the Honesty section:
   "By voice you only do easy-to-undo things: screens, music, timers, reading out, and those mira tools. You never send or delete mail, change the calendar, touch a printer, buy or delete anything, or change any other setting by voice, because the wake word sometimes fires on its own. If he asks, say it needs him at a screen, and offer to leave a note."
   Conflict to settle first: the live Honesty bullet lets her send mail or start a print if "he clearly asked" and she confirmed out loud. This paragraph forbids it. Hermes's SOUL.md already forbids it from a voice turn. Decide which one the owner wants, then make the persona and SOUL.md agree.
3. Opener rule: "No gushing openers ("Sure!", "Great question")" instead of the live "No openers like "Sure!" or "Great question"". Only needed because the scripted tuning dialogue opens with a calm "Sure."; leave the live wording until that dialogue ships.

## Lines from the draft that are NOT to be added at all

- "The wall is quiet from eleven at night to six in the morning." (owner decision: she speaks at night, only quieter; the merged file says so instead.)
- "Most answers are one." (the live 25 word hard limit is stricter.)
- "A screen goes away on its own after a few minutes." (conflicts with the live "never describe how long it stays"; Hermes screens last 1 to 30 minutes, default 5, and the voice-live check showed a 10 minute push.)
- "At night he mutes it and the wall goes dim, so stay quiet and brief." and the school-hours line: Hermes's SOUL.md already carries the schedule and night mode, so adding them to the persona duplicates context on every turn.

## Decision that gates line 1 (owner)

Her live voice is Kokoro on aiserver through the splitter. Speed, peak and voice changes made on the mini's speech server do not reach it (aiserver's v1 server ignores speed and peak, and an unknown voice name falls back to its default). Either run the plugin speech server on aiserver and put it behind the splitter, or leave voice tuning out of the persona and the skill and keep `voice_volume` as the only voice control. See drift.md section 7.
