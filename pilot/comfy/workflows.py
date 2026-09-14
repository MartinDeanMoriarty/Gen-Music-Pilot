"""Typed wrappers around the three ComfyUI workflows.

The workflows were hand-cleaned in ComfyUI down to a few titled input nodes;
Python assembles every value that used to be stitched together in the graph.
These wrappers inject those values by node title, run the graph, and copy the
one audio output into the library as a READY :class:'Track'.

Injection is **strict**: every value must land on a node, otherwise
:class:'WorkflowError' names the missing title -- the old silent breakage
("any error in a workflow will break the app without a plausible error message")
becomes a specific, actionable message.

Node titles the shipped workflows expose (owner-cleaned in the ComfyUI GUI):

* moderation / story -- "System Prompt", "Prompt"
* music              -- "System Prompt" (kept in-graph, not injected),
                        "Prompt" (lyrics request), "Genre Tags" (ACE-Step
                        style string), "Length" (seconds, a "PrimitiveFloat")

plus a "SaveAudioAdvanced" node in each (the renderer sets its
"filename_prefix" to "audio/GMP/<Kind>/<Kind>"). moderation / story also
carry a "PreviewAny" ("Preview as Text") node on the Ollama output -- its
"{"text": [...]}" result is what the DJ actually said, captured into
"track.meta["dj_text"]" and summarised into the show memory.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from pilot import ffmpeg
from pilot.comfy.client import ComfyClient, ComfyExecutionError
from pilot.config import WorkflowsConfig
from pilot.domain import SongData, Track, TrackKind
from pilot.germanum import duration_words
from pilot.library import Library

log = logging.getLogger("pilot.comfy.workflows")

FILENAME_PREFIX = "filename_prefix"
_SAVE_NODES = ("SaveAudioAdvanced", "SaveAudioMP3", "SaveAudio")

_PREFIX = {
    TrackKind.MODERATION: "audio/GMP/TTS/TTS",
    TrackKind.STORY: "audio/GMP/Story/Story",
    TrackKind.MUSIC_GEN: "audio/GMP/Music/Music",
}

OnPoll = Callable[[float], None]


class WorkflowError(RuntimeError):
    """The workflow file is the wrong shape or is missing an expected node."""


def load_api_workflow(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise WorkflowError(f"workflow not found: {path}") from e
    except json.JSONDecodeError as e:
        raise WorkflowError(f"{path}: invalid JSON: {e}") from e
    if not isinstance(raw, dict) or isinstance(raw.get("nodes"), list):
        raise WorkflowError(
            f"{path} looks like a GUI export -- use ComfyUI: File > Export (API)"
        )
    return raw


def inject_inputs(prompt: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of "prompt" with values placed by node title.

    "values[FILENAME_PREFIX]" goes on the "SaveAudioMP3" node; every other
    key must match at least one node's "_meta.title".
    """
    out = copy.deepcopy(prompt)
    satisfied: set[str] = set()

    for node in out.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue

        if node.get("class_type") in _SAVE_NODES and FILENAME_PREFIX in values:
            inputs[FILENAME_PREFIX] = values[FILENAME_PREFIX]
            satisfied.add(FILENAME_PREFIX)

        title = str(node.get("_meta", {}).get("title", "")).strip()
        if title and title in values:
            _set_primary_input(inputs, values[title])
            satisfied.add(title)

    missing = sorted(set(values) - satisfied)
    if missing:
        raise WorkflowError(
            "workflow has no node for: "
            + ", ".join(missing)
            + (" (no Save Audio node)" if FILENAME_PREFIX in missing else "")
        )
    return out


def _extract_text(outputs: dict[str, Any]) -> str | None:
    """The DJ's spoken words, from a "PreviewAny" / "Preview as Text" node
    whose "/history" output is "{"text": ["..."]}"."""
    for node_out in outputs.values():
        if isinstance(node_out, dict) and isinstance(node_out.get("text"), list):
            joined = "\n".join(
                str(t) for t in node_out["text"] if isinstance(t, str)
            ).strip()
            if joined:
                return joined
    return None


def _set_primary_input(inputs: dict[str, Any], value: Any) -> None:
    if "value" in inputs:
        inputs["value"] = value
        return
    for key, current in inputs.items():
        if isinstance(current, list):
            continue  # a [node_id, slot] link, not a literal
        if isinstance(current, (str, int, float)) or current in ("", None):
            inputs[key] = value
            return
    raise WorkflowError(f"node has no settable literal input among {list(inputs)}")


def validate_workflows(
    client: ComfyClient, workflows: WorkflowsConfig
) -> dict[str, list[str]]:
    """Check each workflow against ComfyUI's installed nodes.

    Returns "{workflow_name: [problems]}" for the workflows that have any --
    an empty dict means all three are runnable.
    """
    report: dict[str, list[str]] = {}
    for name, path in (
        ("moderation", workflows.moderation),
        ("story", workflows.story),
        ("music", workflows.music),
    ):
        try:
            prompt = load_api_workflow(path)
        except WorkflowError as e:
            report[name] = [str(e)]
            continue
        missing = client.validate_workflow(prompt)
        if missing:
            report[name] = [f"missing node: {m}" for m in missing]
    return report


def lyrics_prompt(song: SongData) -> str:
    """The request for the songwriter LLM (the workflow's "Prompt" node).

    Raw song facts only -- the "how" lives in the workflow's own System Prompt
    node (decision B: the lyrics writer stays uninfluenced by the DJ).
    """
    return (
        "Please write lyrics for the following song:\n\n"
        f"Song: {song.song_name} by {song.artist}\n"
        f"Genre: {song.genre_desc}\n"
        f"Length: {duration_words(song.length_s)}\n\n"
        "Please don't forget: Only your lyrics with structure blocks! "
        "This is real music!"
    )


def style_tags(song: SongData) -> str:
    """The comma-separated style string for ACE-Step's "Genre Tags" node."""
    return f"{song.artist}, {song.song_name}, {song.genre_desc}"


class WorkflowRenderer:
    def __init__(
        self,
        client: ComfyClient,
        library: Library,
        workflows: WorkflowsConfig,
        *,
        cancel: threading.Event | None = None,
    ) -> None:
        self.client = client
        self.library = library
        self.workflows = workflows
        self.cancel = cancel  # set -> in-flight waits raise ComfyCancelled

    # -- public API --------------------------------------------

    def music(
        self,
        song: SongData,
        *,
        genre_tags: str | None = None,
        lyrics: str | None = None,
        on_poll: OnPoll | None = None,
    ) -> Track:
        track = Track.planned(
            TrackKind.MUSIC_GEN,
            song_name=song.song_name,
            artist=song.artist,
            genre_desc=song.genre_desc,
        )
        prompt = inject_inputs(
            load_api_workflow(self.workflows.music),
            {
                "Prompt": lyrics or lyrics_prompt(song),
                "Genre Tags": genre_tags or style_tags(song),
                "Length": float(song.length_s),
                FILENAME_PREFIX: self._prefix(track),
            },
        )
        return self._render(track, prompt, on_poll)

    def moderation(
        self,
        *,
        system: str,
        prompt: str,
        meta: dict[str, Any] | None = None,
        on_poll: OnPoll | None = None,
    ) -> Track:
        return self._talk(TrackKind.MODERATION, system, prompt, meta, on_poll)

    def story(
        self,
        *,
        system: str,
        prompt: str,
        meta: dict[str, Any] | None = None,
        on_poll: OnPoll | None = None,
    ) -> Track:
        return self._talk(TrackKind.STORY, system, prompt, meta, on_poll)

    # -- internals -------------------------------------------

    def _talk(
        self,
        kind: TrackKind,
        system: str,
        user_prompt: str,
        meta: dict[str, Any] | None,
        on_poll: OnPoll | None,
    ) -> Track:
        track = Track.planned(kind, **(meta or {}))
        path = (
            self.workflows.moderation
            if kind is TrackKind.MODERATION
            else self.workflows.story
        )
        prompt = inject_inputs(
            load_api_workflow(path),
            {
                "System Prompt": system,
                "Prompt": user_prompt,
                FILENAME_PREFIX: self._prefix(track),
            },
        )
        return self._render(track, prompt, on_poll)

    def _prefix(self, track: Track) -> str:
        return _PREFIX.get(track.kind, f"audio/GMP/{track.kind.value}")

    def _render(
        self, track: Track, prompt: dict[str, Any], on_poll: OnPoll | None
    ) -> Track:
        store = self.library.store
        store.upsert_track(track)
        track.mark_generating()
        store.upsert_track(track)
        try:
            prompt_id = self.client.submit(prompt)
            track.comfy_prompt_id = prompt_id
            store.upsert_track(track)

            result = self.client.wait(prompt_id, on_poll=on_poll, cancel=self.cancel)

            spoken = _extract_text(result.outputs)  # from a "Preview as Text" node
            if spoken:
                track.meta["dj_text"] = spoken

            refs = self.client.outputs(result)
            audio = [r for r in refs if r.kind == "audio"] or refs
            if not audio:
                raise ComfyExecutionError(
                    f"{track.kind.value} job {prompt_id} produced no output file"
                )

            fd, tmp_name = tempfile.mkstemp(dir=self.library.tmp_dir, suffix=".fetch")
            os.close(fd)
            fetch_tmp = Path(tmp_name)
            try:
                self.client.fetch(audio[0], fetch_tmp)
                dest = self.library.publish_atomic(track.id, fetch_tmp)
            finally:
                fetch_tmp.unlink(missing_ok=True)

            probe = ffmpeg.probe(dest)
            track.mark_ready(dest, probe.duration_s)
            store.upsert_track(track)
            log.info(
                "rendered %s %s (%.1fs) -> %s",
                track.kind.value,
                track.id[:8],
                probe.duration_s,
                dest.name,
            )
            return track
        except Exception as e:
            if not track.is_terminal:
                track.mark_failed(f"{type(e).__name__}: {e}")
                store.upsert_track(track)
            raise
