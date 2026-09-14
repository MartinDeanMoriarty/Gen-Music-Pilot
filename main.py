#!/usr/bin/env python3
"""Thin entrypoint for the headless radio. Real wiring lives in :mod:'pilot.app'.

Run "python main.py" for a headless station (Ctrl-C to stop), or
"python -m pilot.web" for the Gradio dashboard.
"""

from pilot.app import main

if __name__ == "__main__":
    main()
