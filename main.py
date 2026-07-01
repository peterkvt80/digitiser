#!/usr/bin/env python3
"""
main.py — Teletext Design Sheet Digitiser
Entry point: wires camera, hardware, LCD, gallery, and GUI together.

Usage:
    python3 main.py [--debug] [--no-hardware] [--no-lcd]

Dependencies (install on Pi):
    sudo apt install tesseract-ocr python3-tk
    pip install opencv-python pillow pytesseract RPi.GPIO RPLCD picamera2
"""

import argparse
import logging
import sys
import io
from pathlib import Path

# Force the Windows terminal to interpret text as UTF-8 (like Linux does)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ── Argument parsing ──────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Teletext Design Sheet Digitiser")
parser.add_argument("--debug",        action="store_true", help="Verbose logging")
parser.add_argument("--no-hardware",  action="store_true", help="Disable GPIO")
parser.add_argument("--no-lcd",       action="store_true", help="Disable LCD")
args = parser.parse_args()

# ── Logging ───────────────────────────────────────────────────────────────────

# ── Logging ───────────────────────────────────────────────────────────────────
#
# On Windows, a plain logging.StreamHandler(sys.stdout) silently inherits
# the *console's* active codepage (often cp1252), not UTF-8 — even though
# sys.stdout itself was just wrapped as UTF-8 above. cp1252 has no glyph
# for characters like '→' (U+2192), so any log message containing one
# raises a UnicodeEncodeError inside logging's internal emit(). Python's
# logging module catches that error itself (it never crashes the app),
# but it prints a "--- Logging error ---" traceback and the original log
# line is silently dropped.
#
# Fix (Option A): explicitly construct the console StreamHandler around
# a text stream that is forced to UTF-8, with errors="backslashreplace"
# so an unencodable character degrades gracefully instead of raising.

_console_stream = io.TextIOWrapper(
    sys.stdout.buffer,
    encoding="utf-8",
    errors="backslashreplace",
    line_buffering=True,
)
_console_handler = logging.StreamHandler(_console_stream)

_file_handler = logging.FileHandler(
    Path.home() / "tdi620_digitiser.log",
    encoding="utf-8",
)

logging.basicConfig(
    level=logging.DEBUG if args.debug else logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[_console_handler, _file_handler],
)
log = logging.getLogger(__name__)
log.info("TDI620 Digitiser starting up")

# ── Imports (after logging is configured) ────────────────────────────────────

from config import GPIO_PINS, DEBOUNCE_MS, LCD, CAMERA, GALLERY_DIR

# Conditionally disable hardware / LCD via CLI flags
if args.no_hardware:
    GPIO_PINS.clear()

if args.no_lcd:
    LCD["enabled"] = False

from camera import Camera
from hardware import HardwareManager
from lcd import LCDManager
from gallery import GalleryManager
from gui import TeletextGUI

# ── Bootstrap ─────────────────────────────────────────────────────────────────

def main():
    log.info("Initialising subsystems...")

    camera  = Camera(CAMERA)
    gallery = GalleryManager(GALLERY_DIR)

    # GUI must be created before HardwareManager so we can pass root.after
    # We use a temporary placeholder and patch it in after Tk is up.
    # Solution: create a minimal Tk first, then build the full GUI.

    import tkinter as tk
    # We need `root.after` for hardware callbacks — create Tk first.
    root_ref = [None]

    def _after_proxy(ms, fn):
        if root_ref[0]:
            root_ref[0].after(ms, fn)

    hw  = HardwareManager(GPIO_PINS, DEBOUNCE_MS, _after_proxy)
    lcd = LCDManager(LCD)

    lcd.show_message("boot")

    app = TeletextGUI(camera, hw, lcd, gallery)
    root_ref[0] = app   # Now hardware callbacks can reach the event loop

    log.info("GUI ready — entering main loop")
    try:
        app.mainloop()
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    finally:
        hw.cleanup()
        camera.release()
        log.info("Shutdown complete")


if __name__ == "__main__":
    main()
