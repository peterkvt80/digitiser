"""
camera.py — Camera abstraction layer.

Supported backends (set CAMERA["backend"] in config.py):
    "ipwebcam"  — Android phone running IP Webcam app, over USB via ADB
                  port-forwarding.  No WiFi needed.  ← NEW DEFAULT
    "opencv"    — USB webcam via OpenCV
    "picamera2" — Pi Camera Module via libcamera
    "auto"      — probes ipwebcam → picamera2 → opencv; unreliable if
                  multiple sources are present, so avoid in production
    "none"      — file-load only (no camera)

IP Webcam / ADB setup (one-time, on the phone):
    1. Install "IP Webcam" (Pavel Khlebovich) from the Play Store.
    2. Enable Developer Options: Settings → About Phone → tap Build Number 7×.
    3. Enable USB Debugging: Settings → Developer Options → USB Debugging ON.
    4. Connect phone to Pi via USB-C cable.
    5. On first connection accept the "Allow USB debugging?" prompt on the phone.
    6. Open IP Webcam, scroll to bottom, tap "Start server".
       Note the port shown (default 8080).  Leave the app running.
    7. The Pi-side ADB port-forward is set up automatically by this module
       each time Camera() is instantiated (see _setup_adb_forward).

IP Webcam endpoints used:
    Preview (MJPEG stream) : http://localhost:<port>/video
    Still capture (JPEG)   : http://localhost:<port>/photo.jpg
    Focus trigger          : http://localhost:<port>/focus   (GET)

The Huawei P9 shoots 12 MP stills (3968×2976).  IP Webcam delivers the
full-sensor JPEG when /photo.jpg is requested, giving the digitiser
significantly more resolution than a typical USB webcam.
"""

import io
import logging
import subprocess
import time
import threading
import urllib.request
import urllib.error
from PIL import Image

log = logging.getLogger(__name__)


# ── Backend detection ─────────────────────────────────────────────────────────

def _detect_backend(forced: str) -> str:
    if forced and forced != "auto":
        log.info("Camera backend forced to: %s", forced)
        return forced

    # Try IP Webcam / ADB first
    if _adb_available():
        log.info("Camera backend: ipwebcam (ADB detected)")
        return "ipwebcam"

    # Try picamera2
    try:
        from picamera2 import Picamera2
        cameras = Picamera2.global_camera_info()
        if cameras:
            log.info("Camera backend: picamera2 (%d camera(s) found)", len(cameras))
            return "picamera2"
        log.info("picamera2 installed but no Pi camera detected — trying OpenCV")
    except Exception:
        pass

    # Try OpenCV USB webcam
    try:
        import cv2
        cap = cv2.VideoCapture(0)
        if cap.isOpened():
            cap.release()
            log.info("Camera backend: OpenCV (USB webcam on /dev/video0)")
            return "opencv"
        cap.release()
    except Exception:
        pass

    log.info("Camera backend: none (file-load only)")
    return "none"


def _adb_available() -> bool:
    """Return True if adb is on PATH and at least one device is connected."""
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True, text=True, timeout=3
        )
        lines = [l.strip() for l in result.stdout.splitlines()
                 if l.strip() and "List of devices" not in l]
        return any("device" in l for l in lines)
    except Exception:
        return False


# ── ADB port-forwarding ───────────────────────────────────────────────────────

def _setup_adb_forward(port: int) -> bool:
    """
    Forward localhost:<port> on the Pi to the phone's IP Webcam server.
    Returns True on success.
    """
    try:
        result = subprocess.run(
            ["adb", "forward", f"tcp:{port}", f"tcp:{port}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            log.info("ADB port-forward set up: localhost:%d → phone:%d", port, port)
            return True
        log.error("ADB forward failed: %s", result.stderr.strip())
        return False
    except FileNotFoundError:
        log.error("adb not found — install with: sudo apt install adb")
        return False
    except Exception as e:
        log.error("ADB forward error: %s", e)
        return False


def _teardown_adb_forward(port: int):
    try:
        subprocess.run(
            ["adb", "forward", "--remove", f"tcp:{port}"],
            capture_output=True, timeout=3
        )
    except Exception:
        pass


# ── IP Webcam HTTP helpers ────────────────────────────────────────────────────

def _ipwc_get(url: str, timeout: float = 5.0) -> "bytes | None":
    """Fetch a URL from the IP Webcam server. Returns raw bytes or None."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        log.debug("IP Webcam fetch failed (%s): %s", url, e)
        return None


def _ipwc_jpeg_to_pil(data: bytes) -> "Image.Image | None":
    try:
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as e:
        log.debug("JPEG decode failed: %s", e)
        return None


# ── Camera class ──────────────────────────────────────────────────────────────

class Camera:
    """
    Unified camera interface.

    Parameters
    ----------
    config : dict
        From config.CAMERA.  Key fields:
            backend         — "ipwebcam" | "opencv" | "picamera2" | "auto" | "none"
            ipwebcam_port   — IP Webcam server port (default 8080)
            preview_width   — preview frame width
            preview_height  — preview frame height
            capture_width   — still capture width  (ipwebcam delivers full sensor)
            capture_height  — still capture height
            rotation        — 0 | 90 | 180 | 270
            warmup_seconds  — seconds to wait after opening (default 1.5 for phone)
    """

    def __init__(self, config: dict):
        self._cfg     = config
        self._cam     = None      # OpenCV / picamera2 handle
        self._lock    = threading.Lock()
        self._port    = config.get("ipwebcam_port", 8080)
        self._base    = f"http://localhost:{self._port}"
        self._session_active = False   # True while MJPEG stream thread is running

        self.backend = _detect_backend(config.get("backend", "ipwebcam"))
        self._open()

    # ── Open ──────────────────────────────────────────────────────────────────

    def _open(self):
        if self.backend == "ipwebcam":
            self._open_ipwebcam()
        elif self.backend == "picamera2":
            self._open_picamera2()
        elif self.backend == "opencv":
            self._open_opencv()

    def _open_ipwebcam(self):
        """Set up ADB forward and verify the IP Webcam server is reachable."""
        if not _setup_adb_forward(self._port):
            log.error("IP Webcam: ADB port-forward failed — check USB connection")
            return

        # Give the phone a moment in case IP Webcam was just started
        warmup = self._cfg.get("warmup_seconds", 1.5)
        log.info("IP Webcam: waiting %.1fs for server to be ready...", warmup)
        time.sleep(warmup)

        # Probe the server — fetch a single JPEG to confirm it's alive
        probe = _ipwc_get(f"{self._base}/photo.jpg", timeout=6.0)
        if probe and len(probe) > 1000:
            img = _ipwc_jpeg_to_pil(probe)
            if img:
                log.info("IP Webcam: connected — sensor %dx%d", img.width, img.height)
                self._cam = "ipwebcam"   # sentinel: not None means available
                return

        log.error(
            "IP Webcam: server not responding at %s\n"
            "  • Open IP Webcam on the phone and tap 'Start server'\n"
            "  • Confirm the port matches ipwebcam_port in config.py (%d)\n"
            "  • Accept the USB debugging prompt on the phone if shown",
            self._base, self._port
        )

    def _open_picamera2(self):
        try:
            from picamera2 import Picamera2
            self._cam = Picamera2()
            preview_cfg = self._cam.create_preview_configuration(
                main={"size": (self._cfg["preview_width"],
                               self._cfg["preview_height"])}
            )
            self._cam.configure(preview_cfg)
            self._cam.start()
            time.sleep(self._cfg.get("warmup_seconds", 0.5))
            log.info("picamera2 started")
        except Exception as e:
            log.error("picamera2 open failed: %s", e)
            self._cam = None

    def _open_opencv(self):
        try:
            import cv2
            self._cam = cv2.VideoCapture(0)
            if not self._cam.isOpened():
                log.error("OpenCV: no camera found on /dev/video0")
                self._cam = None
            else:
                self._cam.set(cv2.CAP_PROP_FRAME_WIDTH,  self._cfg["preview_width"])
                self._cam.set(cv2.CAP_PROP_FRAME_HEIGHT, self._cfg["preview_height"])
                log.info("OpenCV camera started")
        except Exception as e:
            log.error("OpenCV open failed: %s", e)
            self._cam = None

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self._cam is not None

    def grab_preview(self) -> "Image.Image | None":
        """Grab a preview frame (lower resolution, fast)."""
        return self._grab()

    def grab_capture(self) -> "Image.Image | None":
        """
        Grab a high-resolution still for digitising.

        ipwebcam : requests /photo.jpg — the phone shoots at full sensor
                   resolution (3968×2976 for Huawei P9), optionally triggers
                   auto-focus first.
        picamera2: reconfigures to full-res then back to preview.
        opencv   : flushes stale frames then grabs a fresh one.
        """
        if self.backend == "ipwebcam" and self._cam:
            return self._capture_ipwebcam()
        elif self.backend == "picamera2" and self._cam:
            return self._capture_picamera2()
        elif self.backend == "opencv" and self._cam:
            return self._capture_opencv()
        return None

    def release(self):
        if self.backend == "ipwebcam":
            _teardown_adb_forward(self._port)
        elif self.backend == "picamera2" and self._cam:
            try:
                self._cam.stop()
            except Exception:
                pass
        elif self.backend == "opencv" and self._cam:
            self._cam.release()
        self._cam = None
        log.info("Camera released")

    # ── IP Webcam capture ─────────────────────────────────────────────────────

    def _capture_ipwebcam(self) -> "Image.Image | None":
        """
        Capture a full-resolution still from IP Webcam.

        Sequence:
          1. Trigger auto-focus (GET /focus) and wait for it to settle.
          2. Fetch /photo.jpg at full sensor resolution.
          3. Decode and return as PIL RGB image.
        """
        # Step 1: trigger AF and wait
        af_wait = self._cfg.get("autofocus_wait", 1.2)
        _ipwc_get(f"{self._base}/focus", timeout=3.0)
        log.debug("IP Webcam: AF triggered, waiting %.1fs", af_wait)
        time.sleep(af_wait)

        # Step 2: fetch full-res JPEG
        data = _ipwc_get(f"{self._base}/photo.jpg", timeout=15.0)
        if not data or len(data) < 5000:
            log.error("IP Webcam: photo.jpg returned no data or too small")
            return None

        # Step 3: decode
        img = _ipwc_jpeg_to_pil(data)
        if img is None:
            log.error("IP Webcam: JPEG decode failed")
            return None

        log.info("IP Webcam: captured %dx%d still", img.width, img.height)
        return self._apply_rotation(img)

    def _grab_ipwebcam(self) -> "Image.Image | None":
        """
        Grab a preview-resolution frame from IP Webcam for the live preview.
        Uses /shot.jpg (single-frame JPEG, much faster than /photo.jpg).
        """
        data = _ipwc_get(f"{self._base}/shot.jpg", timeout=3.0)
        if not data:
            return None
        img = _ipwc_jpeg_to_pil(data)
        if img is None:
            return None
        # Downscale to preview resolution for speed
        pw = self._cfg.get("preview_width", 1280)
        ph = self._cfg.get("preview_height", 960)
        img.thumbnail((pw, ph), Image.BILINEAR)
        return self._apply_rotation(img)

    # ── picamera2 capture ─────────────────────────────────────────────────────

    def _capture_picamera2(self) -> "Image.Image | None":
        with self._lock:
            try:
                from picamera2 import Picamera2
                self._cam.stop()
                still_cfg = self._cam.create_still_configuration(
                    main={"size": (self._cfg["capture_width"],
                                   self._cfg["capture_height"])}
                )
                self._cam.configure(still_cfg)
                self._cam.start()
                time.sleep(0.3)
                arr = self._cam.capture_array()
                img = Image.fromarray(arr).convert("RGB")
                self._cam.stop()
                prev_cfg = self._cam.create_preview_configuration(
                    main={"size": (self._cfg["preview_width"],
                                   self._cfg["preview_height"])}
                )
                self._cam.configure(prev_cfg)
                self._cam.start()
                return self._apply_rotation(img)
            except Exception as e:
                log.error("picamera2 capture failed: %s", e)
                return None

    # ── OpenCV capture ────────────────────────────────────────────────────────

    def _capture_opencv(self) -> "Image.Image | None":
        import cv2
        for _ in range(5):
            self._cam.read()
        ret, frame = self._cam.read()
        if ret:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            return self._apply_rotation(Image.fromarray(rgb))
        return None

    # ── Generic grab (preview) ────────────────────────────────────────────────

    def _grab(self) -> "Image.Image | None":
        if self.backend == "ipwebcam" and self._cam:
            return self._grab_ipwebcam()

        elif self.backend == "picamera2" and self._cam:
            with self._lock:
                try:
                    arr = self._cam.capture_array()
                    return self._apply_rotation(Image.fromarray(arr).convert("RGB"))
                except Exception:
                    return None

        elif self.backend == "opencv" and self._cam:
            import cv2
            ret, frame = self._cam.read()
            if ret:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                return self._apply_rotation(Image.fromarray(rgb))
            return None

        return None

    # ── Rotation ──────────────────────────────────────────────────────────────

    def _apply_rotation(self, img: Image.Image) -> Image.Image:
        rot = self._cfg.get("rotation", 0)
        if rot == 90:
            return img.rotate(-90, expand=True)
        elif rot == 180:
            return img.rotate(180, expand=True)
        elif rot == 270:
            return img.rotate(90, expand=True)
        return img
