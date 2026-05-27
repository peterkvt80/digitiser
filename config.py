"""
config.py — Hardware pin assignments and tunable constants.
Edit this file to match your wiring without touching any logic.
"""

# ── GPIO Pin Assignments (BCM numbering) ─────────────────────────────────────

GPIO_PINS = {
    # Inputs (buttons, active LOW with internal pull-up)
    "BTN_CAPTURE":  17,
    "BTN_SAVE":     27,
    "BTN_CANCEL":   22,
    "BTN_NEXT":     23,
    "BTN_PREV":     24,

    # Outputs
    "LIGHT_RELAY":  18,
    "LED_READY":    25,
    "LED_BUSY":     8,
}

DEBOUNCE_MS     = 50
LIGHT_OFF_DELAY = 2.0

# ── LCD ───────────────────────────────────────────────────────────────────────

LCD = {
    "enabled":  True,
    "cols":     32,
    "rows":     2,
    "i2c_addr": 0x27,
    "i2c_port": 1,
}

# ── Camera ────────────────────────────────────────────────────────────────────
#
# Backend: "ipwebcam" — Huawei P9 (or any Android phone) running the
#          "IP Webcam" app by Pavel Khlebovich, connected via USB-C cable.
#          ADB port-forwarding is set up automatically; no WiFi needed.
#
# One-time phone setup:
#   1. Install "IP Webcam" from the Play Store.
#   2. Enable Developer Options: Settings → About Phone → tap Build Number 7×.
#   3. Enable USB Debugging: Settings → Developer Options → USB Debugging ON.
#   4. Connect phone to Pi with the USB-C cable.
#   5. Accept the "Allow USB debugging?" prompt on the phone.
#   6. Open IP Webcam, configure settings below, tap "Start server" at the
#      bottom of the app.  Leave it running whenever the digitiser is in use.
#
# Recommended IP Webcam app settings (tap the ≡ menu in the app):
#   Video preferences → Resolution : 1920×1080  (preview stream)
#   Photo settings    → Resolution : Maximum     (3968×2976 for P9)
#   Photo settings    → Quality    : 95
#   Focus             → Continuous auto-focus ON
#   Orientation       → Landscape  (or set rotation below to compensate)
#   Port              : 8080  (matches ipwebcam_port below)
#   Authentication    : leave blank (local USB connection, no need)
#
# capture_width / capture_height are informational only for ipwebcam —
# the phone always delivers the full sensor resolution from /photo.jpg.
# The values here are used by other backends and for display purposes.

CAMERA = {
    "backend":         "ipwebcam",   # Android phone via IP Webcam + ADB USB

    # IP Webcam settings
    "ipwebcam_port":   8080,         # must match port set in the app
    "autofocus_wait":  1.2,          # seconds to wait after triggering AF

    # Huawei P9 sensor: 12 MP RGB, 3968×2976 (f/2.2, 1/2.9", 1.25µm pixels)
    "capture_width":   3968,
    "capture_height":  2976,

    # Preview stream resolution (phone streams at this size via /shot.jpg)
    "preview_width":   1280,
    "preview_height":  960,

    # Rotation — set to 90 or 270 if the phone is mounted in portrait
    # orientation on the copy stand.  0 = landscape (recommended).
    "rotation":        0,

    # Warmup: time (seconds) to wait for IP Webcam server to be reachable
    # after ADB forward is established.  Increase if connection is slow.
    "warmup_seconds":  1.5,
}

# ── Digitiser / CV Pipeline ───────────────────────────────────────────────────

DIGITISER = {
    "cols": 40,
    "rows": 24,

    # Perspective warp output resolution — keep multiples of 40 and 24.
    # Increased from 800×480 to take advantage of the P9's higher resolution.
    # Each cell is now 40×40 px (up from 20×20), giving Tesseract more pixels.
    "warp_width":  1600,
    "warp_height":  960,

    # Empty-cell threshold.  A cell whose mean greyscale value (after
    # normalisation) is above this is considered blank paper.
    # Range measured on P9 JPEGs: blank ≈ 210-220, lightest pencil ≈ 170-185.
    # Lowered from 207 to 190 to catch lightly-drawn pencil strokes and
    # hand-drawn lowercase letters which have thinner strokes than block caps.
    "empty_brightness_threshold": 190,

    # Sixel (block-graphics) fill thresholds
    "graphics_fill_threshold": 0.15,  # fraction of subcell area that must be dark
    "sixel_fill_threshold":    200,   # greyscale value below which a pixel is "dark"

    # Colour classification — used by the HLS circular-mean classifier.
    # These are fallback HSV gates, kept for backward-compat with calibration.
    "min_saturation":        12,
    "dark_value_threshold": 160,

    # Noise guard for the TEXT classifier (two-gate system).
    # Gate 1: lum_spread must reach TEXT_SPREAD_THRESH (set via
    #         text_spread_threshold, default 25).  Lowered from 40 to catch
    #         lightly-drawn pencil text whose cells measure spread 25-35.
    # Gate 2: dark_px (pixels < sixel_fill_threshold) must reach
    #         min_ink_pixels, AND spread must reach noise_spread_limit,
    #         AND the cell minimum luminance must be below text_min_lum.
    #         Together these reject dot-grid paper noise (spread <45, dark_px≈0)
    #         while accepting faint text (spread 50+, dark_px ≥ 20).
    # Gate 3 (min-lum): The darkest pixel in the cell must be below
    #         text_min_lum.  Spurious OCR hits on near-white noise cells
    #         never have a genuinely dark pixel; real pencil strokes always do.
    #         Set to 100 — pencil text on white paper always reaches below 100
    #         in the darkest part of the stroke; paper grain / compression
    #         artefacts stay above ~180.
    "text_spread_threshold": 100,  # min lum spread to even consider a cell as TEXT
    "min_ink_pixels":    20,   # fewer absolute dark pixels than this → EMPTY
    "noise_spread_limit": 50,  # spread below this → EMPTY (dot-grid noise gate)
    "text_min_lum":     100,   # cell min-luminance must be below this → TEXT

    # Grid-line suppression.  The printed template grid lines bleed into the
    # outermost pixels of every warped cell patch.  Setting this to N replaces
    # the outermost N pixel rows/columns of each cell with the interior median
    # before any colour or text classifier runs.  This removes saturation
    # dilution on coloured cells, false lum-spread on blank cells, and spurious
    # dark pixels at sixel sub-cell edges.  Set to 0 to disable.
    # 2px is correct for the standard template printed at A4 / 300 DPI and
    # warped to 1600×960; increase to 3 only if the camera angle is steep
    # enough that lines bleed more than 2px after warping.
    "grid_line_suppress_px": 2,

    # Border bleed guard.  The printed grid border (6px at 300 DPI, drawn with
    # cv2.rectangle which centres the stroke on the boundary) bleeds its inner
    # half into the warped cell area.  On edge cells this dark intrusion inflates
    # lum_spread and drives lum_min down, causing false TEXT or GRAPHICS hits.
    # For cells on the outermost row or column the classification patches (pg and
    # pr) are inset by this many pixels on the exposed edge before any classifier
    # runs.  Interior cells are never inset.  4px covers the worst-case half-
    # border bleed with some margin for perspective distortion; increase to 6 if
    # the camera angle is steep.
    "border_inset_px":   4,    # pixels to trim from exposed edge of border cells

    # Tesseract config.  --psm 6 (uniform text block) suits the strip-based
    # OCR approach where a whole row strip is passed to Tesseract at once.
    # Whitelist includes lowercase so Tesseract detects mixed-case handwriting;
    # the result is uppercased before writing to TTI (teletext is uppercase-only).
    "tesseract_config": (
        "--psm 6 --oem 1 "
        "-c tessedit_char_whitelist="
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 !\"#$%&'()*+,-./:;<=>?@[\\]^_{|}~"
    ),

    # OL,0 header line — plain text only, max 32 chars.
    # strftime codes (%H:%M etc.) are substituted by the broadcast inserter,
    # not here — use plain text only.
    "header_line": "BLOCK PARTY 26",

    # ── ArUco template geometry ───────────────────────────────────────────────
    # Four 4×4 ArUco markers (IDs 0=TL, 1=TR, 2=BR, 3=BL) at grid corners.
    # These values match the printed template exactly and never need changing.
    "template_params": {
        "aruco_dict":  "DICT_4X4_50",
        "corner_ids":  [0, 1, 2, 3],
        "grid_cols":   40,
        "grid_rows":   24,
        "sixel_cols":  2,
        "sixel_rows":  3,
    },
}

# ── Teletext Colour Palette (RGB) ─────────────────────────────────────────────

TELETEXT_PALETTE = {
    "BLACK":   (0,   0,   0),
    "RED":     (255, 0,   0),
    "GREEN":   (0,   255, 0),
    "YELLOW":  (255, 255, 0),
    "BLUE":    (0,   0,   255),
    "MAGENTA": (255, 0,   255),
    "CYAN":    (0,   255, 255),
    "WHITE":   (255, 255, 255),
}

# ── Gallery ───────────────────────────────────────────────────────────────────

from pathlib import Path

GALLERY_DIR = Path.home() / "digitiser_gallery"

# ── TTI Defaults ──────────────────────────────────────────────────────────────

TTI_DEFAULTS = {
    "description":  "Captured by TDI620 Digitiser",
    "cycle_time":   "8",
    "default_page": 100,
}

# ── Renderer ──────────────────────────────────────────────────────────────────

RENDERER = {
    # Path to teletext2.ttf.  None = fall back to Courier.
    # Place teletext2.ttf in the same directory as this file, or set an
    # absolute path, e.g. "/usr/share/fonts/truetype/teletext2.ttf"
    "teletext_font": "teletext2.ttf",
}
