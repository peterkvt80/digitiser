"""
lcd.py — 2×32 LCD panel manager via I2C (PCF8574 backpack).
Uses the RPLCD library. Degrades gracefully if hardware is absent.
"""

import logging
import threading
import time

log = logging.getLogger(__name__)

try:
    from RPLCD.i2c import CharLCD
    _LCD_LIB = True
except ImportError:
    _LCD_LIB = False
    log.info("RPLCD not installed — LCD display disabled")


# ── Pre-formatted message templates (2 rows × 32 chars) ─────────────────────

def _pad(text: str, width: int) -> str:
    """Centre-pad a string to exactly `width` characters."""
    return text[:width].center(width)


MESSAGES = {
    "boot":       ("  TELETEXT CREATOR  v1.0   ", "   Point camera at sheet   "),
    "ready":      (" READY - PRESS CAPTURE BTN ", "  Ensure sheet is in frame "),
    "lighting":   ("    LIGHTS ON...           ", "   Preparing capture...    "),
    "capturing":  ("      CAPTURING...         ", "     Please hold still     "),
    "processing": ("     PROCESSING PAGE       ", "  Detecting grid & text... "),
    "ocr":        ("     READING TEXT...       ", "     Running OCR engine    "),
    "done":       ("   CONVERSION COMPLETE!    ", "  Press SAVE or CANCEL     "),
    "saving":     ("       SAVING...           ", "                           "),
    "cancelled":  ("      CANCELLED            ", "   Ready for next sheet    "),
    "error":      ("        ERROR!             ", "  Check log for details    "),
    "no_grid":    ("   GRID NOT DETECTED!      ", " Adjust position & retry   "),
    "gallery":    ("       GALLERY             ", "                           "),
}


class LCDManager:
    """
    Thread-safe LCD manager.

    Parameters
    ----------
    config : dict
        From config.LCD — keys: enabled, cols, rows, i2c_addr, i2c_port
    """

    def __init__(self, config: dict):
        self._cols = config.get("cols", 32)
        self._rows = config.get("rows", 2)
        self._lcd = None
        self._lock = threading.Lock()
        self._scroll_thread = None
        self._scroll_stop = threading.Event()

        if config.get("enabled") and _LCD_LIB:
            try:
                self._lcd = CharLCD(
                    i2c_expander="PCF8574",
                    address=config.get("i2c_addr", 0x27),
                    port=config.get("i2c_port", 1),
                    cols=self._cols,
                    rows=self._rows,
                    dotsize=8,
                )
                self._lcd.backlight_enabled = True
                self.show_message("boot")
                log.info("LCD initialised at 0x%02X", config.get("i2c_addr", 0x27))
            except Exception as e:
                log.warning("LCD init failed: %s", e)
                self._lcd = None
        else:
            log.info("LCD not enabled or RPLCD unavailable")

    # ── Public API ────────────────────────────────────────────────────────────

    def show_message(self, key: str):
        """Display a named message from the MESSAGES dict."""
        if key in MESSAGES:
            rows = MESSAGES[key]
            self._write_rows(rows)
        else:
            log.warning("Unknown LCD message key: %s", key)

    def show_custom(self, row1: str, row2: str = ""):
        """Display arbitrary text, truncated/padded to fit."""
        self._write_rows((row1, row2))

    def show_saved(self, page_number: int):
        self._write_rows((
            f"  SAVED TO GALLERY! P{page_number:03d} ",
            "  Press CAPTURE for next   ",
        ))

    def show_gallery_page(self, index: int, total: int, name: str):
        row1 = f" GALLERY: {index+1}/{total} "
        row2 = name[:self._cols].center(self._cols)
        self._write_rows((row1, row2))

    def scroll_message(self, row1: str, row2: str = "", delay: float = 0.35):
        """
        Scroll a long message across row 1.
        Runs in a background thread; call stop_scroll() to halt.
        """
        self._stop_scroll()
        self._scroll_stop.clear()

        def _run():
            pad = " " * self._cols
            msg = pad + row1[:80] + pad
            for i in range(len(msg) - self._cols + 1):
                if self._scroll_stop.is_set():
                    break
                chunk = msg[i:i + self._cols]
                self._write_rows((chunk, row2))
                time.sleep(delay)

        self._scroll_thread = threading.Thread(target=_run, daemon=True)
        self._scroll_thread.start()

    def stop_scroll(self):
        self._stop_scroll()

    def backlight(self, on: bool):
        if self._lcd:
            with self._lock:
                try:
                    self._lcd.backlight_enabled = on
                except Exception:
                    pass

    def clear(self):
        if self._lcd:
            with self._lock:
                try:
                    self._lcd.clear()
                except Exception:
                    pass

    # ── Internal ──────────────────────────────────────────────────────────────

    def _write_rows(self, rows: tuple):
        """Write up to 2 rows to the display, thread-safely."""
        if not self._lcd:
            # Log to console so development is easy without hardware
            r1 = rows[0] if len(rows) > 0 else ""
            r2 = rows[1] if len(rows) > 1 else ""
            log.debug("LCD | %-32s |", r1[:self._cols])
            log.debug("LCD | %-32s |", r2[:self._cols])
            return

        with self._lock:
            try:
                self._lcd.clear()
                for i, row in enumerate(rows[:self._rows]):
                    self._lcd.cursor_pos = (i, 0)
                    self._lcd.write_string(row[:self._cols].ljust(self._cols))
            except Exception as e:
                log.warning("LCD write error: %s", e)

    def _stop_scroll(self):
        self._scroll_stop.set()
        if self._scroll_thread and self._scroll_thread.is_alive():
            self._scroll_thread.join(timeout=1.0)
