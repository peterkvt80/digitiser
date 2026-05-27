import argparse
import logging
import sys
import numpy as np
from pathlib import Path

log = logging.getLogger(__name__)

# ── Page ─────────────────────────────────────────────────────────────────────
PRINT_DPI = 300
MM_PER_INCH = 25.4
PAGE_W_MM = 297.0
PAGE_H_MM = 210.0
MARGIN_MM = 8.0

# ── Markers & Grid (Critical Layout - Unchanged) ─────────────────────────────
ARUCO_DICT_NAME = "DICT_4X4_50"
CORNER_IDS = [0, 1, 2, 3]
MARKER_MM = 16.0
MARKER_BORDER_MM = 2.0

PANEL_BORDER_MM = 2.0
PANEL_MARGIN_MM = 4.0
PANEL_WIDTH_MM = 62.0
PANEL_GAP_MM = 4.0

GRID_COLS, GRID_ROWS = 40, 24
GRID_BORDER_PX = 6

# ── Calibration strip (drawn below the grid, between the lower ArUco markers) ─
# Six 2×2-cell rectangles, one per teletext colour, coloured in by the user.
# The strip geometry is deterministic from the grid geometry so digitiser.py
# can locate the rectangles without any extra file I/O.
CALIB_COLOURS = ["RED", "GRE", "YEL", "BLU", "MAG", "CYA"]
CALIB_COLOUR_MAP = {
    # Full teletext colour name keyed by 3-letter label
    "RED": "RED",
    "GRE": "GREEN",
    "YEL": "YELLOW",
    "BLU": "BLUE",
    "MAG": "MAGENTA",
    "CYA": "CYAN",
}

# ── Text content ──────────────────────────────────────────────────────────────
TITLE_TEXT = "MAKE YOUR OWN TELETEXT PAGE"
INSTRUCTIONS = [
    "• Draw with red, green, yellow, dark blue, light blue or purple pencils.",
    "• Use 1 colour per rectangle.",
    "• No tiny details (they get lost).",
    "• Leave the background blank.",
    "• We will colour it black later.",
    "• Write 1 letter per rectangle.",
    "• Use a black pencil for letters.",
]

# Write-in fields drawn below the instructions in the left panel.
# Each entry is (caption_text, box_height_mm).
WRITE_IN_FIELDS = [
    ("Credit me as:", 12.0),
    ("About me/my art:", 28.0),
]

TELETEXT_FONT = "teletext2.ttf"


def mm_to_px(mm: float) -> int:
    return round(mm / MM_PER_INCH * PRINT_DPI)


def pt_to_px(pt: float) -> int:
    return round((pt / 72) * PRINT_DPI)


def text_wrap(text, font, max_width, draw):
    lines = []
    words = text.split(' ')
    if not words: return lines

    current_line = words[0]
    for word in words[1:]:
        bbox = draw.textbbox((0, 0), current_line + ' ' + word, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current_line += ' ' + word
        else:
            lines.append(current_line)
            current_line = word
    lines.append(current_line)
    return lines


def get_font(font_name, size_pt):
    from PIL import ImageFont
    size_px = pt_to_px(size_pt)

    local_path = Path(__file__).parent / font_name
    if local_path.exists():
        return ImageFont.truetype(str(local_path), size_px)

    for fallback in ["cour.ttf", "Courier New.ttf", "DejaVuSansMono.ttf"]:
        try:
            return ImageFont.truetype(fallback, size_px)
        except:
            continue

    return ImageFont.load_default()


def calib_strip_geometry(grid_x0, grid_x1, grid_y1, cw, ch) -> dict:
    """
    Compute the pixel coordinates of the six calibration-strip rectangles
    (in the full-page image coordinate system) from the grid geometry.

    Each rectangle is 2 grid cells wide × 2 grid cells tall, drawn with the
    same border thickness as the grid border (GRID_BORDER_PX).  The six
    rectangles are evenly spaced and centred horizontally within the grid
    x-span, in the band between grid_y1 and the bottom page margin.

    The strip area height equals msz_total (the marker + quiet-zone height).
    The strip band starts at grid_y1.

    Returns
    -------
    dict with keys:
        "rects"      : list of 6 (x0, y0, x1, y1) tuples — outer border coords
        "colours"    : CALIB_COLOURS list (order matches rects)
        "border_px"  : border thickness used for the rectangles
        "strip_y0"   : top of the strip band
        "strip_y1"   : bottom of the strip band (= grid_y1 + msz_total)
    """
    msz_total = mm_to_px(MARKER_MM + 2 * MARKER_BORDER_MM)

    rect_w = int(cw * 2)
    rect_h = int(ch * 2)
    n      = len(CALIB_COLOURS)
    gap    = mm_to_px(3.0)   # 3mm horizontal gap between rectangles

    # Centre the six rectangles (with gaps) horizontally inside the grid x-span
    total_w  = rect_w * n + gap * (n - 1)
    strip_x0 = grid_x0 + (grid_x1 - grid_x0 - total_w) // 2

    # Centre vertically in the strip band
    strip_band_h = msz_total
    strip_band_y = grid_y1
    rect_y0 = strip_band_y + (strip_band_h - rect_h) // 2

    rects = []
    for i in range(n):
        rx0 = strip_x0 + i * (rect_w + gap)
        rects.append((rx0, rect_y0, rx0 + rect_w, rect_y0 + rect_h))

    return {
        "rects":     rects,
        "colours":   list(CALIB_COLOURS),
        "border_px": GRID_BORDER_PX,
        "strip_y0":  strip_band_y,
        "strip_y1":  strip_band_y + strip_band_h,
    }


def generate_template(output_dir=None, show=False) -> dict:
    """
    Generate the design-sheet template.

    Returns the calib-strip geometry dict so callers (and digitiser.py) can
    locate the colour rectangles without re-running this function.
    """
    if output_dir is None: output_dir = Path(".")

    try:
        import cv2
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        sys.exit("Required: pip install opencv-contrib-python Pillow")

    W, H = mm_to_px(PAGE_W_MM), mm_to_px(PAGE_H_MM)
    margin = mm_to_px(MARGIN_MM)
    msz = mm_to_px(MARKER_MM)
    msz_total = msz + 2 * mm_to_px(MARKER_BORDER_MM)
    pb, pw, pgap = mm_to_px(PANEL_BORDER_MM), mm_to_px(PANEL_WIDTH_MM), mm_to_px(PANEL_GAP_MM)

    panel_x0, panel_y0 = margin, margin
    panel_x1, panel_y1 = margin + pw, H - margin
    grid_x0, grid_y0 = panel_x1 + pgap, margin + msz_total
    grid_x1, grid_y1 = W - margin, H - margin - msz_total

    canvas = np.ones((H, W), dtype=np.uint8) * 255

    # Markers
    aruco_dict = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, ARUCO_DICT_NAME))
    for mid in CORNER_IDS:
        img = cv2.aruco.generateImageMarker(aruco_dict, mid, msz, 1)
        mx = grid_x0 if mid in [0, 3] else grid_x1 - msz
        my = grid_y0 - msz if mid in [0, 1] else grid_y1
        canvas[my:my + msz, mx:mx + msz] = img

    # Grid
    b = GRID_BORDER_PX
    ix0, iy0, ix1, iy1 = grid_x0 + b, grid_y0 + b, grid_x1 - b, grid_y1 - b
    cw, ch = (ix1 - ix0) / GRID_COLS, (iy1 - iy0) / GRID_ROWS
    for col in range(1, GRID_COLS):
        x = int(ix0 + col * cw)
        cv2.line(canvas, (x, iy0), (x, iy1), 120, 2)
    for row in range(1, GRID_ROWS):
        y = int(iy0 + row * ch)
        cv2.line(canvas, (ix0, y), (ix1, y), 120, 2)

    cv2.rectangle(canvas, (grid_x0, grid_y0), (grid_x1, grid_y1), 0, GRID_BORDER_PX)
    cv2.rectangle(canvas, (panel_x0, panel_y0), (panel_x1, panel_y1), 0, pb)

    # ── Calibration strip ─────────────────────────────────────────────────────
    # Six 2×2-cell rectangles between the lower ArUco markers.
    # Drawn in greyscale (black border only) — user colours each rectangle
    # with the matching pencil before scanning.
    strip_geom = calib_strip_geometry(grid_x0, grid_x1, grid_y1, cw, ch)

    pil_canvas = Image.fromarray(canvas).convert("RGB")
    draw = ImageDraw.Draw(pil_canvas)

    label_f = get_font(TELETEXT_FONT, 8)   # label sits above each rect

    for i, (rx0, ry0, rx1, ry1) in enumerate(strip_geom["rects"]):
        label = CALIB_COLOURS[i]
        bpx   = strip_geom["border_px"]

        # Border rectangle (black, same thickness as grid border)
        draw.rectangle([rx0, ry0, rx1 - 1, ry1 - 1],
                       outline=(0, 0, 0), width=bpx)

        # Label placed outside the box: above the top-left corner, flush with
        # the left border edge so it does not overlap the coloured area at all.
        try:
            t_bbox = draw.textbbox((0, 0), label, font=label_f)
            th = t_bbox[3] - t_bbox[1]
        except Exception:
            th = 12
        label_gap = 4   # px between label baseline and rect top edge
        draw.text((rx0, ry0 - th - label_gap), label,
                  fill=(0, 0, 0), font=label_f)

    # ── Text rendering ────────────────────────────────────────────────────────
    TITLE_SIZE_PT = 18
    INSTR_SIZE_PT = 11
    CAPTION_SIZE_PT = 9
    FIELD_LABEL_SIZE_PT = 8

    title_f   = get_font(TELETEXT_FONT, TITLE_SIZE_PT)
    instr_f   = get_font(TELETEXT_FONT, INSTR_SIZE_PT)
    caption_f = get_font(TELETEXT_FONT, CAPTION_SIZE_PT)
    field_f   = get_font(TELETEXT_FONT, FIELD_LABEL_SIZE_PT)

    # Title
    title_lines = text_wrap(TITLE_TEXT, title_f, (grid_x1 - grid_x0 - msz * 2), draw)
    ty = margin + (msz_total - (len(title_lines) * pt_to_px(TITLE_SIZE_PT))) // 2
    for line in title_lines:
        t_bbox = draw.textbbox((0, 0), line, font=title_f)
        tx = grid_x0 + (grid_x1 - grid_x0 - (t_bbox[2] - t_bbox[0])) // 2
        draw.text((tx, ty), line, fill=(0, 0, 0), font=title_f)
        ty += pt_to_px(TITLE_SIZE_PT) * 1.2

    # Instructions
    tm   = mm_to_px(PANEL_MARGIN_MM)
    tx0  = panel_x0 + pb + tm          # left edge of text inside panel
    max_w = pw - (2 * pb) - (2 * tm)   # max text width
    y_cursor = panel_y0 + pb + tm

    line_h   = pt_to_px(INSTR_SIZE_PT) * 1.3
    item_gap = mm_to_px(2.5)

    for item in INSTRUCTIONS:
        lines = text_wrap(item, instr_f, max_w, draw)
        for line in lines:
            draw.text((tx0, y_cursor), line, fill=(0, 0, 0), font=instr_f)
            y_cursor += line_h
        y_cursor += item_gap

    # ── Write-in fields ───────────────────────────────────────────────────────
    # Each field: bold caption then a ruled write-in box with light horizontal
    # lines for the user to write on.  A small gap separates the instruction
    # block from the first field.
    y_cursor += mm_to_px(5.0)   # gap between instructions and first field

    field_gap    = mm_to_px(4.0)   # gap between consecutive fields
    rule_spacing = mm_to_px(5.5)   # spacing between ruled lines inside box
    rule_colour  = (180, 180, 180) # light grey ruled lines
    border_colour = (0, 0, 0)

    for caption, box_h_mm in WRITE_IN_FIELDS:
        box_h = mm_to_px(box_h_mm)
        box_x0 = tx0
        box_x1 = panel_x1 - pb - tm
        box_y0 = y_cursor
        box_y1 = y_cursor + box_h

        # Caption above the box
        draw.text((box_x0, y_cursor), caption,
                  fill=(0, 0, 0), font=caption_f)
        cap_h = pt_to_px(CAPTION_SIZE_PT) * 1.3
        box_y0 = int(y_cursor + cap_h)
        box_y1 = box_y0 + box_h

        # Ruled lines inside the box (drawn before the border so border sits on top)
        rule_y = box_y0 + rule_spacing
        while rule_y < box_y1 - 2:
            draw.line([(box_x0 + 2, rule_y), (box_x1 - 2, rule_y)],
                      fill=rule_colour, width=1)
            rule_y += rule_spacing

        # Box border
        draw.rectangle([box_x0, box_y0, box_x1, box_y1],
                       outline=border_colour, width=2)

        y_cursor = box_y1 + field_gap

    # Output
    canvas = np.array(pil_canvas.convert("L"))
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path / "template.png"), canvas)
    try:
        Image.fromarray(canvas).save(str(output_path / "template.pdf"), "PDF",
                                      resolution=PRINT_DPI)
    except PermissionError:
        print("PERMISSION DENIED: Close template.pdf first.")

    if show:
        cv2.imshow("TDI620", cv2.resize(canvas, None, fx=0.3, fy=0.3))
        cv2.waitKey(0)

    return strip_geom


if __name__ == "__main__":
    generate_template(show="--show" in sys.argv)