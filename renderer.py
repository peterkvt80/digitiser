"""
renderer.py — Renders a TTI page onto a Tkinter Canvas.

Implements the full teletext Level 1 rendering spec per TTI_RENDERER_IMPLEMENTATION_GUIDE:

  - Proper state machine: fg/bg colour, alpha/graphics mode, separated,
    double-height, hold graphics, flash, conceal
  - ESC+letter decoding (0x1B + ch → ord(ch)-0x40)
  - Row 0: 8 leading spaces are in the TTI data (added by _build_tti in digitiser)
  - Blast-through range (0x40-0x5F): alphanumeric even in graphics mode
  - National character mapping (English by default)
  - Background colour: 0x1C (black bg), 0x1D (new bg = current fg)
  - Double-height: uses teletext4.ttf; subsequent row is skipped
  - Control codes fill their cell with current background colour
  - Graphics PUA mapping for teletext2.ttf (contiguous and separated)
  - Fallback bitmask renderer when font not available

Font mapping (teletext2.ttf / teletext4.ttf):
  Contiguous graphics:
    Pattern 0x00-0x1F (char 0x20-0x3F) → U+E680-E69F
    Pattern 0x20-0x3F (char 0x60-0x7F) → U+E6C0-E6DF
  Separated graphics:
    Pattern 0x00-0x1F (char 0x20-0x3F) → U+E6A0-E6BF
    Pattern 0x20-0x3F (char 0x60-0x7F) → U+E6E0-E6FF
"""

import tkinter as tk
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# ── Colour palette ────────────────────────────────────────────────────────────

COLOURS = [
    "#000000",  # 0 Black
    "#ff0000",  # 1 Red
    "#00ff00",  # 2 Green
    "#ffff00",  # 3 Yellow
    "#0000ff",  # 4 Blue
    "#ff00ff",  # 5 Magenta
    "#00ffff",  # 6 Cyan
    "#ffffff",  # 7 White
]

DEFAULT_FG = 7   # White
DEFAULT_BG = 0   # Black

# ── National character mapping — English ──────────────────────────────────────

NATIONAL_ENGLISH = {
    0x23: '\u00a3',   # £
    0x5b: '\u2190',   # ←
    0x5c: '\u00bd',   # ½
    0x5d: '\u2192',   # →
    0x5e: '\u2191',   # ↑
    0x5f: '\u0023',   # #
    0x60: '\u2014',   # —
    0x7b: '\u00bc',   # ¼
    0x7c: '\u2016',   # ‖
    0x7d: '\u00be',   # ¾
    0x7e: '\u00f7',   # ÷
    0x7f: '\ue65f',   # teletext2 special
}


def _apply_national(code: int, national_map: dict) -> str:
    return national_map.get(code, chr(code))


# ── Graphics character → teletext2/4 PUA codepoint ───────────────────────────

def _gfx_codepoint(ch_code: int, separated: bool) -> int:
    """
    Map a teletext graphics character byte to its teletext2.ttf PUA codepoint.

    From the spec:
      0x20-0x3F: pattern = ch - 0x20  (bits 0-4, 5-bit pattern 0x00-0x1F)
                 contiguous base = 0xE680, separated base = 0xE6A0
      0x60-0x7F: pattern = ch - 0x60  (bits 0-5, 6-bit pattern 0x00-0x3F)
                 contiguous base = 0xE6C0, separated base = 0xE6E0
      0x40-0x5F: blast-through (not handled here — caller must check)
    """
    if 0x20 <= ch_code <= 0x3F:
        pattern = ch_code - 0x20        # 0x00-0x1F
        base    = 0xE6A0 if separated else 0xE680
        return base + pattern
    elif 0x60 <= ch_code <= 0x7F:
        pattern = ch_code - 0x60        # 0x00-0x1F
        base    = 0xE6E0 if separated else 0xE6C0
        return base + pattern
    return ord(' ')


# ── TTI parser — produces a 25×40 cell grid ───────────────────────────────────

def parse_tti_to_grid(tti_content: str, national_option: int = 0) -> list:
    """
    Parse a TTI string into a 25-row × 40-col grid of cell dicts.
    Each cell: {char, fg, bg, graphics, separated, double_height}

    Returns list of 25 rows.
    """
    national_map = NATIONAL_ENGLISH   # extend for other languages as needed

    # Blank grid
    def blank_cell():
        return {'char': ' ', 'fg': DEFAULT_FG, 'bg': DEFAULT_BG,
                'graphics': False, 'separated': False, 'double_height': False}

    grid = [[blank_cell() for _ in range(40)] for _ in range(25)]

    # Parse PS for national option
    for raw_line in tti_content.splitlines():
        line = raw_line.strip()
        if line.startswith('PS,'):
            try:
                ps = int(line[3:], 16)
                lang_bits = (ps & 0x0380) >> 7
                # Only English fully implemented; others fall through
            except ValueError:
                pass

    parsed_rows = set()

    for raw_line in tti_content.splitlines():
        line = raw_line.strip()
        if not (line.startswith('OL,') or line.startswith('FL,')):
            continue
        parts = line.split(',', 2)
        if len(parts) < 3:
            continue
        try:
            row_idx = int(parts[1])
        except ValueError:
            continue
        if not (0 <= row_idx <= 24):
            continue

        data = parts[2]
        _parse_row(grid, row_idx, data, national_map)
        parsed_rows.add(row_idx)

    # Missing rows stay as blank cells (already initialised)
    return grid


def _parse_row(grid: list, row_idx: int, data: str, national_map: dict):
    """
    Parse one OL/FL data string into grid[row_idx], implementing the full
    teletext state machine per the spec.
    """
    fg           = DEFAULT_FG
    bg           = DEFAULT_BG
    graphics     = False
    separated    = False
    double_h     = False
    held_gfx     = ' '    # for Hold Graphics (0x1E)
    hold_active  = False

    col = 0

    # Row 0 now has 8 leading spaces in the TTI data itself (added by _build_tti).
    # No special prepending needed here.

    i = 0
    while i < len(data) and col < 40:
        ch_byte = ord(data[i]) & 0x7F   # strip parity bit

        # Decode ESC sequence
        if ch_byte == 0x1B:
            i += 1
            if i < len(data):
                ch_byte = (ord(data[i]) - 0x40) & 0x7F
            else:
                break

        # ── Control code (0x00-0x1F) ─────────────────────────────────────────
        if ch_byte < 0x20:
            # Update state
            if 0x00 <= ch_byte <= 0x07:
                fg       = ch_byte
                graphics = False
                hold_active = False
            elif 0x10 <= ch_byte <= 0x17:
                fg       = ch_byte - 0x10
                graphics = True
            elif ch_byte == 0x08:   # Flash
                pass   # not rendered
            elif ch_byte == 0x09:   # Steady
                pass
            elif ch_byte == 0x0C:   # Normal height
                double_h = False
            elif ch_byte == 0x0D:   # Double height
                double_h = True
            elif ch_byte == 0x18:   # Conceal
                pass   # simplified: show anyway
            elif ch_byte == 0x19:   # Contiguous graphics
                separated = False
            elif ch_byte == 0x1A:   # Separated graphics
                separated = True
            elif ch_byte == 0x1C:   # Black background
                bg = 0
            elif ch_byte == 0x1D:   # New background = current fg
                bg = fg
            elif ch_byte == 0x1E:   # Hold graphics
                hold_active = True
            elif ch_byte == 0x1F:   # Release graphics
                hold_active = False

            # Control codes display as space with current background
            display_char = held_gfx if (hold_active and graphics) else ' '
            grid[row_idx][col] = {
                'char':          display_char,
                'fg':            fg,
                'bg':            bg,
                'graphics':      graphics,
                'separated':     separated,
                'double_height': double_h,
            }
            col += 1
            i   += 1
            continue

        # ── Displayable character (0x20-0x7F) ────────────────────────────────
        if graphics:
            if 0x40 <= ch_byte <= 0x5F:
                # Blast-through: display as alphanumeric character
                display_char = _apply_national(ch_byte, national_map)
                gfx_mode     = False
            else:
                # Map to PUA glyph codepoint
                cp           = _gfx_codepoint(ch_byte, separated)
                display_char = chr(cp)
                gfx_mode     = True
                held_gfx     = display_char
        else:
            display_char = _apply_national(ch_byte, national_map)
            gfx_mode     = False

        grid[row_idx][col] = {
            'char':          display_char,
            'fg':            fg,
            'bg':            bg,
            'graphics':      gfx_mode,
            'separated':     separated,
            'double_height': double_h,
        }
        col += 1
        i   += 1

    # Fill remaining columns with default blank cells
    while col < 40:
        grid[row_idx][col] = {
            'char': ' ', 'fg': DEFAULT_FG, 'bg': DEFAULT_BG,
            'graphics': False, 'separated': False, 'double_height': False
        }
        col += 1


# ── Tkinter canvas renderer ────────────────────────────────────────────────────

class TeletextRenderer:
    """
    Renders a TTI page onto a Tkinter Canvas.

    Parameters
    ----------
    canvas : tk.Canvas
    font_path : str | None
        Path to teletext2.ttf.  teletext4.ttf is looked for alongside it.
        None → fall back to Courier.
    """

    def __init__(self, canvas: tk.Canvas, font_path: str = None):
        self._canvas = canvas
        self._font2_path = None
        self._font4_path = None
        self._tk_fonts   = {}
        self._fonts_confirmed = False   # True once teletext2 is verified in Tkinter
        self._last_grid  = None         # stored so <Configure> can re-render
        self._load_fonts(font_path)

    # ── Font loading ──────────────────────────────────────────────────────────

    def _load_fonts(self, font_path: str | None):
        candidates2 = []
        if font_path:
            candidates2.append(Path(font_path))
        candidates2 += [
            Path(__file__).parent / "teletext2.ttf",
            Path.home() / "teletext2.ttf",
            Path("/usr/share/fonts/truetype/teletext2.ttf"),
        ]
        for p in candidates2:
            if p.exists():
                self._font2_path = str(p)
                log.info("teletext2.ttf: %s", p)
                p4 = p.parent / "teletext4.ttf"
                if p4.exists():
                    self._font4_path = str(p4)
                    log.info("teletext4.ttf: %s", p4)
                break
        if not self._font2_path:
            log.info("teletext2.ttf not found — using Courier fallback")
            return

        # On Linux/Pi, Tk finds fonts via fontconfig.  A TTF sitting in the
        # app directory isn't automatically registered.  Copy the file(s) to
        # ~/.fonts and run fc-cache so fontconfig (and therefore Tk) can see
        # them.  This is a one-time operation per font version; subsequent
        # runs find the font already installed and skip the copy.
        self._ensure_fonts_installed()

    def _ensure_fonts_installed(self):
        """
        Copy teletext2.ttf (and teletext4.ttf if present) to ~/.fonts and
        refresh the fontconfig cache so Tkinter can load them by family name.
        Safe to call repeatedly — skips the copy if files are already there.
        """
        import shutil, subprocess, sys

        fonts_dir = Path.home() / ".fonts"
        try:
            fonts_dir.mkdir(exist_ok=True)
        except Exception as e:
            log.warning("Cannot create ~/.fonts: %s", e)
            return

        installed_any = False
        for src_str in [self._font2_path, self._font4_path]:
            if not src_str:
                continue
            src = Path(src_str)
            dst = fonts_dir / src.name
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                try:
                    shutil.copy2(str(src), str(dst))
                    log.info("Installed %s → %s", src.name, dst)
                    installed_any = True
                except Exception as e:
                    log.warning("Could not copy %s to ~/.fonts: %s", src.name, e)

        # Refresh fontconfig cache so Tk's next font families query sees them.
        # Only needed when we actually installed something, and only on Linux.
        if installed_any and sys.platform.startswith("linux"):
            try:
                subprocess.run(
                    ["fc-cache", "-f", str(fonts_dir)],
                    capture_output=True, timeout=10
                )
                log.info("fc-cache refreshed for %s", fonts_dir)
            except Exception as e:
                log.debug("fc-cache failed (non-fatal): %s", e)

    def _get_tk_fonts(self, cell_h: float):
        """
        Return (font2_spec, font4_spec) for the given cell height.
        font2 = normal height, font4 = double height.
        Results are cached by pixel size.

        Font size is set to 72% of cell height (down from 85%) so that
        descenders and the bottom of capital letters are not clipped by the
        cell background rectangle below.
        """
        size = max(6, int(cell_h * 0.72))
        if size in self._tk_fonts:
            return self._tk_fonts[size]

        if self._font2_path:
            try:
                import tkinter.font as tkfont

                name2 = f"_tt2_{size}"
                if name2 not in tkfont.names():
                    tkfont.Font(name=name2, family="teletext2",
                                size=-size, weight="normal")

                # Confirm the font family is actually available in Tkinter.
                # Check once and cache the result.
                if not self._fonts_confirmed:
                    families = [f.lower() for f in tkfont.families()]
                    self._fonts_confirmed = "teletext2" in families
                    if not self._fonts_confirmed:
                        log.warning(
                            "teletext2.ttf file found but font family not "
                            "registered in Tkinter — using bitmask fallback.\n"
                            "Tip: run  fc-cache -f ~/.fonts  then restart."
                        )

                f2 = name2

                if self._font4_path:
                    name4 = f"_tt4_{size * 2}"
                    if name4 not in tkfont.names():
                        tkfont.Font(name=name4, family="teletext4",
                                    size=-(size * 2), weight="normal")
                    f4 = name4
                else:
                    f4 = f2
            except Exception as e:
                log.debug("tkfont failed: %s", e)
                f2 = ("Courier", size, "bold")
                f4 = ("Courier", size * 2, "bold")
        else:
            f2 = ("Courier", size, "bold")
            f4 = ("Courier", size * 2, "bold")

        self._tk_fonts[size] = (f2, f4)
        return f2, f4

    # ── Public API ────────────────────────────────────────────────────────────

    def render_tti(self, tti_content: str):
        """Parse TTI and render to canvas."""
        self._last_grid = parse_tti_to_grid(tti_content)
        self._last_tti  = tti_content
        # Bind resize so the page re-renders whenever the canvas is resized.
        # Remove any previous binding first to avoid stacking.
        self._canvas.unbind("<Configure>")
        self._canvas.bind("<Configure>", self._on_configure)
        # Initial draw — pass 0 so _draw measures the canvas itself.
        self._draw(self._last_grid)

    def _on_configure(self, event):
        """Re-render when the canvas is resized, using the exact new dimensions."""
        if hasattr(self, "_last_grid") and self._last_grid is not None:
            self._draw(self._last_grid, canvas_w=event.width, canvas_h=event.height)

    def clear(self):
        self._canvas.delete("all")

    # ── Drawing ───────────────────────────────────────────────────────────────

    def _draw(self, grid: list, canvas_w: int = 0, canvas_h: int = 0):
        self.clear()

        # Measure the available canvas area.
        # <Configure> events pass exact inner dimensions; for direct calls
        # we flush layout and read winfo_width/height.
        if canvas_w <= 1 or canvas_h <= 1:
            self._canvas.update_idletasks()
            canvas_w = self._canvas.winfo_width()
            canvas_h = self._canvas.winfo_height()
        if canvas_w <= 1:
            canvas_w = 640
        if canvas_h <= 1:
            canvas_h = 500

        ROWS = 25   # row 0 = header + rows 1-24
        COLS = 40

        # ── Aspect-ratio constrained cell sizing ──────────────────────────────
        #
        # A teletext page on a 4:3 screen has 40 columns and 25 rows.
        # The cell aspect ratio is therefore:
        #   cell_w / cell_h = (4/3) / (40/25) = 100/120 = 5/6
        # i.e. cells are taller than wide.
        #
        # We find the largest integer cell_h such that both dimensions fit:
        #   COLS * cell_w <= canvas_w   →  COLS * cell_h * 5/6 <= canvas_w
        #   ROWS * cell_h <= canvas_h
        #
        # cell_h constrained by width:  cell_h <= canvas_w * 6 / (5 * COLS)
        # cell_h constrained by height: cell_h <= canvas_h / ROWS
        #
        # Take the floor of the minimum and ensure at least 1 px.

        cell_h = max(1, int(min(canvas_w * 6 / (5 * COLS),
                                canvas_h / ROWS)))
        cell_w = max(1, int(cell_h * 5 / 6))

        # Grid pixel size
        grid_w = cell_w * COLS
        grid_h = cell_h * ROWS

        # Centre the grid within the canvas
        x_off = (canvas_w - grid_w) // 2
        y_off = (canvas_h - grid_h) // 2

        # Fill the canvas background first so margins around the grid are black
        self._canvas.configure(bg=COLOURS[DEFAULT_BG])

        # Precompute integer column and row boundaries relative to the canvas,
        # built from cell_w / cell_h so every boundary is exact and adjacent
        # cells share exactly one pixel — no gaps, no overlaps.
        col_x = [x_off + c * cell_w for c in range(COLS + 1)]
        row_y = [y_off + r * cell_h for r in range(ROWS + 1)]

        font2, font4 = self._get_tk_fonts(cell_h)

        # Pre-scan for double-height rows
        dh_rows = set()
        for row_idx in range(ROWS):
            if any(grid[row_idx][col]['double_height'] for col in range(COLS)):
                dh_rows.add(row_idx)

        skip_next = False
        for row_idx in range(ROWS):
            if skip_next:
                skip_next = False
                continue
            self._draw_row_bg(grid, row_idx, col_x, row_y)
            if row_idx in dh_rows:
                skip_next = True

        skip_next = False
        for row_idx in range(ROWS):
            if skip_next:
                skip_next = False
                continue
            self._draw_row_glyphs(grid, row_idx, col_x, row_y, cell_w, cell_h, font2, font4)
            if row_idx in dh_rows:
                skip_next = True

    def _draw_row_bg(self, grid, row_idx, col_x, row_y):
        """Pass 1 — background rectangles only for one row."""
        y0 = row_y[row_idx]
        y1 = row_y[row_idx + 1]
        for col in range(40):
            cell   = grid[row_idx][col]
            x0     = col_x[col]
            x1     = col_x[col + 1]
            bg_hex = COLOURS[cell['bg'] & 0x07]
            bg_y1  = row_y[min(row_idx + 2, len(row_y) - 1)] if cell['double_height'] else y1
            self._canvas.create_rectangle(x0, y0, x1, bg_y1,
                                          fill=bg_hex, outline="")

    def _draw_row_glyphs(self, grid, row_idx, col_x, row_y, cell_w, cell_h, font2, font4):
        """Pass 2 — glyphs only for one row, drawn after all backgrounds."""
        y0 = row_y[row_idx]
        y1 = row_y[row_idx + 1]
        for col in range(40):
            cell   = grid[row_idx][col]
            x0     = col_x[col]
            x1     = col_x[col + 1]
            xc     = x0 + (x1 - x0) / 2
            fg_hex = COLOURS[cell['fg'] & 0x07]

            if cell['double_height']:
                font  = font4
                dh_y1 = row_y[min(row_idx + 2, len(row_y) - 1)]
                yc    = y0 + (dh_y1 - y0) / 2
                bg_y1 = dh_y1
            else:
                font  = font2
                yc    = y0 + (y1 - y0) / 2
                bg_y1 = y1

            ch = cell['char']
            if not ch or ch == ' ':
                continue

            if cell['graphics']:
                if self._font2_path and self._fonts_confirmed:
                    self._canvas.create_text(
                        xc, yc, text=ch, font=font,
                        fill=fg_hex, anchor=tk.CENTER,
                    )
                else:
                    self._draw_gfx_bitmask(ch, x0, y0, x1, bg_y1, fg_hex,
                                           cell['separated'])
            elif 0x20 <= ord(ch) <= 0x7E or ord(ch) > 0x7F:
                self._canvas.create_text(
                    xc, yc, text=ch, font=font,
                    fill=fg_hex, anchor=tk.CENTER,
                )

    def _draw_gfx_bitmask(self, ch: str, x0: int, y0: int, x1: int, y1: int,
                           colour: str, separated: bool):
        """
        Fallback renderer: draw 2×3 sixel bitmask rectangles.

        Bit layout: bit0=TL bit1=TR bit2=ML bit3=MR bit4=BL bit5=BR
        Pattern extraction:
          0x20-0x3F: pattern = ch & 0x1F  (5 bits, bit5 always 0)
          0x60-0x7F: pattern = ch & 0x3F  (6 bits)

        Note: ch here is already a PUA character if font is loaded,
        or the raw teletext char if not. We reverse-map from PUA if needed.
        """
        code = ord(ch)

        # Reverse PUA mapping if font was partially loaded
        if 0xE680 <= code <= 0xE69F:
            bits = code - 0xE680
        elif 0xE6A0 <= code <= 0xE6BF:
            bits = code - 0xE6A0
        elif 0xE6C0 <= code <= 0xE6DF:
            bits = (code - 0xE6C0) | 0x20
        elif 0xE6E0 <= code <= 0xE6FF:
            bits = (code - 0xE6E0) | 0x20
        elif 0x20 <= code <= 0x3F:
            bits = code & 0x1F
        elif 0x60 <= code <= 0x7F:
            bits = code & 0x3F
        else:
            return

        cw = x1 - x0
        ch_h = y1 - y0
        pw = cw // 2
        ph = ch_h // 3
        gap = 1 if separated else 0

        for bit_idx, (cx, cy) in enumerate(
            [(0,0),(1,0),(0,1),(1,1),(0,2),(1,2)]
        ):
            if bits & (1 << bit_idx):
                rx = x0 + cx * pw + gap
                ry = y0 + cy * ph + gap
                self._canvas.create_rectangle(
                    rx, ry, rx + pw - gap, ry + ph - gap,
                    fill=colour, outline=""
                )
