"""HTTP client for a ComfyUI instance.

The legacy code polled "/history" in a "while True" with no deadline, so a
ComfyUI that died mid-job hung the radio forever. This client puts a hard
deadline on every wait, tells "unreachable" apart from "timed out" apart from
"the graph errored", and copies outputs out over HTTP ("/view") so ComfyUI's
output folder is never touched at runtime.

Progress today comes from polling "/history" (+ an "on_poll" hook for the
UI). A "/ws" progress feed can layer on later without changing this surface.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

log = logging.getLogger("pilot.comfy")

_DONE = {"success", "completed"}


class ComfyError(RuntimeError):
    """Base class for every ComfyUI failure."""


class ComfyUnreachable(ComfyError):
    """Could not reach ComfyUI (connection refused, DNS, timeout, 5xx)."""


class ComfyTimeout(ComfyError):
    """ComfyUI was reachable but the job did not finish before the deadline."""


class ComfyExecutionError(ComfyError):
    """ComfyUI rejected the prompt or the graph raised during execution."""

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.details = details


class ComfyCancelled(ComfyError):
    """The wait was cancelled (the app is shutting down)."""


@dataclass(frozen=True)
class OutputRef:
    filename: str
    subfolder: str = ""
    type: str = "output"
    node_id: str | None = None
    kind: str | None = None  # "audio", "images", ...

    def view_params(self) -> dict[str, str]:
        return {
            "filename": self.filename,
            "subfolder": self.subfolder,
            "type": self.type,
        }


@dataclass(frozen=True)
class HistoryResult:
    prompt_id: str
    status: str
    outputs: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class ComfyClient:
    def __init__(
        self,
        base_url: str,
        *,
        request_timeout: float = 10.0,
        job_timeout: float = 600.0,
        poll_interval: float = 1.5,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.job_timeout = job_timeout
        self.poll_interval = poll_interval
        self.client_id = uuid.uuid4().hex
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._object_info: dict[str, Any] | None = None

    # -- lifecycle ------------------------------------------------

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> ComfyClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- low level ----------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _get(self, path: str, **kw: Any) -> requests.Response:
        kw.setdefault("timeout", self.request_timeout)
        try:
            return self._session.get(self._url(path), **kw)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise ComfyUnreachable(f"GET {path}: {e}") from e

    def _get_json(self, path: str) -> Any:
        r = self._get(path)
        if r.status_code >= 500:
            raise ComfyUnreachable(f"GET {path}: HTTP {r.status_code}")
        r.raise_for_status()
        return r.json()

    # -- API ---------------------------------------------------

    def is_reachable(self) -> bool:
        try:
            return self._get("/system_stats").status_code == 200
        except (ComfyUnreachable, requests.RequestException):
            return False

    def submit(self, prompt: dict[str, Any]) -> str:
        """Queue a prompt graph. Returns its prompt_id."""
        payload = {"prompt": prompt, "client_id": self.client_id}
        try:
            r = self._session.post(
                self._url("/prompt"), json=payload, timeout=self.request_timeout
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            raise ComfyUnreachable(f"POST /prompt: {e}") from e

        if r.status_code == 400:
            body = _safe_json(r)
            raise ComfyExecutionError(
                f"prompt rejected: {body.get('error') or body}",
                details=body.get("node_errors"),
            )
        if r.status_code >= 500:
            raise ComfyUnreachable(f"POST /prompt: HTTP {r.status_code}")
        r.raise_for_status()

        prompt_id = _safe_json(r).get("prompt_id")
        if not prompt_id:
            raise ComfyError(f"POST /prompt: no prompt_id in response: {r.text[:200]}")
        return prompt_id

    def wait(
        self,
        prompt_id: str,
        *,
        timeout: float | None = None,
        on_poll: Callable[[float], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> HistoryResult:
        """Block until the job finishes, errors, the deadline passes, or
        "cancel" is set (:class:'ComfyCancelled')."""
        started = time.monotonic()
        deadline = started + (timeout if timeout is not None else self.job_timeout)
        ever_connected = False

        while True:
            if cancel is not None and cancel.is_set():
                raise ComfyCancelled(f"wait for {prompt_id} cancelled")
            data: Any = None
            try:
                data = self._get_json(f"/history/{prompt_id}")
                ever_connected = True
            except ComfyUnreachable:
                pass  # transient - keep trying until the deadline

            entry = data.get(prompt_id) if isinstance(data, dict) else None
            if entry:
                status = entry.get("status", {}) or {}
                st = str(status.get("status_str", "")).lower()
                if st in _DONE:
                    return HistoryResult(prompt_id, st, entry.get("outputs", {}), entry)
                if st == "error":
                    raise ComfyExecutionError(
                        f"graph execution failed for {prompt_id}",
                        details=status.get("messages"),
                    )

            if on_poll is not None:
                on_poll(time.monotonic() - started)

            if time.monotonic() >= deadline:
                if not ever_connected:
                    raise ComfyUnreachable(
                        f"ComfyUI unreachable for {prompt_id} after "
                        f"{time.monotonic() - started:.0f}s"
                    )
                raise ComfyTimeout(
                    f"{prompt_id} did not finish within "
                    f"{time.monotonic() - started:.0f}s"
                )
            if cancel is not None:
                if cancel.wait(self.poll_interval):
                    raise ComfyCancelled(f"wait for {prompt_id} cancelled")
            else:
                time.sleep(self.poll_interval)

    def outputs(self, result: HistoryResult | str) -> list[OutputRef]:
        """Every downloadable file a finished job produced."""
        if isinstance(result, HistoryResult):
            outputs = result.outputs
        else:
            data = self._get_json(f"/history/{result}")
            outputs = (data.get(result) or {}).get("outputs", {})

        refs: list[OutputRef] = []
        for node_id, node_out in outputs.items():
            if not isinstance(node_out, dict):
                continue
            for kind, entries in node_out.items():
                if not isinstance(entries, list):
                    continue
                for e in entries:
                    if isinstance(e, dict) and e.get("filename"):
                        refs.append(
                            OutputRef(
                                filename=e["filename"],
                                subfolder=e.get("subfolder", ""),
                                type=e.get("type", "output"),
                                node_id=node_id,
                                kind=kind,
                            )
                        )
        return refs

    def fetch(self, ref: OutputRef, dest: str | Path) -> Path:
        """Stream an output file from "/view" to "dest"."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._session.get(
                self._url("/view"),
                params=ref.view_params(),
                stream=True,
                timeout=self.request_timeout,
            ) as r:
                if r.status_code >= 500:
                    raise ComfyUnreachable(f"GET /view: HTTP {r.status_code}")
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
        except (requests.ConnectionError, requests.Timeout) as e:
            dest.unlink(missing_ok=True)
            raise ComfyUnreachable(f"GET /view: {e}") from e

        if dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            raise ComfyError(f"/view returned an empty file for {ref.filename}")
        return dest

    def object_info(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._object_info is None or refresh:
            self._object_info = self._get_json("/object_info")
        return self._object_info

    def validate_workflow(self, prompt: dict[str, Any]) -> list[str]:
        """Return the sorted class_types the prompt uses that ComfyUI does not
        have installed."""
        available = set(self.object_info().keys())
        used = {
            node["class_type"]
            for node in prompt.values()
            if isinstance(node, dict) and "class_type" in node
        }
        return sorted(used - available)


def _safe_json(r: requests.Response) -> dict[str, Any]:
    try:
        body = r.json()
        return body if isinstance(body, dict) else {"body": body}
    except ValueError:
        return {}
