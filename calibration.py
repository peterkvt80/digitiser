"""
calibration.py — Per-user colour calibration for the TDI620 Digitiser.

The calibration chart is a monochrome (black-and-white) sheet suitable for
any laser or inkjet printer.  Each swatch box is labelled with the colour
name and a unique hatch pattern so the user can easily identify which box to
colour with each pencil.

Chart layout (A5 landscape):
  [ArUco] [RED//] [GREEN\\] [YELLOW--] [BLUE||] [CYAN XX] [MAGENTA..] [WHITE//]
  ← marker ← 7 swatch boxes, each ~equal width, all labelled in black ────────

All swatch boxes are optional — only fill in the colours whose pencils are
scanning incorrectly.  Unfilled boxes are detected as uncoloured and skipped;
their hue ranges fall back to the built-in defaults.

Geometry is computed purely from module-level constants so it is always
consistent between generate_chart() and calibrate_from_image() — no
runtime state or file is needed to match the two.

Usage
-----
    python3 calibration.py --generate            # write calibration_chart.png/pdf
    python3 calibration.py --calibrate IMAGE     # process a photo of the chart
"""

import argparse
import json
import logging
import sys
import numpy as np
from pathlib import Path

log = logging.getLogger(__name__)

# ── Module-level cv2 import (required for ArUco and image processing) ─────────
try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False
    log.error("opencv-contrib-python not installed — pip install opencv-contrib-python")

# ── Layout constants (all mm, converted to px at PRINT_DPI) ──────────────────

PRINT_DPI      = 300
MM_PER_INCH    = 25.4
PAGE_W_MM      = 210.0    # A5 landscape width
PAGE_H_MM      = 100.0    # A5 landscape height
MARGIN_MM      = 10.0
SWATCH_GAP_MM  = 2.5      # gap between swatch boxes
LABEL_H_MM     = 8.0      # height reserved above each swatch for the text label
BORDER_PX      = 5        # swatch border thickness

# ArUco marker
ARUCO_DICT_NAME = "DICT_6X6_50"
ARUCO_MARKER_ID = 1          # ID 1 — distinct from capture template ID 0
ARUCO_MARKER_MM = 25.0

# Swatch colours in left-to-right order
SWATCH_COLOURS = ["RED", "GREEN", "YELLOW", "BLUE", "CYAN", "MAGENTA", "WHITE"]

# Hatch patterns per colour — drawn in black so the chart is monochrome.
# Each entry: (angle_deg, spacing_px)
# The pattern makes each box visually distinct without colour.
HATCH_PATTERNS = {
    "RED":     ("fwd",    14),   # forward diagonal  ////
    "GREEN":   ("back",   14),   # backward diagonal \\\\
    "YELLOW":  ("horiz",  14),   # horizontal lines  ────
    "BLUE":    ("vert",   14),   # vertical lines    ||||
    "CYAN":    ("cross",  18),   # cross-hatch       ####
    "MAGENTA": ("dots",   20),   # dot grid          ....
    "WHITE":   ("fwd",    28),   # sparse diagonal — fill with grey/white pencil
}

# Monochrome grey levels
GREY_BG        = 255   # white paper
GREY_BORDER    = 0     # black border
GREY_LABEL     = 0     # black text
GREY_HATCH     = 180   # light grey hatch lines (distinguishable but subtle)

CALIB_FILENAME = "calibration.json"


def mm_to_px(mm: float) -> int:
    return round(mm / MM_PER_INCH * PRINT_DPI)


# ─────────────────────────────────────────────────────────────────────────────
# Geometry — computed deterministically from constants
# ─────────────────────────────────────────────────────────────────────────────

def _compute_chart_geom() -> dict:
    """
    Derive the chart geometry record purely from module-level constants.
    This is called by both generate_chart() and calibrate_from_image() so
    they always agree — no runtime state or saved file required.
    """
    W           = mm_to_px(PAGE_W_MM)
    H           = mm_to_px(PAGE_H_MM)
    margin      = mm_to_px(MARGIN_MM)
    marker_size = mm_to_px(ARUCO_MARKER_MM)
    gap         = mm_to_px(SWATCH_GAP_MM)
    label_h     = mm_to_px(LABEL_H_MM)

    # Swatch area begins to the right of the marker
    sa_x0 = margin + marker_size + gap
    sa_y0 = margin
    sa_x1 = W - margin
    sa_y1 = H - margin

    n          = len(SWATCH_COLOURS)
    total_gap  = gap * (n - 1)
    swatch_w   = (sa_x1 - sa_x0 - total_gap) // n
    box_y0     = sa_y0 + label_h
    box_h      = sa_y1 - box_y0

    # Marker top-left position
    mx = margin
    my = (H - marker_size) // 2

    swatch_centres = []
    for i in range(n):
        x0 = sa_x0 + i * (swatch_w + gap)
        x1 = x0 + swatch_w
        cx = (x0 + x1) // 2
        cy = box_y0 + box_h // 2
        swatch_centres.append((cx, cy))

    centres_arr  = np.array(swatch_centres, dtype=float)
    marker_tl    = np.array([mx, my], dtype=float)
    offsets_norm = (centres_arr - marker_tl) / marker_size

    return {
        "aruco_dict":      ARUCO_DICT_NAME,
        "aruco_marker_id": ARUCO_MARKER_ID,
        "swatch_colours":  SWATCH_COLOURS,
        "swatch_w_px":     swatch_w,
        "swatch_h_px":     box_h,
        "marker_x":        mx,
        "marker_y":        my,
        "marker_size_px":  marker_size,
        "offsets_from_marker_tl_normalised": offsets_norm.tolist(),
    }


# Pre-computed geometry — available at import time, no generate step needed
CHART_GEOM = _compute_chart_geom()


# ─────────────────────────────────────────────────────────────────────────────
# Chart generation — monochrome
# ─────────────────────────────────────────────────────────────────────────────

def generate_chart(output_dir: Path = Path(".")) -> dict:
    """
    Generate the monochrome calibration chart PNG and PDF.
    Returns the chart geometry dict (same as CHART_GEOM).
    """
    if not _CV2:
        raise RuntimeError("opencv-contrib-python required: pip install opencv-contrib-python")
    try:
        from PIL import Image as PILImage
    except ImportError:
        sys.exit("Pillow required: pip install Pillow")

    geom        = CHART_GEOM
    W           = mm_to_px(PAGE_W_MM)
    H           = mm_to_px(PAGE_H_MM)
    margin      = mm_to_px(MARGIN_MM)
    marker_size = geom["marker_size_px"]
    gap         = mm_to_px(SWATCH_GAP_MM)
    label_h     = mm_to_px(LABEL_H_MM)
    mx, my      = geom["marker_x"], geom["marker_y"]

    # Greyscale canvas
    canvas = np.ones((H, W), dtype=np.uint8) * GREY_BG

    # ── Title ─────────────────────────────────────────────────────────────────
    cv2.putText(canvas, "TDI620 COLOUR CALIBRATION — only colour the boxes for pencils that scan incorrectly",
                (margin, margin - 4),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, GREY_LABEL, 1, cv2.LINE_AA)

    # ── ArUco marker ──────────────────────────────────────────────────────────
    aruco_dict = _get_aruco_dict(ARUCO_DICT_NAME)
    marker_img = np.zeros((marker_size, marker_size), dtype=np.uint8)
    cv2.aruco.generateImageMarker(aruco_dict, ARUCO_MARKER_ID,
                                  marker_size, marker_img, 1)
    canvas[my:my + marker_size, mx:mx + marker_size] = marker_img

    # ── Swatch boxes ──────────────────────────────────────────────────────────
    n         = len(SWATCH_COLOURS)
    swatch_w  = geom["swatch_w_px"]
    box_h     = geom["swatch_h_px"]
    sa_x0     = mx + marker_size + gap
    sa_y0     = margin
    box_y0    = sa_y0 + label_h

    for i, colour_name in enumerate(SWATCH_COLOURS):
        x0 = sa_x0 + i * (swatch_w + gap)
        x1 = x0 + swatch_w
        y0 = box_y0
        y1 = y0 + box_h

        # Hatch fill (drawn inside the box, before the border)
        _draw_hatch(canvas, x0 + BORDER_PX, y0 + BORDER_PX,
                    x1 - BORDER_PX, y1 - BORDER_PX,
                    HATCH_PATTERNS[colour_name])

        # Box border
        cv2.rectangle(canvas, (x0, y0), (x1, y1), GREY_BORDER, BORDER_PX)

        # Colour name label above the box (bold black text)
        label     = colour_name          # full name: RED, GREEN, YELLOW…
        font      = cv2.FONT_HERSHEY_SIMPLEX
        fscale    = 0.5
        thick     = 1
        tw, th    = cv2.getTextSize(label, font, fscale, thick)[0]
        cx        = (x0 + x1) // 2
        tx        = cx - tw // 2
        ty        = y0 - 4
        cv2.putText(canvas, label, (tx, ty), font, fscale,
                    GREY_LABEL, thick, cv2.LINE_AA)

        # Instruction text inside the box (small, centred)
        instr  = "optional"
        iw, ih = cv2.getTextSize(instr, font, 0.35, 1)[0]
        cv2.putText(canvas, instr,
                    (cx - iw // 2, y0 + box_h // 2 + ih // 2),
                    font, 0.35, 140, 1, cv2.LINE_AA)

    # ── Save ──────────────────────────────────────────────────────────────────
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    png_path = output_dir / "calibration_chart.png"
    cv2.imwrite(str(png_path), canvas)
    log.info("Saved %s", png_path)

    pdf_path = output_dir / "calibration_chart.pdf"
    try:
        pil = PILImage.fromarray(canvas)   # already greyscale
        pil.save(str(pdf_path), "PDF", resolution=PRINT_DPI)
        log.info("Saved %s", pdf_path)
    except Exception as e:
        log.warning("PDF save failed: %s", e)

    return geom


def _draw_hatch(canvas: np.ndarray, x0: int, y0: int, x1: int, y1: int,
                pattern: tuple):
    """Draw a hatch pattern inside the box defined by (x0,y0)-(x1,y1)."""
    style, spacing = pattern
    if style is None or spacing == 0:
        return   # blank box

    if style == "fwd":        # ////
        for offset in range(-canvas.shape[0], canvas.shape[1], spacing):
            pt1 = (x0 + offset,       y0)
            pt2 = (x0 + offset + (y1 - y0), y1)
            _clipped_line(canvas, pt1, pt2, x0, y0, x1, y1, GREY_HATCH)

    elif style == "back":     # \\\\
        for offset in range(-canvas.shape[0], canvas.shape[1], spacing):
            pt1 = (x0 + offset,       y1)
            pt2 = (x0 + offset + (y1 - y0), y0)
            _clipped_line(canvas, pt1, pt2, x0, y0, x1, y1, GREY_HATCH)

    elif style == "horiz":    # ────
        y = y0 + spacing
        while y < y1:
            cv2.line(canvas, (x0, y), (x1, y), GREY_HATCH, 1)
            y += spacing

    elif style == "vert":     # ||||
        x = x0 + spacing
        while x < x1:
            cv2.line(canvas, (x, y0), (x, y1), GREY_HATCH, 1)
            x += spacing

    elif style == "cross":    # ####
        _draw_hatch(canvas, x0, y0, x1, y1, ("horiz", spacing))
        _draw_hatch(canvas, x0, y0, x1, y1, ("vert",  spacing))

    elif style == "dots":     # ....
        y = y0 + spacing // 2
        row = 0
        while y < y1:
            x_start = x0 + (spacing // 2 if row % 2 else 0) + spacing // 4
            x = x_start
            while x < x1:
                if x0 <= x < x1 and y0 <= y < y1:
                    canvas[y, x] = GREY_HATCH
                    if x + 1 < x1:
                        canvas[y, x + 1] = GREY_HATCH
                    if y + 1 < y1:
                        canvas[y + 1, x] = GREY_HATCH
                x += spacing
            y += spacing
            row += 1


def _clipped_line(canvas, pt1, pt2, x0, y0, x1, y1, colour):
    """Draw a line clipped to the bounding box."""
    ax = max(x0, min(x1 - 1, pt1[0]))
    ay = max(y0, min(y1 - 1, pt1[1]))
    bx = max(x0, min(x1 - 1, pt2[0]))
    by = max(y0, min(y1 - 1, pt2[1]))
    cv2.line(canvas, (ax, ay), (bx, by), colour, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Calibration — process a photo of the filled-in chart
# ─────────────────────────────────────────────────────────────────────────────

def calibrate_from_image(pil_image, gallery_dir: Path) -> dict:
    """
    Sample each swatch from a photo of the filled-in calibration chart.
    Saves calibration.json to gallery_dir.

    Uses CHART_GEOM (computed from constants) — no prior generate_chart()
    call is needed.

    Parameters
    ----------
    pil_image : PIL.Image
    gallery_dir : Path

    Returns
    -------
    dict  colour → {"hue", "hue_lo", "hue_hi"}
    """
    if not _CV2:
        raise RuntimeError("opencv-contrib-python required")

    geom      = CHART_GEOM
    img_np    = np.array(pil_image.convert("RGB"))

    # Apply the same grey-world white balance used by the digitiser so that
    # calibration swatch colours are measured in the same colour space as
    # the captured teletext sheets.
    try:
        from digitiser import _normalise_image
        img_np = _normalise_image(img_np)
        log.info("Image normalised (WB + exposure) for calibration")
    except Exception as e:
        log.warning("Image normalisation unavailable for calibration: %s", e)

    grey      = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)

    # Detect the calibration marker (ID 1)
    aruco_dict = _get_aruco_dict(geom["aruco_dict"])
    detector   = _make_detector(aruco_dict)
    target_id  = geom["aruco_marker_id"]

    mc = _detect_marker(grey, detector, target_id)
    if mc is None:
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        mc = _detect_marker(clahe.apply(grey), detector, target_id)
    if mc is None:
        raise RuntimeError(
            f"Calibration marker (ID {target_id}) not found.\n"
            "Ensure the chart is flat, well-lit, and fully visible.\n"
            "Tip: the marker is the black square on the left of the chart."
        )

    # Derive swatch centres from detected marker position + stored offsets
    marker_tl    = mc[0]   # top-left corner of detected marker
    marker_side  = float(np.linalg.norm(mc[1] - mc[0]))
    offsets_norm = np.array(geom["offsets_from_marker_tl_normalised"])
    swatch_cx_cy = marker_tl + offsets_norm * marker_side   # shape (N, 2)

    # Scale swatch sampling dimensions proportionally to the detected marker
    design_marker = geom["marker_size_px"]
    scale         = marker_side / design_marker
    sw_px  = max(10, int(geom["swatch_w_px"] * scale))
    sh_px  = max(10, int(geom["swatch_h_px"] * scale))

    calib   = {}
    colours = geom["swatch_colours"]

    for i, colour_name in enumerate(colours):
        cx = int(swatch_cx_cy[i, 0])
        cy = int(swatch_cx_cy[i, 1])

        # Sample the central half of the swatch to avoid border/hatch artefacts
        inset = max(6, sw_px // 5)
        x0 = max(0, cx - sw_px // 2 + inset)
        x1 = min(img_np.shape[1], cx + sw_px // 2 - inset)
        y0 = max(0, cy - sh_px // 2 + inset)
        y1 = min(img_np.shape[0], cy + sh_px // 2 - inset)

        patch = img_np[y0:y1, x0:x1]
        if patch.size == 0:
            log.warning("Empty patch for %s swatch — skipping", colour_name)
            continue

        hue_data = _sample_hue(patch)
        if hue_data is None:
            # Box was left unfilled — silently skip; partial calibration is fine.
            log.info("%s swatch not filled — will use default hue range", colour_name)
            continue

        mean_hue, spread = hue_data
        tolerance = max(spread * 1.5, 8.0)
        calib[colour_name] = {
            "hue":    round(float(mean_hue), 1),
            "hue_lo": round(float(mean_hue - tolerance), 1),
            "hue_hi": round(float(mean_hue + tolerance), 1),
        }
        log.info("Calibrated %-8s hue=%5.1f  ±%.1f", colour_name,
                 mean_hue, tolerance)

    if not calib:
        raise RuntimeError(
            "No swatches were detected as filled in.\n"
            "Colour in at least one box with the matching pencil and try again.\n"
            "Tip: you only need to fill in the colours that are scanning incorrectly."
        )

    out_path = Path(gallery_dir) / CALIB_FILENAME
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(calib, f, indent=2)
    log.info("Calibration saved → %s", out_path)

    return calib


# ─────────────────────────────────────────────────────────────────────────────
# Calibration loading and hue-range building
# ─────────────────────────────────────────────────────────────────────────────

def load_calibration(gallery_dir: Path) -> "dict | None":
    """Load calibration.json from gallery_dir, or return None."""
    path = Path(gallery_dir) / CALIB_FILENAME
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        log.info("Loaded calibration: %d colours", len(data))
        return data
    except Exception as e:
        log.warning("Could not load calibration: %s", e)
        return None


def build_hue_ranges(calibration: "dict | None") -> list:
    """
    Convert calibration dict → list of (name, hue_lo, hue_hi) tuples
    in OpenCV HSV convention (hue 0-179).  Handles red wrap-around.
    Falls back to _DEFAULT_HUE_RANGES if calibration is None.
    """
    if calibration is None:
        return _DEFAULT_HUE_RANGES

    ranges = []
    for name, data in calibration.items():
        lo = data["hue_lo"]
        hi = data["hue_hi"]
        if lo < 0:
            ranges.append((name, int(lo + 180), 179))
            ranges.append((name, 0, int(hi)))
        elif hi > 179:
            ranges.append((name, int(lo), 179))
            ranges.append((name, 0, int(hi - 180)))
        else:
            ranges.append((name, int(lo), int(hi)))

    ranges.sort(key=lambda x: x[1])
    return ranges


_DEFAULT_HUE_RANGES = [
    # (name,    hue_lo, hue_hi)  — OpenCV HLS/HSV hue 0-179
    #
    # Derived from the reference analyser's colorsys boundaries (0-360°)
    # by dividing by 2, with extra tolerance for real pencil variation.
    #
    # Reference (colorsys 0-360°) → OpenCV 0-179:
    #   RED:     0-35 and 330-360  →  0-17  and 165-179
    #   YELLOW:  35-75  (L>55%)   →  17-37
    #   GREEN:   75-160            →  37-80
    #   CYAN:    160-210           →  80-105
    #   BLUE:    210-275           →  105-137
    #   MAGENTA: 275-330           →  137-165
    #
    # Widened slightly each side to cover real pencil variation.
    ("RED",      0,   17),
    ("RED",    163,  179),   # red wraps at both ends of the 0-179 scale
    ("YELLOW",  18,   37),   # yellow: also requires high lightness (checked in classifier)
    ("GREEN",   38,   80),
    ("CYAN",    81,  105),
    ("BLUE",   106,  137),
    ("MAGENTA",138,  162),
]


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sample_hue(patch_rgb: np.ndarray) -> "tuple[float,float] | None":
    """
    Return (circular_mean_hue, spread) for the ink pixels in a swatch.
    Returns None if the patch looks uncoloured (no saturated pixels).
    """
    if not _CV2:
        return None

    hsv = cv2.cvtColor(patch_rgb, cv2.COLOR_RGB2HSV)
    s   = hsv[:, :, 1]
    v   = hsv[:, :, 2]
    h   = hsv[:, :, 0].astype(np.float32)

    ink = (s > 15) | (v < 160)
    if ink.sum() < 10:
        return None

    ink_h = h[ink].ravel()
    ink_w = s[ink].astype(np.float32) / 255.0 + 0.15

    # Circular mean — handles red wrap-around at 0/179.
    # OpenCV hue is 0-179 (degrees/2).  We double the angles so they span
    # a full 0-2π circle, then halve the result back.
    # arctan2 returns -π..+π (i.e. -180°..+180°); % 360 maps that to 0-360°,
    # then dividing by 2 recovers the correct 0-179 OpenCV hue.
    # The old code used "+ 180 if negative" which folded cyan/blue/magenta
    # back onto the yellow/green range.
    angles = ink_h * (2 * np.pi / 180)
    sin_m  = np.average(np.sin(angles), weights=ink_w)
    cos_m  = np.average(np.cos(angles), weights=ink_w)
    mean_hue = float((np.arctan2(sin_m, cos_m) * 180 / np.pi) % 360) / 2

    r      = np.sqrt(sin_m**2 + cos_m**2)
    spread = min(float(np.sqrt(-2 * np.log(max(r, 1e-6)))) * 180 / np.pi, 30.0)

    return mean_hue, spread


def _get_aruco_dict(name: str):
    did = getattr(cv2.aruco, name, None)
    if did is None:
        raise RuntimeError(f"Unknown ArUco dict: {name}")
    return cv2.aruco.getPredefinedDictionary(did)


def _make_detector(aruco_dict):
    try:
        p = cv2.aruco.DetectorParameters()
        p.adaptiveThreshWinSizeMin    = 3
        p.adaptiveThreshWinSizeMax    = 53
        p.adaptiveThreshWinSizeStep   = 10
        p.minMarkerPerimeterRate      = 0.02
        p.maxMarkerPerimeterRate      = 0.8
        p.cornerRefinementMethod      = cv2.aruco.CORNER_REFINE_SUBPIX
        return cv2.aruco.ArucoDetector(aruco_dict, p)
    except AttributeError:
        return aruco_dict


def _detect_marker(grey, detector, target_id: int):
    try:
        corners_list, ids, _ = detector.detectMarkers(grey)
    except AttributeError:
        params = cv2.aruco.DetectorParameters_create()
        corners_list, ids, _ = cv2.aruco.detectMarkers(
            grey, detector, parameters=params)
    if ids is None:
        return None
    for i, mid in enumerate(ids.flatten()):
        if mid == target_id:
            return corners_list[i].reshape(4, 2)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    ap = argparse.ArgumentParser(description="TDI620 colour calibration")
    ap.add_argument("--generate",  action="store_true",
                    help="Generate calibration chart PNG/PDF")
    ap.add_argument("--calibrate", metavar="IMAGE",
                    help="Process a photo of the filled-in chart")
    ap.add_argument("--output",    default=".",
                    help="Output directory for chart files")
    ap.add_argument("--gallery",   default=str(Path.home() / "digitiser_gallery"),
                    help="Gallery directory for calibration.json")
    args = ap.parse_args()

    if args.generate:
        generate_chart(Path(args.output))

    if args.calibrate:
        from PIL import Image
        img = Image.open(args.calibrate)
        result = calibrate_from_image(img, Path(args.gallery))
        print("\nCalibration result:")
        for name, data in result.items():
            print(f"  {name:8s}: hue={data['hue']:5.1f}  "
                  f"range [{data['hue_lo']:5.1f} – {data['hue_hi']:5.1f}]")
