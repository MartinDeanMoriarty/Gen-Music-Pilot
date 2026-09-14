# Tech stack & architecture

What Gen-Music-Pilot is made of and why it is shaped the way it is. For setup
steps see [INSTALL.md](INSTALL.md).

- [Runtime & dependencies](#runtime--dependencies)
- [External services](#external-services)
- [The pipeline](#the-pipeline)
- [Package layout](#package-layout)
- [Design decisions](#design-decisions)
- [Persistence](#persistence)
- [Testing](#testing)

---

## Runtime & dependencies

- **Python 3.13+**, standard library first.
- Runtime packages: **'requests'**, **'gradio'**. That is the whole list.
- Command-line tools, expected on 'PATH': **'ffmpeg'**, **'ffprobe'**,
  **'ffplay'** (playback only).

No async framework, no ORM, no task queue, no message broker - threads, a
synchronous in-process event bus, and SQLite.

## External services

| Service | Used for | Transport |
|---|---|---|
| **ComfyUI** | runs the three generation workflows | HTTP ('/prompt', '/history', '/view', '/object_info') |
| **Ollama** | the LLM, driven *from inside* ComfyUI's 'comfyui-ollama' nodes | (via ComfyUI) |
| **OpenWeatherMap** | the weather line in story blocks | HTTP, optional |
| **Icecast** | optional stream fan-out | HTTP PUT source + '/admin/metadata' |

ComfyUI is treated as a **generator, not a store**: every output is fetched over
HTTP and copied into an app-owned library, then referenced by id.

## The pipeline

"'
          assets / user_music              ComfyUI (Ollama + TTS + ACE-Step)
                  │                                    ▲
                  ▼                                    │ one job at a time
   SongSource ─▶ Planner ─▶ Producer ──────────────────┘
                              │      (moderation, then music / or import a file)
                              ▼
                       Library (atomic copy-out, loudnorm, content-addressed)
                              │
                              ▼
                          Timeline  ── the single clock: decodes each track to
                              │         PCM and writes it to every sink at real
                        ┌─────┴─────┐   time (len/byterate seconds per chunk)
                        ▼           ▼
                   LocalSink    Broadcaster ──▶ HTTP listeners / Icecast
                   (ffplay)     (one encoder, same playhead for everyone)
"'

1. **Planner** decides the next block: talk kind (moderation / story / none),
   music source, whether to announce the time, whether a caller is waiting.
2. **Producer** keeps 'buffer_target_seconds' of audio ready, working *ahead* of
   playback. It runs the two heavy ComfyUI jobs **strictly sequentially**
   (moderation → music), because ComfyUI processes prompts serially anyway and a
   home GPU cannot do both at once.
3. **DJ brain** assembles the moderation / story prompts in Python from the
   persona, the song data, the time, and a rolling digest of the show so far.
   The lyrics prompt is left alone - the songwriter gets raw song data only.
4. **Library** copies each finished file out of ComfyUI ('.part' → 'fsync' →
   'os.replace'), probes its real duration, and for user files runs one
   'loudnorm' pass. Nothing is referenced by a ComfyUI path.
5. **Timeline** is the only clock. It owns one 'Segment' queue, decodes tracks to
   canonical PCM (s16le / 44.1 kHz / stereo) and paces the bytes to every sink in
   real time, so local audio and every stream listener stay in lockstep.
6. **Broadcaster** is just another timeline sink: one 'ffmpeg' encoder, then
   either a built-in HTTP fan-out ('pyfanout') or an Icecast source ('icecast').

If ComfyUI is unreachable the Producer stops generating, feeds fallback tracks,
and probes for recovery - the Timeline never runs dry.

## Package layout

"'
pilot/
  config.py        typed config, aggregates every validation error at once
  domain.py        Track / Segment / TrackKind / TrackState, guarded transitions
  events.py        synchronous thread-safe pub/sub bus
  store.py         SQLite: tracks, history, queue_snapshot; user_version migrations
  ffmpeg.py        probe / loudnorm / to_pcm / pcm_player wrappers
  library.py       owned media library: atomic publish, import, GC, recovery scan
  comfy/
    client.py      ComfyUI HTTP client: submit / wait / fetch / validate
    workflows.py   load API JSON, inject by node title, render moderation/story/music
  dj_brain.py      persona, show-memory digest, moderation & story prompt assembly
  germanum.py      spell numbers as German words for the TTS prompts
  weather.py       OpenWeatherMap, TTS-ready, never raises
  sources.py       GeneratedSource (assets) and UserMusicSource (files)
  metadata.py      read artist/title/genre from tags, then filename heuristics
  planner.py       policy: what the next block looks like
  producer.py      the buffer loop: plan → generate/import → publish → enqueue
  timeline.py      the single clock; Segment queue; emits events
  sinks.py         Sink protocol, LocalSink (ffplay)
  broadcast/
    base.py        Broadcaster interface + NullBroadcaster + factory
    pyfanout.py    zero-install: one encoder, stdlib HTTP server, live-edge fan-out
    icecast.py     push one encoder to an Icecast mount, reconnect on loss
  web/
    state.py       UiState: bus events → one immutable UiView
    controller.py  RadioController: owns the Application lifecycle for the UI
    ui.py          the Gradio page; a gr.Timer polls the controller
    __main__.py    'python3 -m pilot.web'
  app.py           wiring, lifecycle, signal handling
main.py            headless entrypoint
"'

## Design decisions

- **One clock.** Playback speed is set in the Timeline, not in any sink. Sinks
  receive already-paced PCM, so nothing can drift.
- **One queue.** A single 'Segment' queue (talk + music), not two parallel
  lists. Segments are persisted; a restart resumes them.
- **Identity, not paths.** Tracks have stable ids; files are published
  atomically and content-addressed. No string-matching on ComfyUI URLs.
- **Sequential generation.** Never two ComfyUI jobs at once.
- **Show memory for the DJ only.** Moderation and story see a digest of what
  aired; the lyrics writer does not, so songs can develop freely. Small models
  misread "write lyrics that match" - the DJ interprets artist/title instead.
  ([DJ.md](DJ.md) covers the prompt assembly and tuning.)
- **LLM stays in ComfyUI.** Via 'comfyui-ollama', so the workflow remains the
  single source of truth for prompts and model choice.
- **Fail soft.** ComfyUI down → fallback music. Stream won't start → log loudly,
  keep broadcasting locally. Weather down → "nicht verfügbar". The radio keeps
  going.
- **SQLite for state.** One file, one connection under a lock, WAL,
  'PRAGMA user_version' migrations.

## Persistence

'media/state.db' (SQLite):

| Table | Holds |
|---|---|
| 'tracks' | every track and its state, indexed by state |
| 'history' | one row per aired block - song, DJ summary, caller, flags - the DJ's rolling memory |
| 'queue_snapshot' | the live segment queue, so a restart resumes mid-show |

Media lives beside it: 'media/tracks/' (generated, GC'd), 'media/imported/'
(normalised user files, cached), 'media/fallback/' (never deleted).

## Testing

'./run-tests.sh' - standard library 'unittest', no 'pytest'. ComfyUI, Icecast
and an HTTP listener all have small stdlib stubs; time-paced tests use short
real audio fixtures and skip cleanly without 'ffmpeg'. The suite covers
generation slower than playback, ComfyUI timeouts, queue-desync attempts, an
empty user directory, two listeners receiving identical stream bytes, and a
whole-chain scenario run. Details: **[TESTING.md](TESTING.md)**.
