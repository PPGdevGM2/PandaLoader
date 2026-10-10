"""Entry point for the frozen PandaLoader.exe (PyInstaller, windowed).

Runs the tray app. Any CLI arguments are passed through, so
``PandaLoader.exe --port 7000`` or ``PandaLoader.exe queue.rq.json`` work.
"""
import sys

from pandaloader.__main__ import main

if __name__ == "__main__":
    sys.exit(main(["tray"] + sys.argv[1:]))
