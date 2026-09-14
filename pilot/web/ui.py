"""The Gradio dashboard.

"build_ui(controller)" returns a "gr.Blocks". A "gr.Timer" polls
"controller.view()" once a second and repaints the live panels -- no injected
refresh JS and no hidden refresh button, unlike the pre-rebuild UI.

The render helpers ("_render" and the "_*_md" formatters) are pure functions
of a :class:'~pilot.web.controller.ControllerView', so the refresh path is
testable without a browser.
"""

from __future__ import annotations

import gradio as gr

from pilot import __version__
from pilot.web.controller import MODE_CHOICES, ControllerView, RadioController

_UNDERRUN_SHOW_SECONDS = 300

_LABEL = {
    "moderation": "🎙 Moderation",
    "story": "📖 Story",
    "music_gen": "🎵 Generated",
    "music_user": "💿 Collection",
    "fallback": "🔁 Fallback",
    "gen_music": "🎵 Generated",
    "user_music": "💿 Collection",
}


def _label(kind: str | None) -> str:
    return _LABEL.get(kind or "", kind or "-")


def _status_md(v: ControllerView) -> str:
    if not v.on_air:
        return f"### ⚪ Off air\n{v.status or 'idle'}"
    phase = f"{v.phase} - {v.detail}" if v.detail else v.phase
    return f"### 🔴 On air\n{phase}"


def _now_md(v: ControllerView) -> str:
    if not v.now_playing:
        return "**Now playing:** -"
    tail = "" if v.now_active else " _(ready)_"
    since = f" · since {v.now_since:%H:%M}" if v.now_since else ""
    return f"**Now playing:** {v.now_playing}{tail}\n\n{_label(v.now_kind)}{since}"


def _buffer_md(v: ControllerView) -> str:
    target = v.buffer_target_seconds or 0.0
    return (
        f"**Puffer:** {v.buffered_seconds:.0f}s / {target:.0f}s"
        f" · **Queue:** {v.queue_segments} Blocks ({v.queue_seconds:.0f}s)"
        f" · **send:** {v.segments_aired}"
    )


def _stream_md(v: ControllerView) -> str:
    if not v.streaming or not v.listen_url:
        return "**Stream:** off"
    return f"**Stream:** [{v.listen_url}]({v.listen_url}) · {v.listeners} Listeners"


def _comfy_md(v: ControllerView) -> str:
    if v.comfy_reachable is None:
        return "**ComfyUI:** -"
    if v.comfy_reachable:
        return "**ComfyUI:** 🟢 Online"
    return "**ComfyUI:** 🔴 Offline"


def _underrun_md(v: ControllerView) -> str:
    u = v.underrun
    if u is None:
        return ""
    age = int(u.age().total_seconds())
    if age > _UNDERRUN_SHOW_SECONDS:
        return ""
    what = u.detail or f"{u.queue_seconds:.0f}s Audio left"
    return f"⚠️ **Underrun** for {age}s - {what}"


def _history_md(v: ControllerView) -> str:
    if not v.history:
        return "_Nothing aired yet._"
    rows = ["| Time | Block | DJ |", "|---|---|---|"]
    for h in v.history:
        said = " ".join((h.dj_summary or "").split()) or "-"
        if len(said) > 200:
            said = said[:199] + "…"
        subject = h.title or _label(h.kind)
        if h.caller:
            subject += " 📞"
        rows.append(f"| {h.at:%H:%M} | {subject} | {said} |")
    return "\n".join(rows)


def _recent_md(v: ControllerView) -> str:
    if not v.recent:
        return "_Nothing played yet._"
    out = []
    for r in v.recent:
        tail = "" if r.completed else " _(aborted)_"
        out.append(f"- {r.at:%H:%M} {_label(r.kind)} - {r.title}{tail}")
    return "\n".join(out)


def _render(
    view: ControllerView,
) -> tuple[str, str, str, str, str, str, str, str, str]:
    """The timer's output tuple, in the order of the "live" component list."""
    return (
        _status_md(view),
        _now_md(view),
        _buffer_md(view),
        _stream_md(view),
        _comfy_md(view),
        _underrun_md(view),
        _history_md(view),
        _recent_md(view),
        view.mode,
    )


def build_ui(controller: RadioController) -> gr.Blocks:
    with gr.Blocks(
        title=f"Gen-Music-Pilot (Beta)", analytics_enabled=False
    ) as demo:
        gr.Markdown(f"# 📻 Gen-Music-Pilot **`BETA`**")

        status = gr.Markdown()
        with gr.Row():
            now = gr.Markdown()
            buffer = gr.Markdown()
            stream = gr.Markdown()
            comfy = gr.Markdown()
        underrun = gr.Markdown()

        with gr.Row():
            start_btn = gr.Button("▶️ Start", variant="primary")
            stop_btn = gr.Button("⏹️ Stop")
        action_msg = gr.Textbox(
            label="Informational messages", interactive=False,
            placeholder="Appears after Start/Stop - also error messages",
        )
        gr.Markdown(
            "ℹ️ After startup, the first generation may take one to two minutes. "
            "ComfyUI generates the narration and music sequentially."
        )

        with gr.Row():
            mode = gr.Radio(
                list(MODE_CHOICES), value="generative", label="Music-Mode", scale=2
            )
            mode_msg = gr.Textbox(label="Modus", interactive=False, scale=1)

        with gr.Row():
            caller = gr.Textbox(
                label="📞 Caller", lines=2, scale=2,
                placeholder="Hello, this is ... "
                "(please say hello to my mother | it's my birthday)",
            )
            inject_btn = gr.Button("💉 Inject", scale=1)
        inject_msg = gr.Textbox(label="Caller-Info", interactive=False)

        gr.Markdown("### 🗒️ History")
        history = gr.Markdown()
        gr.Markdown("### ⏮️ Previously")      
        
        recent = gr.Markdown()

        gr.Markdown(f'<a href="https://github.com/MartinDeanMoriarty/Gen-Music-Pilot" target="_blank">**Gen-Music-Pilot**</a> `v{__version__}` 2026')
        
        live = [status, now, buffer, stream, comfy, underrun, history, recent, mode]

        def refresh() -> tuple:
            return _render(controller.view())

        def initial_mode_status() -> str:
            # a one-time read at page load, not on the timer -- so a later
            # click's success/error message isn't overwritten a second later
            return f"current: {controller.view().mode}"

        start_btn.click(controller.start, outputs=action_msg).then(
            refresh, outputs=live
        )
        stop_btn.click(controller.stop, outputs=action_msg).then(
            refresh, outputs=live
        )
        inject_btn.click(controller.inject_caller, inputs=caller, outputs=inject_msg)
        inject_btn.click(lambda: "", outputs=caller)
        mode.select(controller.set_mode, inputs=mode, outputs=mode_msg)

        gr.Timer(1.0).tick(refresh, outputs=live)
        demo.load(refresh, outputs=live)
        demo.load(initial_mode_status, outputs=mode_msg)

    return demo
