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

logging.basicConfig(
    level=logging.DEBUG if args.debug else logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path.home() / "tdi620_digitiser.log"),
    ]
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
