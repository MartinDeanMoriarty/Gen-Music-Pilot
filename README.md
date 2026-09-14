# 📻 Gen-Music-Pilot

An autonomous radio station driven by an AI moderator - a DJ persona that
announces every song, remembers the show so far, and can take a caller, live
and unattended. The moderator is the constant across modes: point the station
at your own MP3s, let it generate music, or mix both, and the show carries on
the same either way.

When music generation is on, it runs through its own
[ComfyUI](https://www.comfy.org/) workflow - [ACE-Step](https://github.com/ace-step/ACE-Step/)
turning LLM-written lyrics into a track, decoupled enough from the moderator
to run an entirely different model. Everything is queued and played
back-to-back on a single timeline, and can be streamed to other listeners.
The workflows stay yours to retune.

## What it does

- **Autonomous DJ** - moderation with a rolling memory of the show so far (what
  it said, callers, running gags). Numbers are spoken as words.
- **Generative music** - ACE-Step from LLM-written lyrics, left uninfluenced by
  the DJ so the lyrics can go their own way.
- **Your own music** - switch to real MP3s under 'user_music/'; the DJ reads
  artist / title / genre from the tags and announces them, with no music
  generation for those blocks.
- **Story time** - a longer segment around :00 and :30 past the hour, with
  weather.
- **Streaming** - one encoder, many listeners at the same playhead
  (zero-install HTTP, or push to an Icecast mount).
- **Dashboard** - a small Gradio page: what is on air, the buffer, recent
  history, a mode switch, and a "caller" box.
- **Never dead air** - if ComfyUI is down it airs fallback music and recovers on
  its own.

## Run it

You need Python 3.13+, the 'ffmpeg' tools, and a running ComfyUI + Ollama. Full
steps, including the ComfyUI side, are in **[docs/INSTALL.md](docs/INSTALL.md)**.

"'bash
cp config/config.example.json config/config.json   # then edit it
./start.sh                                          # or: python3 -m pilot.web
"'

Then open <http://127.0.0.1:7860>.

'./start.sh' sets up '.venv' and activates it for you. Running the commands by
hand instead? Activate '.venv' first ('source .venv/bin/activate') - see
[docs/INSTALL.md](docs/INSTALL.md).

- **Dashboard:** 'python3 -m pilot.web'
- **Headless:** 'python3 main.py' (no UI, Ctrl-C to stop)

## How it is built

Python 3.13+, standard library where it can be, two runtime dependencies
('requests', 'gradio'). External tools: the 'ffmpeg' suite, ComfyUI, Ollama.
The architecture and the design decisions behind it are in
**[docs/TECHSTACK.md](docs/TECHSTACK.md)**.

## Tuning the DJ

The moderator's voice, the show memory, the number-spelling, and why the lyrics
writer is left alone: **[docs/DJ.md](docs/DJ.md)**.

## Rebuild History

Gen-Music-Pilot started as an experiment, could a small local LLM, a
cloned voice, and ACE-Step run an actual radio station - it worked, then
got shelved for a while. This is a ground-up rewrite of that original
project: same idea, the same ComfyUI pipeline, built with the engineering it
deserved the first time around.

## Credits

The generation pipeline stands on a pile of ComfyUI custom nodes - the list is
in [docs/INSTALL.md](docs/INSTALL.md#comfyui-custom-nodes). Thanks to all of
their authors; without them this would not work.

## License

[Creative Commons Attribution 4.0 International](LICENSE) (CC BY 4.0).
