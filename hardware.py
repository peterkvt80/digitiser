"""
hardware.py — GPIO buttons, illumination relay, indicator LEDs.
All hardware is optional: if RPi.GPIO is unavailable everything is a no-op
so the app runs fine on a desktop for development.
"""

import threading
import time
import logging

log = logging.getLogger(__name__)

# ── Try importing GPIO ────────────────────────────────────────────────────────

try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    _GPIO_AVAILABLE = True
    log.info("RPi.GPIO available — hardware I/O enabled")
except ImportError:
    _GPIO_AVAILABLE = False
    log.info("RPi.GPIO not available — running in software-only mode")


class HardwareManager:
    """
    Manages all physical I/O: buttons, LEDs, illumination relay.
    Callbacks are registered per button and called from a background thread,
    then marshalled onto the Tkinter main thread via `tk_after_fn`.

    Parameters
    ----------
    pins : dict
        From config.GPIO_PINS
    debounce_ms : int
        Button debounce time
    tk_after_fn : callable
        Pass `root.after` so callbacks land on the GUI thread.
    """

    def __init__(self, pins: dict, debounce_ms: int, tk_after_fn):
        self._pins = pins
        self._debounce_ms = debounce_ms
        self._after = tk_after_fn
        self._callbacks = {}          # pin → callable
        self._poll_thread = None
        self._running = False
        self._last_state = {}
        self._last_trigger = {}

        if _GPIO_AVAILABLE:
            self._setup_pins()
            self._start_polling()

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _setup_pins(self):
        inputs = ["BTN_CAPTURE", "BTN_SAVE", "BTN_CANCEL", "BTN_NEXT", "BTN_PREV"]
        outputs = ["LIGHT_RELAY", "LED_READY", "LED_BUSY"]

        for name in inputs:
            pin = self._pins.get(name)
            if pin is not None:
                GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                self._last_state[pin] = GPIO.HIGH
                self._last_trigger[pin] = 0

        for name in outputs:
            pin = self._pins.get(name)
            if pin is not None:
                GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

        log.info("GPIO pins configured")

    # ── Button polling ────────────────────────────────────────────────────────

    def _start_polling(self):
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def _poll_loop(self):
        debounce_s = self._debounce_ms / 1000.0
        input_names = ["BTN_CAPTURE", "BTN_SAVE", "BTN_CANCEL", "BTN_NEXT", "BTN_PREV"]

        while self._running:
            now = time.time()
            for name in input_names:
                pin = self._pins.get(name)
                if pin is None:
                    continue
                state = GPIO.input(pin)
                last = self._last_state.get(pin, GPIO.HIGH)
                # Detect falling edge (button press, active LOW)
                if state == GPIO.LOW and last == GPIO.HIGH:
                    if now - self._last_trigger.get(pin, 0) > debounce_s:
                        self._last_trigger[pin] = now
                        cb = self._callbacks.get(name)
                        if cb:
                            self._after(0, cb)
                self._last_state[pin] = state
            time.sleep(0.02)  # 20ms poll interval

    # ── Public API ────────────────────────────────────────────────────────────

    def on_button(self, button_name: str, callback):
        """Register a callback for a named button press."""
        self._callbacks[button_name] = callback

    def set_output(self, name: str, state: bool):
        """Drive a named output HIGH (True) or LOW (False)."""
        if not _GPIO_AVAILABLE:
            return
        pin = self._pins.get(name)
        if pin is not None:
            GPIO.output(pin, GPIO.HIGH if state else GPIO.LOW)

    def lights_on(self):
        self.set_output("LIGHT_RELAY", True)
        self.set_output("LED_BUSY", True)
        self.set_output("LED_READY", False)
        log.debug("Illumination ON")

    def lights_off(self):
        self.set_output("LIGHT_RELAY", False)
        log.debug("Illumination OFF")

    def set_ready(self):
        self.set_output("LED_READY", True)
        self.set_output("LED_BUSY", False)

    def set_busy(self):
        self.set_output("LED_BUSY", True)
        self.set_output("LED_READY", False)

    def lights_off_after(self, delay: float):
        """Turn off illumination after `delay` seconds (non-blocking)."""
        def _off():
            time.sleep(delay)
            self.lights_off()
        threading.Thread(target=_off, daemon=True).start()

    def cleanup(self):
        self._running = False
        if _GPIO_AVAILABLE:
            # Safe state
            self.set_output("LIGHT_RELAY", False)
            self.set_output("LED_READY", False)
            self.set_output("LED_BUSY", False)
            GPIO.cleanup()
            log.info("GPIO cleaned up")
