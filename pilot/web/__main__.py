""""python -m pilot.web" -- the Gradio dashboard.

Builds a :class:'~pilot.web.controller.RadioController' and the UI and serves it
on "web.host:web.port" from the config. "web.auto_start" puts the radio on
air immediately; otherwise press Start in the browser. Ctrl-C stops the radio,
then the server.
"""

from __future__ import annotations

import logging

from pilot.app import setup_logging
from pilot.config import ConfigError, load_config
from pilot.web.controller import RadioController
from pilot.web.ui import build_ui
from pathlib import Path

log = logging.getLogger("pilot.web")


def main() -> None:
    setup_logging()
    try:
        config = load_config()
    except ConfigError as e:
        log.error("%s", e)
        raise SystemExit(1) from None

    controller = RadioController()
    if config.web.auto_start:
        log.info("web.auto_start -> %s", controller.start())

    demo = build_ui(controller)
    try:
        demo.launch(
            server_name=config.web.host,
            server_port=config.web.port,
            share=config.web.share,
            show_error=True,
            quiet=True,
            favicon_path= Path(__file__).resolve().parents[1] / "static" / "favicon_server.png",
        )
    except KeyboardInterrupt:  # gradio usually swallows this itself
        pass
    finally:
        log.info("shutting down")
        controller.stop()
        demo.close()


if __name__ == "__main__":
    main()
