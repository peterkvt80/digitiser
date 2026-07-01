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
    #         Set to 100 — real pencil strokes reach raw_min ≈ 40–80;
    #         graph-paper grid-line intersections and near-blank cells measure
    #         raw_min ≈ 95–115.  The current value of 100 sits in the gap.
    #         If OCR false positives reappear (raw_min just below 100), raise
    #         to 110 — but first verify that genuine pencil text on your sheet
    #         has raw_min < 90 to confirm a safe gap exists.
    "text_spread_threshold": 100,  # min lum spread to even consider a cell as TEXT
    "min_ink_pixels":    20,   # fewer absolute dark pixels than this → EMPTY
    "noise_spread_limit": 60,  # spread below this → EMPTY (dot-grid noise gate)
    "text_min_lum":     100,   # cell min-luminance must be below this → TEXT

    # white_gfx_fill_threshold : fraction of cell pixels below sixel_fill_threshold
    #     that must be dark to qualify as achromatic (white) graphics.
    #
    #     IMPORTANT: this threshold is applied to the PRE-CLAHE (raw) greyscale
    #     patch, not the CLAHE-processed patch.  CLAHE massively inflates fill
    #     fractions on blank paper (empty cell: raw ≈14%, CLAHE ≈69%), so using
    #     CLAHE grey for fill_frac made the gate fire on graph-paper lines and
    #     horse outline strokes that are not genuinely shaded cells.
    #
    #     Measured on raw grey with sixel_fill_threshold=200:
    #       empty paper:           ≈ 0.03–0.15
    #       black outline stroke:  ≈ 0.05–0.35
    #       hand-drawn text:       <  0.25  (typically 0.05–0.20)
    #       genuine grey shading:  ≈ 0.25–0.80
    #
    #     0.25 is the text/graphite-shading boundary: hand-drawn pencil text
    #     (letters, digits) covers less than 25% of a cell's area even for
    #     bold block capitals, while a cell shaded solid with graphite pencil
    #     reliably exceeds it.  Below 0.25 the cell falls through to the TEXT
    #     classifier instead (see Step B in _process_grid); at or above 0.25
    #     (with avg_s and mean_lum also passing) it is CELL_WHITE_GFX and is
    #     never sent to Tesseract.  Raise this only if genuine graphite
    #     shading on your sheets is being OCR'd as text (i.e. its fill
    #     fraction is measuring below 0.25); lower it only if bold/dense
    #     handwriting is being misclassified as shading.
    #
    # white_gfx_max_saturation : mean HSV saturation of ink pixels must be below
    #     this to confirm the ink is achromatic.  Grey pencil measures avg_s 6–24;
    #     the colour classifier requires avg_s >= 50, so 40 sits safely below
    #     that with a 10-unit gap.
    #
    # white_gfx_min_lum : mean raw luminance of the cell must be AT OR ABOVE this
    #     to qualify as white-gfx.  Black-background cells have mean_lum_raw ≈ 0–50
    #     (the cell is dark because the background is dark, not because of dense
    #     pencil shading).  Genuine white-gfx cells contain dark shading on bright
    #     paper, giving mean_lum_raw ≈ 80–180.  Threshold of 60 rejects black-
    #     background cells that would otherwise pass fill_frac (fill ≈100% because
    #     everything is dark) and avg_s (≈0 because black has no saturation).
    "white_gfx_fill_threshold": 0.25,
    "white_gfx_max_saturation":   40,
    "white_gfx_min_lum":          60,

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
        # Border inset for the perspective warp destination quad.
        #
        # The ArUco inward corners coincide with the *outer* edge of the
        # template's printed 6px border.  Mapping those corners straight to
        # (0,0)…(dst_w,dst_h) pulls the border ink into the warp output,
        # producing a dark strip at each edge.
        #
        # This value insets the destination quad inward so the border maps
        # outside the output canvas, eliminating the dark strip and ensuring
        # the cell grid overlay aligns with the image content.
        #
        # Because the camera is never perfectly perpendicular, the printed
        # border maps to DIFFERENT pixel offsets at each of the four edges.
        # A single symmetric value cannot fix all four simultaneously, so
        # the config accepts independent per-edge values:
        #
        #   {"top": bt, "bottom": bb, "left": bl, "right": br}
        #
        # How to measure: open a warped image in an image editor or use the
        # SHOW WARPED button.  Scan inward from each edge and note the first
        # row/col that is not dark border ink (lum > ~150 in the left-10px
        # edge strip).  Set each value to that row/col number.
        #
        # Measured on this camera/mount setup:
        #   top:    9  (sealion image: border ends at row 6, horse: <9 sufficient)
        #   bottom: 13 (horse image: border visible at rows 949-952, H-by must be <948)
        #   left:   6  (sealion image: remaining 2px after old bx=4; horse: clean at 6)
        #   right:  6  (symmetric with left, conservative)
        #
        # Backward-compatible shorthand also accepted:
        #   {"x": bx, "y": by}   symmetric left/right and symmetric top/bottom
        #   <int>                 same inset on all four edges
        "grid_border_warp_px": {"top": -4, "bottom": -4, "left": 2, "right": 4},
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
