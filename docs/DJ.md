# The DJ: persona, prompts, and tuning

Everything that shapes what the moderator says. This is the part you will iterate
on most - small LLMs are picky, and the TTS is pickier.

- [The persona file](#the-persona-file)
- [How a moderation prompt is built](#how-a-moderation-prompt-is-built)
- [The show memory](#the-show-memory)
- [Story blocks](#story-blocks)
- [Numbers are spoken as words](#numbers-are-spoken-as-words)
- [The lyrics writer is deliberately untouched](#the-lyrics-writer-is-deliberately-untouched)
- [Tuning for a different model](#tuning-for-a-different-model)

---

## The persona file

'config/persona.md' (path from 'station.persona_file') becomes the **system
prompt, verbatim**, for both moderation and story. It is the one file to edit to
change the DJ's voice.

The shipped persona pins down what the pipeline needs:

- **German only.** Small models drift into English or leak other languages.
- **Plain spoken text** - letters, full stops, commas. No quotes, no '!'/'?', no
  brackets, asterisks, emojis, lists, or headings. The text goes straight into
  the TTS.
- **Two to four sentences** for moderation. Without a length pin the model
  rambles.
- **Numbers as words** - "übernimm Zahlen und Datumsangaben als Wörter, so wie
  sie im Text stehen" (see [below](#numbers-are-spoken-as-words)).
- Announce the time only around ':00' / ':30'; otherwise use it for atmosphere.
- Engage with a caller when one is present.

If the file is missing the app falls back to a built-in default (kept in sync in
'pilot/dj_brain.py:DEFAULT_PERSONA').

## How a moderation prompt is built

Assembly is pure Python in 'pilot/dj_brain.py' - no extra LLM call. The layout
mirrors the 'Prompt' node written into 'Radio_TTS.json'.

"'
system:  <persona file, verbatim>

user:    Caller Interaction: <text, or None>
         Nächster Song:
         <title> by <artist>
         Genre: <genre, bpm stripped>
         Length: <duration in words>
         Uhrzeit: <clock in words>          # only near :00 / :30
         Datum: <date in words>
         <show-memory digest>
"'

- 'Genre:' is run through 'spoken_genre()' - it drops '"96 bpm"' and bare
  numbers, which the voice mangles. (The **music** workflow keeps the raw genre;
  ACE-Step wants the bpm.)
- 'Caller Interaction:' is literally 'None' when nobody called. 'is_caller_present()'
  also treats '""', '"keine"', '"nichts"' as absent.

## The show memory

The DJ does **not** know the whole show - it gets a digest of the last few
blocks ('history_limit', default 8) so it does not repeat itself. Built by
'history_digest()' from the 'history' table:

"'
Was bisher lief, nur zu deiner Info, nicht vorlesen, damit du dich nicht wiederholst:
- 14:02 "Song" von Artist – du sagtest sinngemäß: „<dj_summary>"
- 14:06 "Other" von Band (Story) (Anrufer: Hallo hier ist Max)
Bereits gegrüßt: Max.
Laufende Gags: <running_gag>.
"'

- 'dj_summary' is an extractive one-liner of what the DJ actually said last time
  (captured from the workflow's "Preview as Text" node).
- 'greeted' names and 'running_gag' come from each history row's free-form
  'meta' - continuity hooks the DJ can pick back up.
- The digest is explicitly marked *not to read out*.

The **lyrics writer never sees this** - see below.

## Story blocks

Around ':00' and ':30' the planner schedules a story segment. Same persona, plus
'STORY_ADDENDUM': talk longer (four to six sentences), lean into the weather and
the season or spin a small story, then hand off to the song. The current weather
line ('pilot/weather.py', TTS-ready, never raises) is injected here.

## Numbers are spoken as words

The TTS speaks written-out numbers far more reliably than digits, so every
number that reaches a talk prompt is spelled out first ('pilot/germanum.py'):

| Helper | '135' / a datetime → |
|---|---|
| 'cardinal(135)' | 'Einhundertfünfunddreißig' |
| 'duration_words(135)' | 'Zwei Minuten und Fünfzehn Sekunden' |
| 'clock_words(...)' | 'Dreizehn Uhr und Zwölf Minuten' |
| 'date_words(...)' | 'Zehnter September Zweitausendsechsundzwanzig' |

Keep this in mind if you rewrite the workflow prompt wording: put example
sentences with **written** numbers in the 'Prompt' node, as the shipped
workflows do.

## The lyrics writer is deliberately untouched

'lyrics_prompt(song)' and 'style_tags(song)' send the songwriter LLM and
ACE-Step **raw song facts only** - title, artist, genre, length. No persona, no
DJ text, no show memory.

"'
style_tags  -> "<artist>, <title>, <genre_desc>"     # ACE-Step "Genre Tags"
lyrics_prompt-> "Please write lyrics for ...: Song: <title> by <artist> ..."
"'

This is decision **B** in the rebuild plan and it is tested heavily. Small
models take "write lyrics that match this song" too literally and produce
pastiche; giving them room produces something of their own. The DJ does the
interpreting instead. **Do not wire the DJ into the lyrics path.**

## Tuning for a different model

Three places, in order of leverage:

1. **The workflow's Ollama nodes** - model name and sampling options live in
   'Radio_TTS.json' / 'Radio_Story.json' / 'Radio_Music+Lyrics.json'.
   Change them in the ComfyUI GUI and re-export as API format. 'config.json''s
   'ollama.model' should match.

   The 'Ollama Options' node ships every knob 'enable_X: false' by default
   (Ollama then falls back to the model's own Modelfile defaults, whatever
   those are) -- the shipped workflows now explicitly enable and set:

   | | TTS / Story | Music + Lyrics |
   |---|---|---|
   | 'temperature' | '0.5' -- precision over flair; the DJ must not improvise facts | '0.75' -- lyrics are meant to develop freely (decision B) |
   | 'top_p' / 'top_k' | '0.9' / '40' | same |
   | 'repeat_penalty' | '1.1' | same |
   | 'num_ctx' | '16384' (headroom for the growing persona + digest) | same |
   | 'num_predict' | '200' / '400' -- a hard cap, so a reasoning tangent gets cut short instead of running to completion | '800' -- a full multi-section song needs more room |

   Left disabled on purpose: 'mirostat*' (an alternative to plain
   temperature/top_p/top_k, don't run both), a fixed 'seed' (would kill the
   per-generation variety), 'stop', 'tfs_z', 'min_p'.
2. **'config/persona.md'** - voice, length, the plain-text rules.
3. **'history_limit'** in 'DJBrain' - how much show memory the DJ carries.

Quirks seen with 'qwen3.5' (the tuned target):

- verbose without a hard sentence cap; occasional stray '!'
- will leak a non-German phrase now and then despite the "German only" line
- reads spelled-out numbers well; reads digits and 'HH:MM' badly - hence
  'germanum'
- occasionally hallucinates a different time or date than the one given --
  seen as a substituted hour ("Uhrzeit: Sechzehn..." spoken back as
  "zwanzig..."), a garbled non-phrase, or a colloquial guess ("halb Vier")
  instead of the literal value. The persona now has a standalone rule against
  this (copy 'Uhrzeit:'/'Datum:' verbatim, never invent or round, omit rather
  than guess) -- it helps but does not eliminate it; check 'history.dj_summary'
  against 'history.at' in 'media/state.db' if it recurs

Larger models need less hand-holding but the plain-text / no-punctuation rules
still matter, because the output is spoken, not read.
