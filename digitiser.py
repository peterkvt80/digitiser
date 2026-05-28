"""
digitiser.py — Core CV pipeline for the TDI620 Digitiser.

Pipeline
--------
1. Detect four 4×4 ArUco corner markers → four grid corners
2. Perspective-warp grid region to 800×480
3. Pre-classify every cell as EMPTY | TEXT | GRAPHICS | WHITE_GFX
   (only TEXT cells run Tesseract — large speed gain)
4. Per-cell mid-row mode detection (ALPHA ↔ GRAPHICS)
5. Encode each row: control codes into user-left empty gaps,
   OCR for text, sixel-sample for graphics/white_gfx
6. Emit OL,0 header + OL,1..24 rows with ESC-encoded control codes

TTI encoding
------------
  ESC (0x1B) + (control_code + 0x40)
  Red alpha (0x01) → 0x1B 0x41   Red graphics (0x11) → 0x1B 0x51

Colour rules
------------
  NONE (unclassified / blank) is treated as empty background.
  Only RED GREEN YELLOW BLUE MAGENTA CYAN WHITE trigger graphics control codes.
  BLACK is never emitted as a control code.

White graphics (CELL_WHITE_GFX)
--------------------------------
  A cell shaded densely with an achromatic (grey/black) pencil — with no
  colour saturation — is classified as CELL_WHITE_GFX.  The user draws the
  outline of their graphic shape and fills it in; the sixel pattern is
  decoded from the fill density, exactly like coloured graphics cells.

  Detection gates (all must pass, checked after colour classification):
    1. Low saturation: avg_s < white_gfx_max_saturation (default 40)
       — rules out coloured pencils that happen to have high fill.
    2. High fill fraction: dark pixels / total pixels >= white_gfx_fill_threshold
       (default 0.30, i.e. 30%)
       — rules out text strokes (typically 5–20% fill) and blank paper.
    3. At least one sixel sub-cell has non-zero fill
       — same empty-cell guard used for coloured graphics.

  Emitted as ESC W (0x17 + 0x40 = 0x57 = 'W') white graphics control code,
  followed by the decoded sixel character.

Sixel encoding
--------------
  bit0=TL  bit1=TR  bit2=ML  bit3=MR  bit4=BL  bit5=BR
  bit5=0 → 0x20+(bits&0x1F)   bit5=1 → 0x60+(bits&0x1F)
"""

import logging
import re
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# ── Optional imports ──────────────────────────────────────────────────────────
try:
    import cv2
    _ = cv2.aruco.DICT_4X4_50
    _CV2 = True
except (ImportError, AttributeError):
    try:
        import cv2
        _ = cv2.aruco.DICT_6X6_50
        _CV2 = True
    except (ImportError, AttributeError):
        _CV2 = False
        log.error("opencv-contrib-python not installed — pip install opencv-contrib-python")

try:
    import pytesseract
    _TESS = True
except ImportError:
    _TESS = False
    log.error("pytesseract not installed — pip install pytesseract")

from calibration import build_hue_ranges, load_calibration, _DEFAULT_HUE_RANGES
_active_hue_ranges = _DEFAULT_HUE_RANGES


def load_calibration_for_config(config: dict):
    """Load calibration and update active hue ranges. Call at app startup."""
    global _active_hue_ranges
    from pathlib import Path
    gallery_dir = config.get("gallery_dir", Path.home() / "digitiser_gallery")
    calib = load_calibration(Path(gallery_dir))
    _active_hue_ranges = build_hue_ranges(calib)
    if calib:
        log.info("Colour classification: calibrated (%d colours)", len(calib))
    else:
        log.info("Colour classification: default hue ranges")


# ── Teletext colour tables ────────────────────────────────────────────────────
PALETTE = {
    "BLACK":   np.array([0,   0,   0],   dtype=float),
    "RED":     np.array([255, 0,   0],   dtype=float),
    "GREEN":   np.array([0,   255, 0],   dtype=float),
    "YELLOW":  np.array([255, 255, 0],   dtype=float),
    "BLUE":    np.array([0,   0,   255], dtype=float),
    "MAGENTA": np.array([255, 0,   255], dtype=float),
    "CYAN":    np.array([0,   255, 255], dtype=float),
    "WHITE":   np.array([255, 255, 255], dtype=float),
}

ALPHA_CODES = {
    "BLACK": 0x00, "RED": 0x01, "GREEN": 0x02, "YELLOW": 0x03,
    "BLUE":  0x04, "MAGENTA": 0x05, "CYAN": 0x06, "WHITE": 0x07,
}
GRAPHICS_CODES = {
    "BLACK": 0x10, "RED": 0x11, "GREEN": 0x12, "YELLOW": 0x13,
    "BLUE":  0x14, "MAGENTA": 0x15, "CYAN": 0x16, "WHITE": 0x17,
}

# Colours that trigger a control code when they change
# WHITE is the implicit initial state — never emit a WHITE control code
# at the start of a row, but DO emit it when transitioning BACK to white
# after another colour (so text after graphics reads correctly).
# NONE and BLACK are never emitted under any circumstances.
SUPPRESS_CTRL_COLOURS = {"NONE", "BLACK"}   # never emit as control codes
SUPPRESS_COLOURS      = {"NONE", "BLACK"}   # cells that produce no output

CELL_EMPTY     = 0
CELL_TEXT      = 1
CELL_GRAPHICS  = 2
CELL_WHITE_GFX = 3   # achromatic dense-fill → white graphics control code

_MIN_MODE_RUN = 3   # minimum consecutive cells to commit to a mode switch


class DigitiserError(Exception): pass
class MarkerNotFoundError(DigitiserError): pass
GridNotFoundError = MarkerNotFoundError   # alias for gui.py


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def digitise(pil_image: Image.Image, config: dict,
             row_callback=None) -> str:
    """
    Digitise a design sheet photo to a TTI string.

    Parameters
    ----------
    pil_image : PIL.Image
    config : dict
    row_callback : callable(row_index, warped_pil) | None
        Called after each row is processed with the row index (0-based)
        and the warped PIL image.  Runs on the worker thread — use
        ``root.after`` to marshal UI updates to the main thread.
    """
    if not _CV2:
        raise DigitiserError("opencv-contrib-python required — pip install opencv-contrib-python")
    if not _TESS:
        raise DigitiserError("pytesseract required — pip install pytesseract")

    log.info("Digitising %dx%d", pil_image.width, pil_image.height)
    img_np = np.array(pil_image.convert("RGB"))

    # Initial normalisation without ArUco reference (grey-world + stretch)
    img_np = _normalise_image(img_np)

    # Detect grid corners
    corners = _find_grid_corners_via_aruco(img_np, config)

    # Re-normalise using detected marker corners as black/white reference
    img_raw = np.array(pil_image.convert("RGB"))
    img_np  = _normalise_image(img_raw, aruco_corners=corners)

    warped = _perspective_warp(img_np, corners,
                               config["warp_width"], config["warp_height"])
    warped_pil = Image.fromarray(warped)
    rows, scan_data = _process_grid(warped, config, row_callback=row_callback,
                                    warped_pil=warped_pil)
    return _build_tti(rows, config)


def digitise_full(pil_image: Image.Image, config: dict,
                  row_callback=None) -> tuple:
    """
    Full digitise pipeline — returns ``(tti_str, warped_pil, scan_data)``.

    ``scan_data`` is a list of 24 rows, each a list of 40 per-cell dicts::

        {
          "cell_type":      CELL_EMPTY | CELL_TEXT | CELL_GRAPHICS | CELL_WHITE_GFX,
          "colour":         str  e.g. "RED", "WHITE", "NONE",
          "mode":           "ALPHA" | "GRAPHICS",
          "ocr_char":       str  — character written to TTI (space if empty),
          "sixel_char":     str  — raw sixel character (graphics cells only),
          "sixel_code":     int  — byte value of sixel_char,
          "subcell_fill":   list[float]  — 6 sub-cell fill fractions,
          "bits":           int  — 6-bit sixel bitmask,
          "sixel_colours":  list[str]  — per-sub-cell colour (6 entries),
                            e.g. ["RED","RED","NONE","RED","NONE","NONE"]
                            NONE = background / unset / paper.
                            Only populated for CELL_GRAPHICS / CELL_WHITE_GFX;
                            all-NONE for other cell types.
          "sixel_bg_colour": str | None — secondary colour in the cell's
                            two-colour palette, or None for single-colour cells.
                            Only populated for CELL_GRAPHICS / CELL_WHITE_GFX.
          "mean_lum":       float,
          "min_lum":        float,
          "max_lum":        float,
          "lum_spread":     float,
          "mean_hue":       float,  # OpenCV 0-179
          "mean_sat":       float,
          "mean_val":       float,
          "ink_pixels":     int,
          "ink_pct":        float,
        }

    Use this instead of ``digitise()`` when the Cell Inspector needs the
    exact data that went into the TTI.
    """
    if not _CV2:
        raise DigitiserError("opencv-contrib-python required")
    if not _TESS:
        raise DigitiserError("pytesseract required")

    log.info("digitise_full %dx%d", pil_image.width, pil_image.height)
    img_np = np.array(pil_image.convert("RGB"))
    img_np = _normalise_image(img_np)
    corners = _find_grid_corners_via_aruco(img_np, config)
    img_raw = np.array(pil_image.convert("RGB"))
    img_np  = _normalise_image(img_raw, aruco_corners=corners)
    warped  = _perspective_warp(img_np, corners,
                                config["warp_width"], config["warp_height"])
    warped_pil = Image.fromarray(warped)
    rows, scan_data = _process_grid(warped, config, row_callback=row_callback,
                                    warped_pil=warped_pil)
    tti = _build_tti(rows, config)
    return tti, warped_pil, scan_data


def _normalise_image(img: np.ndarray,
                     aruco_corners: "np.ndarray | None" = None) -> np.ndarray:
    """
    Correct for camera colour cast and exposure using the ArUco markers as
    black/white reference patches.

    The ArUco markers contain known-black pixels (the dark modules) and
    known-white pixels (the quiet-zone border and white modules).  Sampling
    these directly from the captured image gives exact per-image black and
    white reference levels — far more accurate than grey-world or histogram
    statistics, because the references are right there in the image.

    If aruco_corners is not provided (e.g. when called before detection),
    falls back to the grey-world + exposure-stretch approach.

    Steps
    -----
    1. Grey-world white balance to remove gross colour cast.
    2. If ArUco corners supplied: sample the marker quiet zones (white
       reference) and the marker black modules (black reference) to derive
       per-channel stretch parameters.
    3. Otherwise: stretch so the 98th-percentile luminance reaches 240.
    """
    img_f = img.astype(np.float32)

    # Step 1: grey-world white balance (always applied)
    mean_r = float(img_f[:,:,0].mean())
    mean_g = float(img_f[:,:,1].mean())
    mean_b = float(img_f[:,:,2].mean())
    overall = (mean_r + mean_g + mean_b) / 3.0
    if overall > 1.0:
        img_f[:,:,0] = np.clip(img_f[:,:,0] * (overall / mean_r), 0, 255)
        img_f[:,:,1] = np.clip(img_f[:,:,1] * (overall / mean_g), 0, 255)
        img_f[:,:,2] = np.clip(img_f[:,:,2] * (overall / mean_b), 0, 255)

    # Step 2: per-image levels from ArUco markers
    if aruco_corners is not None:
        try:
            white_ref, black_ref = _sample_marker_levels(img_f, aruco_corners)
            if white_ref is not None and black_ref is not None:
                log.debug("ArUco levels: white=%.0f black=%.0f",
                          white_ref, black_ref)
                # Linear stretch: map black_ref→0, white_ref→255
                span = max(white_ref - black_ref, 1.0)
                img_f = np.clip((img_f - black_ref) * (255.0 / span), 0, 255)
                return img_f.astype(np.uint8)
        except Exception as e:
            log.debug("ArUco level sampling failed: %s", e)

    # Step 3: fallback — exposure stretch from 98th percentile
    grey_f = 0.299*img_f[:,:,0] + 0.587*img_f[:,:,1] + 0.114*img_f[:,:,2]
    p98    = float(np.percentile(grey_f, 98))
    if p98 < 220:
        scale  = 240.0 / p98
        img_f  = np.clip(img_f * scale, 0, 255)
        log.debug("Exposure stretch ×%.3f (p98=%.0f)", scale, p98)

    return img_f.astype(np.uint8)


def _sample_marker_levels(img_f: np.ndarray,
                           aruco_corners: np.ndarray) -> tuple:
    """
    Sample the ArUco marker corners to extract black and white reference levels.

    The detected marker corners define the four vertices of the marker square.
    Within that square:
    - The quiet-zone (2-cell border) is printed white → white reference
    - The centre of the black border frame is printed black → black reference

    Parameters
    ----------
    img_f : float32 RGB array
    aruco_corners : (4,2) float array — four detected grid corners TL,TR,BR,BL

    Returns
    -------
    (white_ref, black_ref) floats, or (None, None) on failure
    """
    # We use all four markers.  aruco_corners is the 4 GRID corners (not marker
    # corners).  We can't directly access marker internals here without re-running
    # detection.  Use a simpler approach: the grid corners sit exactly at the
    # inward corners of the markers.  The marker extends msz pixels outward.
    # Sample a small patch ~1/4 msz outside each grid corner to hit the quiet zone.

    # For simplicity, use the overall image edge regions near the markers.
    # The top-left and top-right corners of the image (near TL and TR markers)
    # contain the quiet-zone white.
    H, W = img_f.shape[:2]

    tl = aruco_corners[0]   # grid TL corner pixel
    tr = aruco_corners[1]   # grid TR corner pixel
    br = aruco_corners[2]   # grid BR corner pixel
    bl = aruco_corners[3]   # grid BL corner pixel

    # Estimate marker size from grid width
    # The marker is MARKER_MM = 16mm.  At this scale, it's about
    # (grid_width_px / 40) * 2 pixels per mm.  Rough estimate: use 30px.
    msz_est = max(20, int(abs(tr[0] - tl[0]) / 40))

    white_samples = []
    black_samples = []

    corners_list = [tl, tr, br, bl]
    # Outward offsets for each grid corner to hit the marker quiet zone
    offsets_white = [(-msz_est//2, -msz_est//2),
                     ( msz_est//2, -msz_est//2),
                     ( msz_est//2,  msz_est//2),
                     (-msz_est//2,  msz_est//2)]
    # Inner offset to hit the black border of the marker
    offsets_black = [(-msz_est//4, -msz_est//4),
                     ( msz_est//4, -msz_est//4),
                     ( msz_est//4,  msz_est//4),
                     (-msz_est//4,  msz_est//4)]

    patch_r = max(3, msz_est // 6)

    for i, (cx, cy) in enumerate(corners_list):
        for offsets, samples in [(offsets_white, white_samples),
                                  (offsets_black, black_samples)]:
            ox, oy = offsets[i]
            px = int(cx + ox);  py = int(cy + oy)
            x0 = max(0, px - patch_r);  x1 = min(W, px + patch_r)
            y0 = max(0, py - patch_r);  y1 = min(H, py + patch_r)
            patch = img_f[y0:y1, x0:x1]
            if patch.size > 0:
                lum = float(0.299*patch[:,:,0].mean() +
                            0.587*patch[:,:,1].mean() +
                            0.114*patch[:,:,2].mean())
                samples.append(lum)

    if len(white_samples) >= 2 and len(black_samples) >= 2:
        # Use median to reject outliers
        white_ref = float(np.median(white_samples))
        black_ref = float(np.median(black_samples))
        # Sanity check: white must be significantly brighter than black
        if white_ref > black_ref + 30:
            return white_ref, black_ref

    return None, None


# Keep old name as alias
_white_balance = _normalise_image


# ─────────────────────────────────────────────────────────────────────────────
# ArUco detection — four corner markers
# ─────────────────────────────────────────────────────────────────────────────
# IDs: 0=TL, 1=TR, 2=BR, 3=BL
# Inward corner of each marker = grid corner:
#   M0 BR → grid TL,  M1 BL → grid TR,  M2 TL → grid BR,  M3 TR → grid BL
# ArUco corner order: 0=TL, 1=TR, 2=BR, 3=BL within each marker

# ArUco corner order within each detected marker: 0=TL, 1=TR, 2=BR, 3=BL
#
# Template marker placement (markers sit outside the grid, touching its corners):
#   M0 (TL): left edge = grid_x0, bottom edge = grid_y0
#             → grid TL corner = marker BL = index 3
#   M1 (TR): right edge = grid_x1, bottom edge = grid_y0
#             → grid TR corner = marker BR = index 2
#   M2 (BR): right edge = grid_x1, top edge = grid_y1
#             → grid BR corner = marker TR = index 1
#   M3 (BL): left edge = grid_x0, top edge = grid_y1
#             → grid BL corner = marker TL = index 0
_INWARD_CORNER = {0: 3, 1: 2, 2: 1, 3: 0}


def _find_grid_corners_via_aruco(img: np.ndarray, config: dict) -> np.ndarray:
    tp = config.get("template_params")
    if tp is None:
        raise DigitiserError(
            "No template_params in DIGITISER config.\n"
            "Run: python3 template.py  and paste output into config.py"
        )
    dict_name  = tp.get("aruco_dict", "DICT_4X4_50")
    corner_ids = tp.get("corner_ids", [0, 1, 2, 3])
    aruco_dict = _get_aruco_dict(dict_name)
    detector   = _make_detector(aruco_dict)
    grey       = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

    found = {}
    for preprocess in ("raw", "clahe", "adaptive"):
        if len(found) == 4:
            break
        if preprocess == "raw":
            proc = grey
        elif preprocess == "clahe":
            proc = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8,8)).apply(grey)
        else:
            proc = cv2.adaptiveThreshold(grey, 255,
                       cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 51, 10)
        for mid, corners in _detect_all_markers(proc, detector, corner_ids).items():
            if mid not in found:
                found[mid] = corners
                log.debug("Marker %d found (%s)", mid, preprocess)

    if len(found) < 3:
        missing = [i for i in corner_ids if i not in found]
        raise MarkerNotFoundError(
            f"Only {len(found)}/4 corner markers detected (missing IDs {missing}).\n"
            "• All four corner markers must be visible\n"
            "• Use LIGHTS ON for even illumination\n"
            "• Keep the sheet flat"
        )

    tl_id, tr_id, br_id, bl_id = corner_ids
    corners_4 = [
        found[tl_id][_INWARD_CORNER[tl_id]] if tl_id in found else None,
        found[tr_id][_INWARD_CORNER[tr_id]] if tr_id in found else None,
        found[br_id][_INWARD_CORNER[br_id]] if br_id in found else None,
        found[bl_id][_INWARD_CORNER[bl_id]] if bl_id in found else None,
    ]

    missing_idx = [i for i, c in enumerate(corners_4) if c is None]
    if missing_idx:
        corners_4 = _estimate_missing_corner(corners_4, missing_idx[0])
        log.warning("Marker %d missing — corner estimated", corner_ids[missing_idx[0]])

    result = np.array(corners_4, dtype=np.float32)
    log.info("Grid TL=(%d,%d) TR=(%d,%d) BR=(%d,%d) BL=(%d,%d)",
             result[0,0],result[0,1], result[1,0],result[1,1],
             result[2,0],result[2,1], result[3,0],result[3,1])
    return result


def _detect_all_markers(grey, detector, wanted_ids) -> dict:
    try:
        corners_list, ids, _ = detector.detectMarkers(grey)
    except AttributeError:
        params = cv2.aruco.DetectorParameters_create()
        corners_list, ids, _ = cv2.aruco.detectMarkers(grey, detector, parameters=params)
    if ids is None:
        return {}
    return {int(mid): corners_list[i].reshape(4, 2)
            for i, mid in enumerate(ids.flatten()) if mid in wanted_ids}


def _estimate_missing_corner(corners, missing_idx):
    c = corners[:]
    opposite = {0: 2, 1: 3, 2: 0, 3: 1}
    adj = [i for i in range(4) if i != missing_idx and i != opposite[missing_idx]]
    c[missing_idx] = (np.array(corners[adj[0]]) +
                      np.array(corners[adj[1]]) -
                      np.array(corners[opposite[missing_idx]]))
    return c


def _get_aruco_dict(name: str):
    did = getattr(cv2.aruco, name, None)
    if did is None:
        raise DigitiserError(f"Unknown ArUco dict: {name}")
    return cv2.aruco.getPredefinedDictionary(did)


def _make_detector(aruco_dict):
    try:
        p = cv2.aruco.DetectorParameters()
        p.adaptiveThreshWinSizeMin    = 3
        p.adaptiveThreshWinSizeMax    = 53
        p.adaptiveThreshWinSizeStep   = 10
        p.minMarkerPerimeterRate      = 0.02
        p.maxMarkerPerimeterRate      = 0.8
        p.polygonalApproxAccuracyRate = 0.05
        p.cornerRefinementMethod      = cv2.aruco.CORNER_REFINE_SUBPIX
        return cv2.aruco.ArucoDetector(aruco_dict, p)
    except AttributeError:
        return aruco_dict


def _perspective_warp(img, corners, dst_w, dst_h):
    dst = np.array([[0,0],[dst_w,0],[dst_w,dst_h],[0,dst_h]], dtype=np.float32)
    M   = cv2.getPerspectiveTransform(corners.astype(np.float32), dst)
    return cv2.warpPerspective(img, M, (dst_w, dst_h))


# ─────────────────────────────────────────────────────────────────────────────
# Cell type classification
# ─────────────────────────────────────────────────────────────────────────────

def _classify_cell_type(patch_grey, sc, sr, sixel_thresh, gfx_fill_thresh):
    """
    Classify a non-empty, non-background cell as TEXT or GRAPHICS.

    Primary signal: overall dark-pixel fraction + lightness variance.
    A hand-drawn letter (pencil stroke on white paper) has:
      - Low overall fill fraction (<35% of the cell is dark)
      - High luminance variance (dark strokes against bright background)
    A solid graphics block has:
      - High overall fill fraction (most of the cell is coloured)
      - Low variance (fairly uniform fill)

    The old rule "partial subcell fill → GRAPHICS" was wrong for hand-drawn
    text: a letter like 'I', 'l', 'T' only touches 1-2 of the 6 subcells
    and was wrongly classified as graphics.  We now use fill fraction and
    variance as primary signals, with subcell count as a tie-breaker.
    """
    h, w = patch_grey.shape

    # Overall dark-pixel fraction and variance — primary signals
    overall      = float((patch_grey < sixel_thresh).sum()) / max(1, patch_grey.size)
    lum_variance = float(patch_grey.astype(np.float32).var())

    if overall == 0.0:
        return CELL_EMPTY

    # High variance + low fill = text (dark strokes on white paper)
    # Threshold tuned for hand-drawn block capitals on dot-grid paper:
    #   letters typically fill 5-30% of the cell with high local contrast.
    if overall < 0.35 and lum_variance > 150:
        return CELL_TEXT

    # Low fill AND low variance = very faint mark; treat as text rather than
    # graphics (a barely-touched cell is more likely a faint letter than a
    # deliberately empty graphics block).
    if overall < 0.15:
        return CELL_TEXT

    # Subcell check for solid/partial block graphics
    sw = w / sc;  sh = h / sr
    filled = 0
    for sy in range(sr):
        for sx in range(sc):
            sub = patch_grey[int(sy*sh):int((sy+1)*sh), int(sx*sw):int((sx+1)*sw)]
            if sub.size > 0 and float(sub.mean()) < sixel_thresh:
                filled += 1
    total = sc * sr

    # Most subcells solidly filled → graphics
    if filled >= total - 1 and overall > gfx_fill_thresh:
        return CELL_GRAPHICS

    # Majority of subcells filled with moderate overall coverage → graphics
    if filled > total // 2 and overall > 0.35:
        return CELL_GRAPHICS

    # Default: treat as text (safer — OCR on a false-text cell produces a
    # space; a false-graphics cell produces a wrong glyph that corrupts layout)
    return CELL_TEXT


# ─────────────────────────────────────────────────────────────────────────────
# Sixel decoding
# ─────────────────────────────────────────────────────────────────────────────

def _decode_sixels(grey, y0, y1, x0, x1, cw, ch, sc, sr, sixel_thresh) -> str:
    """
    Sample 2×3 sixel subcells and return the teletext block-graphics char.

    Bit layout (row-major, TL first):
        bit0=TL  bit1=TR
        bit2=ML  bit3=MR
        bit4=BL  bit5=BR
    Encoding:
        bit5=0 → 0x20+(bits&0x1F)   bit5=1 → 0x60+(bits&0x1F)

    Uses the fixed sixel_thresh throughout (config "sixel_fill_threshold",
    default 200).  A previous adaptive per-cell threshold caused all six bits
    to fire on light-coloured or partially-filled cells, producing spurious
    0x7F output.  The fixed threshold matches the scan_data display and the
    Cell Inspector exactly, so TTI output and inspector readout are always
    consistent.

    SUBCELL_FILL_FRAC must match the value used in _process_grid Step 6.
    """
    SUBCELL_FILL_FRAC = 0.25   # must match scan_data recording in _process_grid

    sw = (x1 - x0) / sc
    sh = (y1 - y0) / sr
    bits = 0
    for bit_idx, (sy, sx) in enumerate([(r,c) for r in range(sr) for c in range(sc)]):
        px0 = int(x0 + sx*sw);  px1 = int(x0 + (sx+1)*sw)
        py0 = int(y0 + sy*sh);  py1 = int(y0 + (sy+1)*sh)
        patch = grey[py0:py1, px0:px1]
        if patch.size > 0:
            if float((patch < sixel_thresh).sum()) / patch.size > SUBCELL_FILL_FRAC:
                bits |= (1 << bit_idx)
    code = (0x60 + (bits & 0x1F)) if (bits & 0x20) else (0x20 + (bits & 0x1F))
    return chr(code)


# ─────────────────────────────────────────────────────────────────────────────
# Sixel-level colour analysis
# ─────────────────────────────────────────────────────────────────────────────

def _hue_distance(a: float, b: float) -> float:
    """Angular distance between two OpenCV hues (0-179)."""
    d = abs(a - b)
    return min(d, 179.0 - d)


def _colour_to_hue(name: str) -> "float | None":
    """
    Return the canonical OpenCV hue centre for a teletext colour name.
    Returns None for NONE/BLACK/WHITE (achromatic — no meaningful hue).
    """
    return {
        "RED":     0.0,
        "YELLOW":  27.0,
        "GREEN":   55.0,
        "CYAN":    90.0,
        "BLUE":    120.0,
        "MAGENTA": 150.0,
    }.get(name, None)


def _nearest_colour(avg_h: float, candidates: "list[str]") -> str:
    """
    Return whichever name in ``candidates`` has the closest hue to ``avg_h``.
    Candidates must all be chromatic (not NONE/BLACK/WHITE).
    """
    best, best_dist = candidates[0], float("inf")
    for name in candidates:
        h = _colour_to_hue(name)
        if h is not None:
            d = _hue_distance(avg_h, h)
            if d < best_dist:
                best_dist = d
                best = name
    return best


def _classify_sixel_colours(
    cell_rgb: np.ndarray,
    grey_patch: np.ndarray,
    bits: int,
    cell_colour: str,
    sc: int,
    sr: int,
    y0: int, y1: int,
    x0c: int, x1c: int,
    sixel_thresh: int,
    config: dict,
) -> "tuple[list[str], str | None]":
    """
    Classify the colour of each individual sixel sub-cell and determine the
    cell's two-colour palette (foreground + background).

    Teletext graphics cells are constrained to exactly two colours — the
    foreground colour (set sub-cells) and the background colour (unset
    sub-cells).  This function measures what colour each sub-cell actually
    contains and enforces that constraint.

    Parameters
    ----------
    cell_rgb   : (H,W,3) uint8 RGB patch of the full cell
    grey_patch : (H,W)   uint8 greyscale patch of the full cell
    bits       : 6-bit sixel bitmask already decoded by _decode_sixels
    cell_colour: dominant colour already classified for the whole cell
    sc, sr     : sixel columns (2) and rows (3)
    y0,y1,x0c,x1c : absolute pixel coordinates of the cell in the warped image
                    (used only so sub-cell patches are sliced from cell_rgb
                     using relative offsets)
    sixel_thresh : greyscale threshold for "dark pixel"
    config     : DIGITISER config dict

    Returns
    -------
    sixel_colours : list[str], length sc*sr
        Per-sub-cell colour name.  Unset (background) sub-cells that contain
        only paper/white return "NONE".  Every element is one of the teletext
        colour names or "NONE".

    bg_colour : str | None
        The secondary colour found in the unset sub-cells, or None if all
        sub-cells are the same colour (single-colour cell).

    Algorithm
    ---------
    1.  For each of the 6 sub-cells, crop the RGB patch and run
        _classify_colour on it.

    2.  Collect all distinct non-NONE colours found across all sub-cells.

    3.  Two-colour enforcement:
          a. If only one distinct colour found → single-colour cell.
             bg_colour = None.
          b. If exactly two distinct colours → fg = cell_colour,
             bg = the other one.
          c. If >2 distinct colours (noise / mixed pencil strokes) →
             keep cell_colour as fg; for every other colour, assign it to
             whichever of fg or the most-common secondary is hue-nearest.
             bg_colour = most-common secondary colour after reassignment.

    4.  Re-label each sub-cell using only the resolved fg/bg pair.
        Sub-cells that returned NONE are labelled as bg_colour (they are
        paper, i.e. background).
    """
    h_cell = cell_rgb.shape[0]
    w_cell = cell_rgb.shape[1]

    sw = w_cell / sc
    sh = h_cell / sr

    # ── Step 1: classify each sub-cell ───────────────────────────────────────
    raw_colours: list[str] = []
    for bit_idx, (sy, sx) in enumerate([(r, c) for r in range(sr) for c in range(sc)]):
        sub_x0 = int(sx * sw);       sub_x1 = int((sx + 1) * sw)
        sub_y0 = int(sy * sh);       sub_y1 = int((sy + 1) * sh)
        sub_rgb = cell_rgb[sub_y0:sub_y1, sub_x0:sub_x1]
        if sub_rgb.size == 0:
            raw_colours.append("NONE")
            continue
        colour = _classify_colour(sub_rgb, config)
        raw_colours.append(colour)

    # ── Step 2: gather distinct chromatic colours ─────────────────────────────
    chromatic = [c for c in raw_colours if c != "NONE"]
    if not chromatic:
        # All sub-cells look like paper — treat everything as background
        return ["NONE"] * (sc * sr), None

    from collections import Counter
    counts = Counter(chromatic)
    distinct = list(counts.keys())

    # ── Step 3: enforce two-colour constraint ─────────────────────────────────
    if len(distinct) == 1:
        # Single colour — straightforward
        fg_colour = distinct[0]
        bg_colour = None

    elif len(distinct) == 2:
        # Exactly two colours — fg is the cell's already-known dominant colour
        # (keeps consistency with the rest of the pipeline).  If cell_colour
        # isn't one of them (shouldn't happen, but guard anyway), use the
        # most common.
        fg_colour = cell_colour if cell_colour in distinct else counts.most_common(1)[0][0]
        bg_colour = next(c for c in distinct if c != fg_colour)

    else:
        # >2 colours — noise or mixed pencil.
        # fg = cell_colour (most common / already validated).
        # For the secondary: pick the most-common non-fg colour.
        fg_colour = cell_colour if cell_colour in counts else counts.most_common(1)[0][0]
        non_fg = [(c, n) for c, n in counts.most_common() if c != fg_colour]
        bg_colour = non_fg[0][0] if non_fg else None

        # Reassign minority colours: map each to whichever of fg/bg is hue-nearest
        if bg_colour is not None:
            pair = [fg_colour, bg_colour]
            pair_hues = [_colour_to_hue(p) for p in pair]
            reassigned: dict[str, str] = {}
            for c in distinct:
                if c in (fg_colour, bg_colour):
                    continue
                c_hue = _colour_to_hue(c)
                if c_hue is None:
                    reassigned[c] = bg_colour
                    continue
                dists = [
                    _hue_distance(c_hue, ph) if ph is not None else float("inf")
                    for ph in pair_hues
                ]
                reassigned[c] = pair[0] if dists[0] <= dists[1] else pair[1]
            raw_colours = [
                reassigned.get(c, c) for c in raw_colours
            ]

    # ── Step 4: build final per-sub-cell colour list ──────────────────────────
    # Sub-cells that returned NONE (paper/white, unset) become bg_colour.
    # If bg_colour is None (single-colour cell) they stay NONE.
    sixel_colours: list[str] = []
    for c in raw_colours:
        if c == "NONE":
            sixel_colours.append(bg_colour if bg_colour is not None else "NONE")
        else:
            sixel_colours.append(c)

    log.debug(
        "_classify_sixel_colours: fg=%s bg=%s per_cell=%s",
        fg_colour, bg_colour, sixel_colours,
    )
    return sixel_colours, bg_colour


# ─────────────────────────────────────────────────────────────────────────────
# Grid processing — per-cell mid-row mode detection
# ─────────────────────────────────────────────────────────────────────────────

def _process_grid(warped: np.ndarray, config: dict,
                  row_callback=None, warped_pil=None) -> tuple:
    """
    Return ``(row_strings, scan_data)``.

    ``row_strings`` — 24 TTI row strings (content for OL,1..OL,24).
    ``scan_data``   — 24×40 list-of-lists of per-cell dicts (see
                      ``digitise_full`` docstring for field descriptions).
    """
    COLS = config["cols"]
    ROWS = config["rows"]
    tp   = config.get("template_params") or {}
    sc   = tp.get("sixel_cols", 2)
    sr   = tp.get("sixel_rows", 3)
    H, W = warped.shape[:2]
    cw   = W / COLS
    ch   = H / ROWS

    empty_thresh      = config.get("empty_brightness_threshold", 235)
    gfx_fill_thresh   = config.get("graphics_fill_threshold", 0.15)
    sixel_thresh      = config.get("sixel_fill_threshold", 175)
    TEXT_SPREAD_THRESH = config.get("text_spread_threshold", 25)

    grey       = cv2.cvtColor(warped, cv2.COLOR_RGB2GRAY)
    if warped_pil is None:
        warped_pil = Image.fromarray(warped)
    row_strings = []
    scan_data   = []   # 24 rows × 40 cols of per-cell dicts

    for row in range(ROWS):
        y0 = int(row * ch)
        y1 = int(y0 + ch)

        # ── Step 1: pre-classify every cell ──────────────────────────────────
        cell_type    = []
        cell_colour  = []
        cell_sixel   = []   # pre-computed sixel char for GRAPHICS cells (avoids re-calling _decode_sixels)
        explicit_mode_at = {}   # populated after strip OCR in Step 2

        for col in range(COLS):
            x0c = int(col * cw);  x1c = int((col+1) * cw)
            pg  = grey[y0:y1, x0c:x1c]
            pr  = warped[y0:y1, x0c:x1c]

            if pg.size == 0:
                cell_type.append(CELL_EMPTY)
                cell_colour.append("NONE")
                continue

            # ── Border bleed guard ────────────────────────────────────────────
            # Edge cells (outermost row or column) may contain dark pixels from
            # the printed grid border intruding into the warped cell area.  Inset
            # the classification patches on the exposed edge(s) so those border
            # pixels are excluded.  Interior cells are never modified.
            border_inset = config.get("border_inset_px", 4)
            if border_inset > 0:
                on_top    = row == 0
                on_bottom = row == ROWS - 1
                on_left   = col == 0
                on_right  = col == COLS - 1
                if on_top or on_bottom or on_left or on_right:
                    bi = border_inset
                    r0 = bi if on_top    else 0
                    r1 = (y1 - y0) - bi if on_bottom else (y1 - y0)
                    c0 = bi if on_left   else 0
                    c1 = (x1c - x0c) - bi if on_right  else (x1c - x0c)
                    # Only apply if the inset still leaves a usable patch
                    if r1 > r0 + 2 and c1 > c0 + 2:
                        pg = pg[r0:r1, c0:c1]
                        pr = pr[r0:r1, c0:c1]

            # ── Three-way cell classifier ─────────────────────────────────────
            #
            # Priority order (matches user measurements):
            #   1. GRAPHICS — coloured pencil fill, detected by HSV saturation
            #   2. TEXT     — achromatic pencil stroke, detected by lum spread
            #   3. NONE     — blank paper, low saturation AND low spread
            #
            # Measured by cell inspector:
            #   graphics cell : high saturation (S >> 30)
            #   text cell     : low saturation (S ~17), high spread (~117)
            #   blank cell    : low saturation (S ~17), low spread  (~20)
            #
            # Step A: colour classify first.  _classify_colour returns NONE for
            # any cell whose ink pixels do not cluster around a teletext hue
            # with sufficient saturation.  Text and blank cells both fall
            # through to Step B.
            colour = _classify_colour(pr, config)
            if colour != "NONE":
                # Guard: if every sixel sub-cell reads as empty the cell
                # contains hue-matching colour but no actual filled area
                # (e.g. a faint tint from paper or bleed from a neighbour).
                # Treat it as EMPTY so no spurious all-set graphics block
                # appears in the TTI output.
                sixel_char = _decode_sixels(
                    grey, y0, y1, x0c, x1c, cw, ch, sc, sr, sixel_thresh
                )
                if ord(sixel_char) == 0x20:   # all six bits zero → truly empty
                    log.debug("R%d C%d: colour=%s but all sixels zero → EMPTY",
                              row, col, colour)
                    cell_type.append(CELL_EMPTY)
                    cell_colour.append("NONE")
                    cell_sixel.append(" ")
                else:
                    cell_type.append(CELL_GRAPHICS)
                    cell_colour.append(colour)
                    cell_sixel.append(sixel_char)   # reused by _encode_row
                continue

            # ── White graphics check ──────────────────────────────────────────
            # A cell shaded with an achromatic (grey/black) pencil — no colour
            # saturation but high fill fraction — is white graphics.  Checked
            # AFTER the colour classifier (which handles saturation > 40) so
            # there is no overlap: a coloured cell never reaches this gate.
            #
            # Gates:
            #   1. Low mean saturation of ink pixels (avg_s < white_gfx_max_sat)
            #      — already guaranteed by _classify_colour returning NONE, but
            #      we re-check explicitly for clarity and configurability.
            #   2. Overall dark-pixel fill fraction >= white_gfx_fill_threshold
            #      — separates dense shading from text strokes (5-20% fill).
            #   3. At least one sixel sub-cell non-zero — same empty-cell guard
            #      used for coloured cells.
            wgfx_fill_thresh = config.get("white_gfx_fill_threshold", 0.30)
            wgfx_max_sat     = config.get("white_gfx_max_saturation", 40)

            # Compute fill fraction and mean saturation on the (possibly inset /
            # grid-line-suppressed) patch.
            total_px   = pg.size
            dark_px    = int((pg < sixel_thresh).sum())
            fill_frac  = dark_px / max(1, total_px)

            hsv_pr = cv2.cvtColor(pr, cv2.COLOR_RGB2HSV)
            s_pr   = hsv_pr[:, :, 1].astype(np.float32)
            avg_s  = float(s_pr.mean())

            if fill_frac >= wgfx_fill_thresh and avg_s < wgfx_max_sat:
                sixel_char = _decode_sixels(
                    grey, y0, y1, x0c, x1c, cw, ch, sc, sr, sixel_thresh
                )
                if ord(sixel_char) == 0x20:   # all sixels zero → treat as empty
                    log.debug("R%d C%d: white_gfx fill=%.0f%% but all sixels zero → EMPTY",
                              row, col, fill_frac * 100)
                    cell_type.append(CELL_EMPTY)
                    cell_colour.append("NONE")
                    cell_sixel.append(" ")
                else:
                    log.debug("R%d C%d: WHITE_GFX fill=%.0f%% avg_s=%.0f",
                              row, col, fill_frac * 100, avg_s)
                    cell_type.append(CELL_WHITE_GFX)
                    cell_colour.append("WHITE")
                    cell_sixel.append(sixel_char)
                continue

            # Step B: spread separates text from blank.
            # Both text and blank have low saturation (~17), so saturation
            # cannot distinguish them.  Spread can: a pencil stroke creates
            # a large luminance range even when faint, while blank paper is
            # nearly uniform (measured ~20).
            #
            # Two-gate noise guard:
            #   Gate 1 — spread >= TEXT_SPREAD_THRESH (config default 25).
            #             Lowered from 40: lightly-drawn pencil cells measure
            #             spread 25-35 and were being missed entirely.
            #   Gate 2 — dark_px (absolute < sixel_thresh) >= min_ink_pixels
            #             AND spread >= noise_spread_limit (default 50).
            #             The dot-grid paper creates many pixels a few counts
            #             below paper-white, making adaptive counting unreliable.
            #             The absolute dark_px count (< sixel_thresh ≈ 175-200)
            #             and spread together cleanly separate real text from
            #             dot-grid noise.  noise_spread_limit of 50 keeps the
            #             gap: faint text spread 50+, dot-grid noise spread <45.
            lum_min    = float(pg.min())
            lum_spread = float(pg.max()) - lum_min
            if lum_spread >= TEXT_SPREAD_THRESH:
                ink_px_thresh    = config.get("min_ink_pixels",    20)
                spread_hi_thresh = config.get("noise_spread_limit", 50)
                min_lum_thresh   = config.get("text_min_lum",      100)
                dark_px = int((pg < sixel_thresh).sum())
                if (dark_px < ink_px_thresh
                        or lum_spread < spread_hi_thresh
                        or lum_min >= min_lum_thresh):
                    cell_type.append(CELL_EMPTY)
                    cell_colour.append("NONE")
                    cell_sixel.append(" ")
                    log.debug("R%d C%d: demoted to EMPTY (ink=%d spread=%.0f min_lum=%.0f)",
                              row, col, dark_px, lum_spread, lum_min)
                else:
                    cell_type.append(CELL_TEXT)
                    cell_colour.append("WHITE")
                    cell_sixel.append(" ")
            else:
                cell_type.append(CELL_EMPTY)
                cell_colour.append("NONE")
                cell_sixel.append(" ")

        # ── Step 2: strip OCR — one call per contiguous text run ──────────────
        # Build runs of TEXT cells bridging single-NONE gaps (inter-letter
        # spaces), but splitting on 2+ consecutive NONE cells so Tesseract
        # never sees wide blank paper regions where grain can look like chars.
        text_cols = [c for c in range(COLS) if cell_type[c] == CELL_TEXT]
        ocr_chars = {}
        if text_cols:
            inset_y   = max(2, int(ch * 0.08))
            margin_px = int(cw)          # 1-cell margin either side of run
            runs      = _build_text_runs(cell_type, COLS,
                                         max_gap=1, spread_thresh=TEXT_SPREAD_THRESH)
            for run_start, run_end in runs:
                px0 = max(0, int(run_start * cw) - margin_px)
                px1 = min(warped_pil.width, int((run_end + 1) * cw) + margin_px)
                run_chars = _ocr_row_strip(
                    warped_pil, y0 + inset_y, y1 - inset_y,
                    text_cols, cw, config,
                    x0=px0, x1=px1
                )
                ocr_chars.update(run_chars)

            log.debug("Row %d: text_cols=%s", row, text_cols)
            log.debug("Row %d: OCR runs=%s chars=%s", row,
                      [(rs, re) for rs, re in runs],
                      {c: v for c, v in ocr_chars.items() if v != " "})

            # Detect [GFX]/[ALF] annotations from strip OCR result
            explicit_mode_at = _detect_annotations(ocr_chars, text_cols)

        # ── Step 3: assign per-cell modes ─────────────────────────────────────
        cell_mode = _assign_cell_modes(cell_type, cell_colour, explicit_mode_at, COLS)

        # ── Step 5: encode row ────────────────────────────────────────────────
        row_str = _encode_row(warped, grey, warped_pil,
                               row, COLS, cw, ch, sc, sr,
                               cell_type, cell_colour, cell_mode,
                               ocr_chars, cell_sixel, config)
        row_strings.append(row_str)

        # ── Step 6: build per-cell scan_data record ───────────────────────────
        # Collect pixel stats and the exact values used by _encode_row so the
        # Cell Inspector can display what actually went into the TTI.
        row_cells = []
        for col in range(COLS):
            x0c = int(col * cw);  x1c = int((col + 1) * cw)
            cell_rgb_np  = warped[y0:y1, x0c:x1c]
            cell_grey_np = grey[y0:y1, x0c:x1c]

            # Pixel stats
            mean_lum = float(cell_grey_np.mean()) if cell_grey_np.size else 0.0
            min_lum  = float(cell_grey_np.min())  if cell_grey_np.size else 0.0
            max_lum  = float(cell_grey_np.max())  if cell_grey_np.size else 0.0
            lum_spread = max_lum - min_lum

            # HSV stats (same gate as classify_colour for consistency)
            hsv    = cv2.cvtColor(cell_rgb_np, cv2.COLOR_RGB2HSV) if cell_rgb_np.size else None
            if hsv is not None:
                h_ch = hsv[:, :, 0].astype(float)
                s_ch = hsv[:, :, 1].astype(float)
                v_ch = hsv[:, :, 2].astype(float)
                min_sat  = config.get("min_saturation", 12)
                dark_v   = config.get("dark_value_threshold", 160)
                ink_mask = (s_ch > min_sat) | (v_ch < dark_v)
                ink_pixels = int(ink_mask.sum())
                total_px   = cell_rgb_np.shape[0] * cell_rgb_np.shape[1]
                ink_pct    = ink_pixels / max(1, total_px) * 100
                mean_hue = float(h_ch[ink_mask].mean()) if ink_pixels else 0.0
                mean_sat = float(s_ch[ink_mask].mean()) if ink_pixels else 0.0
                mean_val = float(v_ch.mean())
            else:
                ink_pixels = ink_pct = mean_hue = mean_sat = mean_val = 0.0

            # Sixel bitmask — only meaningful for GRAPHICS cells.
            # For EMPTY/TEXT cells use the pre-computed value from cell_sixel
            # (always ' '/0x20 for non-GRAPHICS) so the inspector shows exactly
            # what went into the TTI with no independent recomputation.
            ct = cell_type[col]
            if ct in (CELL_GRAPHICS, CELL_WHITE_GFX):
                sixel_char   = cell_sixel[col]
                sixel_code   = ord(sixel_char)
                # Reconstruct subcell fill fractions for inspector display only
                subcell_fill = []
                bits         = 0
                SUBCELL_FILL_FRAC = 0.25
                sw_sub = (x1c - x0c) / sc
                sh_sub = ch / sr
                for bit_idx, (sy, sx) in enumerate(
                    [(r, c) for r in range(sr) for c in range(sc)]
                ):
                    px0 = int(x0c + sx * sw_sub);  px1 = int(x0c + (sx + 1) * sw_sub)
                    py0 = int(y0  + sy * sh_sub);  py1 = int(y0  + (sy + 1) * sh_sub)
                    patch = grey[py0:py1, px0:px1]
                    frac  = float((patch < sixel_thresh).sum()) / max(1, patch.size) if patch.size else 0.0
                    subcell_fill.append(frac)
                    if frac > SUBCELL_FILL_FRAC:
                        bits |= (1 << bit_idx)

                # ── Per-sub-cell colour analysis ──────────────────────────────
                # Run _classify_sixel_colours on the full cell RGB patch.
                # This determines which colour each of the 6 sub-cells contains
                # and resolves the two-colour palette (fg + bg).
                sixel_colours, sixel_bg_colour = _classify_sixel_colours(
                    cell_rgb_np,
                    cell_grey_np,
                    bits,
                    cell_colour[col],
                    sc, sr,
                    y0, y1, x0c, x1c,
                    sixel_thresh,
                    config,
                )
            else:
                # Not a graphics cell — zero all sixel fields
                sixel_char       = " "
                sixel_code       = 0x20
                subcell_fill     = [0.0] * (sc * sr)
                bits             = 0
                sixel_colours    = ["NONE"] * (sc * sr)
                sixel_bg_colour  = None

            ocr = ocr_chars.get(col, " ") or " "

            row_cells.append({
                "cell_type":       ct,
                "colour":          cell_colour[col],
                "mode":            cell_mode[col],
                "ocr_char":        ocr if ct == CELL_TEXT else " ",
                "sixel_char":      sixel_char,
                "sixel_code":      sixel_code,
                "subcell_fill":    subcell_fill,
                "bits":            bits,
                "sixel_colours":   sixel_colours,
                "sixel_bg_colour": sixel_bg_colour,
                "mean_lum":        mean_lum,
                "min_lum":         min_lum,
                "max_lum":         max_lum,
                "lum_spread":      lum_spread,
                "mean_hue":        mean_hue,
                "mean_sat":        mean_sat,
                "mean_val":        mean_val,
                "ink_pixels":      int(ink_pixels),
                "ink_pct":         float(ink_pct),
            })
        scan_data.append(row_cells)

        if row_callback is not None:
            try:
                row_callback(row, warped_pil)
            except Exception:
                pass  # never let a UI callback abort the pipeline

    return row_strings, scan_data


def _build_text_runs(cell_type: list, cols: int,
                     max_gap: int = 1,
                     spread_thresh: float = 40) -> list:
    """
    Find contiguous runs of TEXT cells, bridging single-NONE gaps only.

    A run starts at the first TEXT cell and extends to the next TEXT cell
    provided the gap between them contains at most ``max_gap`` consecutive
    non-TEXT columns.  Runs are split on 2+ consecutive NONE columns so
    that Tesseract never sees wide blank regions where paper grain can be
    mistaken for characters.

    Parameters
    ----------
    cell_type : list of int
        Per-column cell type (CELL_EMPTY, CELL_TEXT, CELL_GRAPHICS).
    cols : int
        Total number of columns (40).
    max_gap : int
        Maximum consecutive non-TEXT columns that may be bridged within a
        single run.  Default 1: bridge a single NONE cell (inter-letter
        space), but split on 2+ consecutive NONE cells.
    spread_thresh : float
        Unused — kept for call-site symmetry.

    Returns
    -------
    list of (first_col, last_col) inclusive
    """
    text_cols = [c for c in range(cols) if cell_type[c] == CELL_TEXT]
    if not text_cols:
        return []

    runs = []
    rs = re = text_cols[0]
    for c in text_cols[1:]:
        # Number of non-TEXT columns between the previous TEXT cell (re)
        # and the current one (c) is exactly c - re - 1.
        gap = c - re - 1
        if gap <= max_gap:
            re = c          # bridge the gap, extend the run
        else:
            runs.append((rs, re))
            rs = re = c     # start a new run
    runs.append((rs, re))
    return runs


def _detect_annotations(ocr_chars: dict, text_cols: list) -> dict:
    """
    Scan strip OCR results for [GFX], [GRA], [ALF], [TXT] annotations.

    The user writes these 5-character sequences in consecutive cells to
    force a mode switch.  We look for '[' followed by the 3-letter keyword
    and ']' across up to 5 consecutive text columns.

    Returns
    -------
    dict  col_index → "GRAPHICS" | "ALPHA"
        Keyed on the column containing '[' (the start of the annotation).
    """
    if not ocr_chars:
        return {}

    annotations = {}
    # Build ordered list of (col, uppercase_char) for text columns only
    seq = [(c, ocr_chars.get(c, ' ').upper()) for c in sorted(text_cols)]

    for i, (col, ch) in enumerate(seq):
        if ch != '[':
            continue
        # Read the next up to 4 entries
        tail = seq[i + 1: i + 5]
        word = ''.join(c for _, c in tail)
        if word.startswith('GFX') or word.startswith('GRA'):
            annotations[col] = 'GRAPHICS'
        elif word.startswith('ALF') or word.startswith('TXT'):
            annotations[col] = 'ALPHA'

    return annotations


def _assign_cell_modes(cell_type, cell_colour, explicit_mode_at, COLS):
    """
    Assign per-cell ALPHA/GRAPHICS mode.

    Rule (mirrors the reference analyser):
      - CELL_GRAPHICS with a real colour → GRAPHICS mode
      - CELL_WHITE_GFX                  → GRAPHICS mode (white graphics)
      - CELL_TEXT or CELL_EMPTY         → ALPHA mode
    Explicit [GFX]/[ALF] annotations override.

    The original run-length heuristic ("need 3+ consecutive graphics cells")
    was wrong: a single isolated coloured cell IS graphics mode and must emit
    a graphics control code, or the cell character lands in alpha mode and
    renders as the wrong glyph.
    """
    modes = []
    for col in range(COLS):
        if col in explicit_mode_at:
            modes.append(explicit_mode_at[col])
        elif cell_type[col] == CELL_WHITE_GFX:
            modes.append("GRAPHICS")
        elif (cell_type[col] == CELL_GRAPHICS and
              cell_colour[col] not in SUPPRESS_COLOURS):
            modes.append("GRAPHICS")
        else:
            modes.append("ALPHA")
    return modes




# ─────────────────────────────────────────────────────────────────────────────
# Row encoding
# ─────────────────────────────────────────────────────────────────────────────

def _place_code(out_chars: list, col: int, code: str):
    """
    Place a 2-byte ESC control code in the nearest free slot to the left of
    col, without overwriting a character already placed there.
    Falls back to col-1 (overwrite) if no free slot within 3 cells.
    """
    if col == 0:
        out_chars[0] = code
        return
    for back in range(col - 1, max(col - 4, -1), -1):
        if out_chars[back] == " ":
            out_chars[back] = code
            return
    out_chars[col - 1] = code


def _encode_row(warped, grey, warped_pil,
                row, COLS, cw, cell_h, sc, sr,
                cell_type, cell_colour, cell_mode,
                ocr_chars: dict, cell_sixel: list, config) -> str:
    """
    Build a 40-character TTI row string, mirroring the reference analyser.

    Reference rules (scan_to_tti in the reference main.py):
      - Each row starts in alpha/WHITE state
      - For each cell determine target mode (alpha=TEXT, graphics=colour cell)
        and target colour (WHITE for text, the colour name for graphics)
      - When mode or colour changes, place the ESC control code in the
        PREVIOUS column (col-1), or overwrite col 0 if at the start
      - Cell content: TEXT → OCR char, GRAPHICS → sixel char, EMPTY → space
      - NONE/BLACK/EMPTY cells are always space; no control codes for them

    ocr_chars  : dict  col_index → char  (from _ocr_row_strip)
    cell_sixel : list  pre-computed sixel char per column (from Step 1);
                 using this avoids re-calling _decode_sixels with a
                 potentially different adaptive threshold.
    """
    y0           = int(row * cell_h)
    y1           = int(y0 + cell_h)

    # Build the raw character array first (content only, no control codes yet)
    out_chars = [" "] * COLS
    for col in range(COLS):
        ct = cell_type[col]
        cn = cell_colour[col]
        mode = cell_mode[col]

        if ct == CELL_EMPTY or cn in SUPPRESS_COLOURS:
            out_chars[col] = " "
        elif ct == CELL_TEXT:
            # Strip OCR covers the whole run; use its result directly.
            # A space means Tesseract found nothing at this column.
            ch_str = ocr_chars.get(col, " ") or " "
            out_chars[col] = ch_str
        elif ct in (CELL_GRAPHICS, CELL_WHITE_GFX) and mode == "GRAPHICS":
            # Use the sixel char computed (and validated) during Step 1.
            # Do NOT call _decode_sixels again — its adaptive threshold can
            # differ from the Step 1 call and produce a different result.
            out_chars[col] = cell_sixel[col]
        else:
            out_chars[col] = " "

    # ── Insert control codes ─────────────────────────────────────────────────
    #
    # Row starts in ALPHA WHITE (teletext default).
    #
    # Text character rules:
    #   0x20-0x3F  blast-through — work in any mode, no control code needed.
    #   0x40-0x7E  require ALPHA WHITE to be active; emit ESC G if not already.
    #
    # Graphics cells always need their colour control code before them.
    # SUPPRESS_COLOURS (NONE, BLACK) produce no output and need no code.
    current_mode   = "ALPHA"
    current_colour = "WHITE"

    for col in range(COLS):
        ct   = cell_type[col]
        cn   = cell_colour[col]
        mode = cell_mode[col]

        # TEXT cells: cn="WHITE" but they DO produce output — check type first
        if ct == CELL_TEXT:
            ch = out_chars[col]
            char_code = ord(ch) if (ch and ch != " ") else 0x20
            if char_code >= 0x40:
                # Proper alpha character — needs ALPHA WHITE mode
                if current_mode != "ALPHA" or current_colour != "WHITE":
                    _place_code(out_chars, col, _esc(ALPHA_CODES["WHITE"]))
                    current_mode   = "ALPHA"
                    current_colour = "WHITE"
            # 0x20-0x3F: blast-through, works in any mode, no code needed
            continue

        # Empty or suppressed cells produce nothing
        if ct == CELL_EMPTY or cn in SUPPRESS_COLOURS:
            continue

        # GRAPHICS or WHITE_GFX cell — emit colour code if state has changed
        tgt_colour = cn   # "RED", "GREEN", … or "WHITE" for white-gfx
        if mode != current_mode or tgt_colour != current_colour:
            _place_code(out_chars, col, _esc(GRAPHICS_CODES.get(tgt_colour, 0x17)))
            current_mode   = mode
            current_colour = tgt_colour

    return "".join(out_chars)


# ─────────────────────────────────────────────────────────────────────────────
# OCR helpers
# ─────────────────────────────────────────────────────────────────────────────

def _quick_ocr(warped_pil, x0, y0, x1, y1, config) -> str:
    """OCR a cell for [GFX]/[ALF] annotations only — result is uppercased."""
    cfg = "--psm 8 --oem 1 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ[]"
    return _ocr_cell(warped_pil, x0, y0, x1, y1, cfg, uppercase=True)


def _ocr_cell(warped_pil, x0, y0, x1, y1, tess_cfg, uppercase=False) -> str:
    """
    OCR a single cell crop and return the best single character.

    uppercase=True  : used by _quick_ocr for [GFX]/[ALF] annotation detection.
    uppercase=False : used as fallback text OCR — preserves case, since TTI
                      alpha mode supports the full 0x20-0x7F character range.
    """
    m = max(2, int((x1 - x0) * 0.08))   # 8% inset on each side
    crop = warped_pil.crop((x0 + m, y0 + m, x1 - m, y1 - m))
    pw, ph = crop.size
    if pw < 2 or ph < 2:
        return " "
    scale = max(4, int(120 / max(pw, 1)))   # upscale so cell is ~120px wide
    crop = crop.resize((pw * scale, ph * scale), Image.LANCZOS)
    import PIL.ImageOps as _io_cell
    gn  = np.array(_io_cell.autocontrast(crop.convert("L"), cutoff=1))
    gn  = cv2.GaussianBlur(gn, (3, 3), 0)
    _, t = cv2.threshold(gn, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    try:
        txt = pytesseract.image_to_string(Image.fromarray(t), config=tess_cfg)
        txt = re.sub(r'\s+', '', txt.strip())
        if uppercase:
            txt = txt.upper()
        txt = re.sub(r'\[[A-Za-z]{2,4}\]', '', txt)
        return txt[0] if txt and 0x20 <= ord(txt[0]) <= 0x7E else " "
    except Exception:
        return " "


def _ocr_row_strip(warped_pil: Image.Image, y0: int, y1: int,
                   text_cols: list, cw: float, config: dict,
                   x0: int = 0, x1: int = None) -> dict:
    """
    OCR a horizontal strip in one Tesseract call using image_to_boxes,
    then map each detected character back to its absolute column index.

    x0, x1 : int
        Pixel bounds of the strip within the warped image.
        Defaults to the full row width.  Pass the run bounds (with margin)
        to restrict OCR to just the region that contains text.

    Returns
    -------
    dict  col_index → detected_char (str, single character or ' ')
    """
    if not text_cols:
        return {}

    import PIL.ImageOps as _io

    if x1 is None:
        x1 = warped_pil.width
    strip = warped_pil.crop((x0, y0, x1, y1))
    strip_grey = strip.convert("L")
    sw, sh = strip_grey.size   # sw = strip width, sh ≈ row height px

    # ── Pre-processing for faint pencil text ──────────────────────────────
    #
    # Root cause of faint-text failures: at ~40px row height Otsu sees a
    # near-unimodal histogram (very few ink pixels vs. many paper pixels)
    # and picks a threshold that swallows light grey strokes.  A uniform
    # 3× upscale made things worse — a 4800px-wide strip is slow and JPEG
    # grain scales up to the same size as the strokes.
    #
    # Fix: upscale the HEIGHT ONLY to TARGET_H (120 px ≈ 3× a 40px row).
    # Taller characters give Otsu a proper bimodal histogram.  Width is
    # kept at sw so the column→pixel mapping (cx / cw) stays correct.
    TARGET_H = 120
    arr = np.array(strip_grey, dtype=np.uint8)
    if sh < TARGET_H:
        arr = cv2.resize(arr, (sw, TARGET_H), interpolation=cv2.INTER_CUBIC)

    # Autocontrast (1% cutoff): maps the darkest ink to black regardless
    # of pencil shade or camera exposure.
    arr = np.array(_io.autocontrast(Image.fromarray(arr), cutoff=1))
    arr = cv2.GaussianBlur(arr, (3, 3), 0)
    otsu_thresh, t = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # ── Faint-stroke fallback ─────────────────────────────────────────────
    # When Otsu's threshold is very high (>200 after autocontrast) it almost
    # certainly failed — the ink pixels were too few or too light to create
    # a bimodal histogram and Otsu placed the threshold above them.  In that
    # case fall back to a fixed threshold that is more aggressive about
    # preserving faint strokes.  VAL 214-225 on the original cell maps to
    # roughly grey 160-200 after autocontrast+resize, so a threshold of 180
    # reliably catches these while rejecting plain paper (>220).
    if otsu_thresh > 200:
        log.debug("_ocr_row_strip: Otsu thresh=%.0f suspiciously high, "
                  "using fixed fallback thresh=180", otsu_thresh)
        _, t = cv2.threshold(arr, 180, 255, cv2.THRESH_BINARY)

    strip_bin = Image.fromarray(t)

    # Column mapping uses the ORIGINAL sw and cw (no horizontal scaling).
    # Note: backslash must be doubled to survive pytesseract's shell quoting;
    # square bracket characters are included without the problematic backslash.
    tess_cfg = (
        "--psm 6 --oem 1 "
        "-c tessedit_char_whitelist="
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        "0123456789 !#$%&()*+,-./:;<=>?@[]^_{|}~"
    )

    # Only populate columns that fall within this strip's x range AND were
    # pre-classified as CELL_TEXT.  The old code seeded result with ALL
    # text_cols for every run, so a later empty run would overwrite good
    # results from an earlier run via ocr_chars.update(result).
    strip_col_start = int(x0 / cw)
    strip_col_end   = int((x1 - 1) / cw)
    result = {col: " " for col in text_cols
              if strip_col_start <= col <= strip_col_end}
    try:
        boxes_str = pytesseract.image_to_boxes(strip_bin, config=tess_cfg)
    except Exception as e:
        log.warning("Tesseract error in _ocr_row_strip: %s", e)
        return result

    # image_to_boxes: char x1 y1 x2 y2 page  (Tesseract bottom-left origin)
    # x-coordinates are relative to the strip (which starts at x0 in the
    # warped image).  Add x0 to recover absolute warped coordinates.
    # Height was scaled but width was not, so no x-scaling is needed.
    text_cols_set = set(text_cols)
    for line in boxes_str.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        char = parts[0]
        if len(char) != 1 or not (0x20 <= ord(char) <= 0x7E):
            continue
        try:
            bx1, by1, bx2, by2 = int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
        except ValueError:
            continue

        # Centre-x in strip coords → absolute warped coords
        cx = x0 + (bx1 + bx2) / 2.0
        col_idx = int(cx / cw)

        # Primary: accept if centre lands in a TEXT column
        if col_idx in result and result[col_idx] == " ":
            result[col_idx] = char
            continue

        # Fallback: the bounding box centre can miss by one column for narrow
        # letters (e.g. 'l', 'i', 'y') whose stroke is slightly off-centre.
        # Accept the box if its left or right edge overlaps a TEXT column that
        # is still empty, preferring the column with the greater overlap.
        abs_bx1 = x0 + bx1
        abs_bx2 = x0 + bx2
        box_w   = max(1, abs_bx2 - abs_bx1)
        best_col, best_overlap = None, 0
        for candidate in (col_idx - 1, col_idx + 1):
            if candidate not in text_cols_set:
                continue
            if result.get(candidate, " ") != " ":
                continue
            c_left  = int(candidate * cw)
            c_right = int((candidate + 1) * cw)
            overlap = max(0, min(abs_bx2, c_right) - max(abs_bx1, c_left))
            if overlap > box_w * 0.3 and overlap > best_overlap:
                best_overlap = overlap
                best_col     = candidate
        if best_col is not None:
            result[best_col] = char

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Colour classification
# ─────────────────────────────────────────────────────────────────────────────

def _classify_colour(cell_rgb: np.ndarray, config: dict) -> str:
    """
    Classify the dominant ink colour in a cell.
    Returns one of: RED GREEN YELLOW BLUE MAGENTA CYAN NONE

    Works entirely in OpenCV HSV (H: 0-179, S: 0-255, V: 0-255) — the same
    space used by calibration and the cell inspector.

    Ink gate: S > min_saturation (default 30) — saturation is the most
    reliable signal for coloured pencil regardless of brightness.  V is
    deliberately not used as a gate because bright colours like yellow
    (V≈244) and green (V≈212) are high-V.

    Classification:
      1. Compute saturation-weighted circular mean hue of ink pixels.
      2. Look up in calibrated ranges (nearest centre hue wins when ranges
         overlap — resolves green/yellow ambiguity by saturation difference).
      3. Fall back to default hue boundaries if no calibration.
    """
    if cell_rgb.size == 0:
        return "NONE"

    # Inset 10% on each side to avoid cell-boundary bleed
    h_px, w_px = cell_rgb.shape[:2]
    ix = max(1, int(w_px * 0.10))
    iy = max(1, int(h_px * 0.10))
    patch = cell_rgb[iy:h_px-iy, ix:w_px-ix]
    if patch.size == 0:
        patch = cell_rgb

    hsv = cv2.cvtColor(patch, cv2.COLOR_RGB2HSV)
    h_ch = hsv[:, :, 0].astype(np.float32)   # 0-179
    s_ch = hsv[:, :, 1].astype(np.float32)   # 0-255
    v_ch = hsv[:, :, 2].astype(np.float32)   # 0-255

    min_sat = config.get("min_saturation", 30)
    ink_mask = s_ch > min_sat
    total = patch.shape[0] * patch.shape[1]

    # Need at least 3% of pixels to be saturated ink (lowered from 6% for
    # lightly-applied pencil strokes — yellow/green pencils produce S≈80-110).
    ink_count = int(ink_mask.sum())
    if ink_count / max(1, total) < 0.03:
        # Too few saturated pixels — not a colour cell.
        # The secondary low-saturation sweep was removed because pencil text
        # and blank paper also have S~17 and were being wrongly promoted into
        # the hue classifier, returning spurious colour results.
        log.debug("_classify_colour: NONE (ink_pct=%.1f%%)", ink_count / total * 100)
        return "NONE"

    ink_h = h_ch[ink_mask]
    ink_s = s_ch[ink_mask]

    # Reject low-saturation false positives — pencil strokes on dot-grid paper
    # or shadow/noise near a coloured cell can pass the ink pixel count gate
    # but have very low mean saturation. Real colours (even light magenta) have
    # mean ink-pixel S ≥ 54. False positives from paper texture measure S ≈ 15-25.
    # Threshold of 40 gives a clear gap on both sides.
    avg_s = float(np.mean(ink_s))
    if avg_s < 40:
        log.debug("_classify_colour: NONE (avg_s=%.1f below threshold)", avg_s)
        return "NONE"

    # Saturation-weighted circular mean hue (handles red wrap-around at 0/179)
    # Map H (0-179) to full circle (0-2π), compute circular mean, map back.
    weights = ink_s / 255.0
    angles  = ink_h * (2.0 * np.pi / 179.0)
    sin_m   = float(np.average(np.sin(angles), weights=weights))
    cos_m   = float(np.average(np.cos(angles), weights=weights))
    avg_h   = float((np.arctan2(sin_m, cos_m) * 179.0 / (2.0 * np.pi)) % 179.0)

    log.debug("_classify_colour: ink_pct=%.0f%%  avg_h=%.1f  avg_s=%.0f",
              ink_count / total * 100, avg_h, avg_s)

    # ── Calibrated lookup: nearest centre hue wins (resolves overlap) ─────────
    if _active_hue_ranges:
        # Build per-name centre from ranges (average of lo and hi)
        # Use angular distance on the 0-179 circle to handle red wrap-around
        best_name = None
        best_dist = float('inf')
        for name, h_lo, h_hi in _active_hue_ranges:
            centre = (h_lo + h_hi) / 2.0
            # Angular distance on 0-179 circle
            diff = abs(avg_h - centre)
            dist = min(diff, 179.0 - diff)
            # Only consider ranges that actually contain avg_h (with 4° tolerance)
            if h_lo - 4 <= avg_h <= h_hi + 4 and dist < best_dist:
                best_dist = dist
                best_name = name
        if best_name is not None:
            log.debug("  → %s (nearest centre, dist=%.1f)", best_name, best_dist)
            return best_name

    # ── Default hue boundaries (OpenCV 0-179) ─────────────────────────────────
    if avg_h <= 8 or avg_h > 165:
        return "RED"
    elif avg_h <= 22:
        return "YELLOW" if avg_s < 150 else "RED"
    elif avg_h <= 37:
        # Yellow vs green disambiguation using both hue and saturation.
        # Measured: yellow H≈27 S≈82, green H≈34 S≈103.
        # Primary split on hue (more reliable than saturation alone):
        # H<=30 strongly suggests yellow; H>30 suggests green.
        # Saturation is a secondary tiebreaker.
        if avg_h <= 30:
            return "YELLOW"
        elif avg_h >= 33:
            return "GREEN"
        else:
            # H 31-32: ambiguous — use saturation
            return "YELLOW" if avg_s < 95 else "GREEN"
    elif avg_h <= 80:
        return "GREEN"
    elif avg_h <= 105:
        return "CYAN"
    elif avg_h <= 137:
        return "BLUE"
    elif avg_h <= 165:
        return "MAGENTA"
    return "NONE"


# ─────────────────────────────────────────────────────────────────────────────
# TTI generation
# ─────────────────────────────────────────────────────────────────────────────

def _esc(code: int) -> str:
    return chr(0x1B) + chr(code + 0x40)


def _build_tti(rows: list, config: dict) -> str:
    """
    Assemble the TTI file.

    OL,0  = 8 spaces + up to 32 chars of header text = 40 chars total.
    Header text is plain — no strftime codes (time/date substitution is done
    by the broadcast inserter at transmission time, not here).
    OL,1..OL,24 = 24 data rows.
    CT,12,T = 12-second cycle time (not a carousel, single page).
    """
    header_text = config.get("header_line", "BLOCK PARTY 26")[:32]
    header      = ("        " + header_text).ljust(40)[:40]

    lines = [
        "DE,Captured by TDI620 Digitiser",
        "DS,inserter",
        "SP,tdi620_digitiser",
        "CT,12,T",
        "PS,8003", "MS,B", "PN,10000", "SC,0000", "CS,0",
        f"OL,0,{_sanitise(header)}",
    ]
    for i, row in enumerate(rows):
        lines.append(f"OL,{i+1},{_sanitise(row)}")
    return "\r\n".join(lines) + "\r\n"


def _sanitise(row: str) -> str:
    """
    Emit a TTI data row, counting logical teletext columns (not bytes).

    An ESC (0x1B) + following byte is a control sequence that occupies ONE
    logical teletext column but TWO bytes in the file.  Padding/truncation
    is done in column-space so control codes are never split or dropped.

    - Preserve ESC + next-byte sequences intact (2 bytes, 1 logical column)
    - Strip parity bit from all bytes per spec
    - Replace any other non-printable bytes with space
    """
    out  = []
    cols = 0   # logical column count
    i    = 0
    while i < len(row) and cols < 40:
        ch_byte = ord(row[i]) & 0x7F
        if ch_byte == 0x1B and i + 1 < len(row):
            # ESC control sequence — 2 bytes, 1 logical column
            next_byte = ord(row[i + 1]) & 0x7F
            out.append(chr(0x1B))
            out.append(chr(next_byte))
            i    += 2
            cols += 1
        elif 0x20 <= ch_byte <= 0x7F:
            out.append(chr(ch_byte))
            i    += 1
            cols += 1
        else:
            out.append(' ')
            i    += 1
            cols += 1
    # Pad to exactly 40 logical columns
    while cols < 40:
        out.append(' ')
        cols += 1
    return "".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# Debug utility
# ─────────────────────────────────────────────────────────────────────────────

def get_warped_image(pil_image: Image.Image, config: dict):
    if not _CV2:
        return None
    try:
        img_np  = _normalise_image(np.array(pil_image.convert("RGB")))
        corners = _find_grid_corners_via_aruco(img_np, config)
        warped  = _perspective_warp(img_np, corners,
                                    config["warp_width"], config["warp_height"])
        return Image.fromarray(warped)
    except Exception as e:
        log.error("get_warped_image: %s", e)
        return None
