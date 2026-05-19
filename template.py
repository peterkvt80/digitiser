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

# ── Text content ──────────────────────────────────────────────────────────────
TITLE_TEXT = "MAKE YOUR OWN TELETEXT PAGE"
INSTRUCTIONS = [
    "1. Colour squares using ONE COLOUR PER RECTANGLE",
    "2. To change colour LEAVE ONE RECTANGLE EMPTY",
    "3. Add text using ONE RECTANGLE PER LETTER",
    "4. Background is BLACK - don't colour that in!",
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


def generate_template(output_dir=None, show=False) -> dict:
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

    # Text Rendering
    pil_canvas = Image.fromarray(canvas).convert("RGB")
    draw = ImageDraw.Draw(pil_canvas)

    # NEW SIZES: Half of 36pt and 30pt
    TITLE_SIZE_PT = 18
    INSTR_SIZE_PT = 15

    title_f = get_font(TELETEXT_FONT, TITLE_SIZE_PT)
    instr_f = get_font(TELETEXT_FONT, INSTR_SIZE_PT)

    # Title
    title_lines = text_wrap(TITLE_TEXT, title_f, (grid_x1 - grid_x0 - msz * 2), draw)
    ty = margin + (msz_total - (len(title_lines) * pt_to_px(TITLE_SIZE_PT))) // 2
    for line in title_lines:
        t_bbox = draw.textbbox((0, 0), line, font=title_f)
        tx = grid_x0 + (grid_x1 - grid_x0 - (t_bbox[2] - t_bbox[0])) // 2
        draw.text((tx, ty), line, fill=0, font=title_f)
        ty += pt_to_px(TITLE_SIZE_PT) * 1.2

    # Instructions
    tm = mm_to_px(PANEL_MARGIN_MM)
    max_w = pw - (2 * pb) - (2 * tm)
    y_cursor = panel_y0 + pb + tm

    for item in INSTRUCTIONS:
        lines = text_wrap(item, instr_f, max_w, draw)
        for line in lines:
            draw.text((panel_x0 + pb + tm, y_cursor), line, fill=0, font=instr_f)
            y_cursor += pt_to_px(INSTR_SIZE_PT) * 1.3  # Line height
        y_cursor += mm_to_px(4.0)  # Gap between items

    # Output
    canvas = np.array(pil_canvas.convert("L"))
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path / "template.png"), canvas)
    try:
        Image.fromarray(canvas).save(str(output_path / "template.pdf"), "PDF", resolution=PRINT_DPI)
    except PermissionError:
        print("PERMISSION DENIED: Close template.pdf first.")

    if show:
        cv2.imshow("TDI620", cv2.resize(canvas, None, fx=0.3, fy=0.3))
        cv2.waitKey(0)


if __name__ == "__main__":
    generate_template(show="--show" in sys.argv)