# Installing Gen-Music-Pilot

This is not a one-click app. The Python side is small; the work is in the
ComfyUI setup. Budget an afternoon the first time.

- [1. Prerequisites](#1-prerequisites)
- [2. ComfyUI](#2-comfyui)
- [3. Ollama](#3-ollama)
- [4. The app](#4-the-app)
- [5. Configuration](#5-configuration)
- [6. User-music mode](#6-user-music-mode)
- [7. First run](#7-first-run)
- [8. Streaming](#8-streaming)
- [9. Troubleshooting](#9-troubleshooting)

---

## 1. Prerequisites

| Need | Notes |
|---|---|
| **Python 3.13+** | 'python3 --version' |
| **ffmpeg / ffprobe / ffplay** | on 'PATH'. 'ffplay' is only needed for local audio playback. |
| **ComfyUI** | a working manual install, reachable at 'http://127.0.0.1:8188' |
| **Ollama** | run through ComfyUI's 'comfyui-ollama' nodes; a chat model pulled |
| **OpenWeatherMap key** | optional, for the weather line in story blocks |

The app talks to ComfyUI and Ollama over HTTP only - they can run anywhere you
can reach, not just localhost.

---

## 2. ComfyUI

### Install

Follow ComfyUI's own
[manual install](https://github.com/comfyanonymous/ComfyUI#manual-install-windows-linux).

### ComfyUI custom nodes

Checked against the node classes the three shipped workflows actually use.
Install these with [ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager)
(open a workflow, *Manager → Install Missing Custom Nodes*):

- [ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager) - itself, for installing the rest
- [TTS Audio Suite](https://github.com/diodiogod/TTS-Audio-Suite) - ChatterBox voice nodes
- [comfyui-ollama](https://github.com/stavsap/comfyui-ollama) - the LLM nodes
- [ComfyUI-Unload-Model](https://github.com/SeanScripts/ComfyUI-Unload-Model) - frees VRAM between jobs
- [ComfyUI-Easy-Use](https://github.com/yolain/ComfyUI-Easy-Use) - only for its 'easy cleanGpuUsed' node

### Models

- **ACE-Step** checkpoint and its dependencies -
  [ACE-Step](https://github.com/ace-step/ACE-Step/)
- A **TTS / ChatterBox** voice (TTS Audio Suite). A cloned voice is optional.
- An **Ollama** chat model (see [section 3](#3-ollama)).

### The three workflows

'workflows/' holds **API-format** exports (ComfyUI: *Save (API Format)*, not the
regular *Save*). The app loads them, injects a few values by node **title**,
submits, and copies the audio back out.

| File | Role | Output |
|---|---|---|
| 'Radio_TTS.json' | moderation - Ollama writes it, TTS speaks it | spoken MP3 + the text |
| 'Radio_Story.json' | the longer :00 / :30 story segment (own voice, weather) | spoken MP3 + the text |
| 'Radio_Music+Lyrics.json' | Ollama writes lyrics, ACE-Step makes the music | music MP3 |

#### The titled-node contract

Keep these '_meta.title' values on the input nodes - the app finds them by
title and 'inject_inputs' **fails loudly** naming any that is missing:

| Workflow | Required titled nodes | Also needs |
|---|---|---|
| TTS / Story | 'System Prompt', 'Prompt' | a save node ('SaveAudioAdvanced' / 'SaveAudioMP3' / 'SaveAudio'); a **"Preview as Text"** ('PreviewAny') on the Ollama text output so the app can capture what the DJ said |
| Music + Lyrics | 'Prompt', 'Genre Tags', 'Length' | a save node |

What the app injects, per block:

- **'System Prompt'** ← 'config/persona.md', verbatim (TTS/Story only).
- **'Prompt'** ← the DJ-brain assembly for TTS/Story; the raw lyrics request for
  Music. See [DJ.md](DJ.md).
- **'Genre Tags'** ← '"<artist>, <title>, <genre>"' for ACE-Step.
- **'Length'** ← seconds (a float).
- Each save node's 'filename_prefix' ← 'audio/GMP/<Kind>/<Kind>'.

The Music workflow's own 'System Prompt' (the songwriter persona) is **left
alone** - decision B, the lyrics writer stays yours to shape.

The Ollama model, the ChatterBox device ('cuda'), temperatures, and the voice
all live **in the workflow nodes**, not in 'config.json' - set them in the GUI.
Numbers in the TTS/Story prompt wording should be written as words (the voice
speaks "dreizehn Uhr" better than "13:00").

#### Bring it up

1. Start ComfyUI, open each of the three workflows.
2. *Manager → Install Missing Custom Nodes*, restart, reload.
3. Fix any red nodes - usual suspects: TTS-Audio-Suite ↔ numpy version,
   'comfyui-ollama' v1→v2 node rename, KJNodes, JakeUpgrade.
4. Set your Ollama model, voice, and any wording. Confirm the titled nodes above
   still have their titles.
5. **Run each workflow once** and check it produces audio.
6. **Re-export as *Save (API Format)*** over the same file in 'workflows/'.
7. Start the app - 'python3 -m pilot.web'. Its startup validator checks every
   node class in each workflow against your ComfyUI and, if one is missing,
   aborts with:

   "'
   ComfyUI cannot run these workflows:
     moderation:
       - missing node: SomeNode
   "'

   Install that node (or its pack) and retry. 'PILOT_SKIP_COMFY_CHECK=1'
   bypasses the check if you know better.

---

## 3. Ollama

Install [Ollama](https://ollama.com/) ('curl -fsSL https://ollama.com/install.sh | sh')
and pull a model:

"'bash
ollama pull qwen3.5:latest
"'

Small local models are what this is tuned for. Set the same model name in the
Ollama nodes of all three workflows and in 'config.json' ('ollama.model').

---

## 4. The app

"'bash
git clone https://github.com/MartinDeanMoriarty/Gen-Music-Pilot.git
cd Gen-Music-Pilot
cp config/config.example.json config/config.json     # then edit - see below
"'

Then either let 'start.sh' manage a virtualenv:

"'bash
chmod +x start.sh
./start.sh
"'

or do it by hand:

"'bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m pilot.web        # dashboard at http://127.0.0.1:7860
# or: python3 main.py       # headless
"'

('python3' works whether or not '.venv' is active; some systems have no plain
'python', only 'python3'.)

'config/config.json' is git-ignored (it holds your API keys). The tracked
template is 'config/config.example.json'.

---

## 5. Configuration

Every section is optional except 'comfy_url' and 'ollama'. Defaults are shown.

| Key | Default | Meaning |
|---|---|---|
| 'comfy_url' | - | ComfyUI base URL |
| 'ollama.url' / 'ollama.model' | '127.0.0.1:11434' / - | Ollama endpoint and model name |
| 'mode' | 'generative' | 'generative', 'user', or 'mixed' |
| 'local_audio' | 'true' | play out loud on this machine via 'ffplay' |
| 'buffer_target_seconds' | '420' | how far ahead to generate |
| 'assets_dir' | 'assets' | 'names.json', 'artists.json', 'genres.json' for the generator |
| 'music.min/max_length_seconds' | '120' / '300' | requested track length range |
| 'station.name' / 'station.persona_file' | 'Gen-Music-Pilot' / 'config/persona.md' | the DJ persona |
| 'weather.apikey' / 'weather.location' | empty | [OpenWeatherMap](https://home.openweathermap.org/api_keys); no key → no weather |
| 'retention.max_age_minutes' / 'max_tracks' | '60' / '40' | played-track cleanup |
| 'stream.*' | disabled | see [section 8](#8-streaming) |
| 'web.host' / 'web.port' | '127.0.0.1' / '7860' | dashboard bind |
| 'web.share' | 'false' | Gradio public share link |
| 'web.auto_start' | 'false' | go on air the moment the dashboard loads |
| 'workflows.*' | the three files above | paths to the API workflow JSON |
| 'user_music_dirs' | none | see [section 6](#6-user-music-mode) |

---

## 6. User-music mode

Point the app at folders of real audio files:

"'json
"mode": "user",
"user_music_dirs": [
  { "name": "My Collection", "path": "user_music/collection" }
]
"'

- 'mode: "user"' plays only your files; 'mixed' alternates with generated music;
  'generative' ignores them.
- Supported: '.mp3 .m4a .flac .ogg .opus .wav .aac .wma'. Artist / title / genre
  come from the file tags, falling back to the filename.
- On import each file is copied into 'media/imported/' and loudness-normalised
  once (cached). Your originals are never touched.
- The DJ still writes and speaks an intro for every file - only the music
  generation is skipped.
- An empty or missing folder is a warning, not a crash, in 'mixed'; in 'user' it
  stops startup.

'user_music/' is git-ignored - your library never leaves your machine.

---

## 7. First run

- Startup validates the workflows against ComfyUI and prints a config summary.
- The first block takes a couple of minutes (moderation + a full music
  generation, run one after the other - ComfyUI processes prompts serially).
- If ComfyUI is unreachable, the app still goes on air on **fallback music**
  from 'media/fallback/' and keeps retrying; drop a few MP3s there so the first
  minutes are not silent.
- 'PILOT_LOG_LEVEL=DEBUG' for a verbose trace. 'PILOT_SKIP_COMFY_CHECK=1' starts
  without the workflow validation.

---

## 8. Streaming

"'json
"stream": { "enabled": true, "backend": "pyfanout", "bind": "0.0.0.0:8080",
            "codec": "mp3", "bitrate_kbps": 128 }
"'

- **'pyfanout'** (default) - the app runs its own HTTP stream server, no extra
  software. Point VLC / a browser at 'http://<host>:8080/'. Every listener sits
  at the same playhead.
- **'icecast'** - set 'backend: "icecast"' and fill in 'stream.icecast'
  ('host', 'port', 'mount', 'source_password'); the app sources one encoder to
  that mount and reconnects on its own if the mount drops.

The stream carries ICY now-playing metadata for the current song. A streaming
failure is logged loudly but never takes the radio down.

'bind: "0.0.0.0:…"' opens the stream to your whole network (handy to test from
a phone or another room) - anyone who can reach that port can listen. Use
'127.0.0.1:…' to keep it local-only, or a firewall if '0.0.0.0' is intentional.

---

## 9. Troubleshooting

| Symptom | Look at |
|---|---|
| startup aborts naming a node | install it (ComfyUI-Manager) or re-export the workflow |
| "GUI export" error | you saved the workflow as *Save*, not *Save (API Format)* |
| DJ says digits oddly / wrong language | tune the workflow prompt; small models are picky |
| 'WARNING pilot.sinks.local: local audio disabled: ffplay exited (code 1)' | harmless if you're listening over the stream / VLC - it just means 'ffplay' couldn't open an audio device here (no soundcard, remote box, PulseAudio not running). The rest of the app is unaffected; set 'local_audio: false' to silence the warning, or fix 'ffplay -f s16le -ar 44100 -ac 2 -i /dev/null' locally |
| no sound anywhere | 'ffplay' on 'PATH'? try a stream backend instead |
| silence at startup | ComfyUI down and 'media/fallback/' empty |
| weather always "nicht verfügbar" | key not active yet (can take ~2 h), or wrong location |

The app is meant to keep running through failures - check the logs before
assuming it is stuck.
