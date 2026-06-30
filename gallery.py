"""
gallery.py — Manages the TTI file gallery.

Each saved page is stored as:
  <gallery_dir>/
    P100_20260415_143022/
      page.tti          ← TTI content
      thumb.png         ← 200×150 thumbnail of the rendered page
      meta.json         ← metadata (timestamp, page number, notes)
"""

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)


class GalleryEntry:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.name
        meta_file = path / "meta.json"
        if meta_file.exists():
            with open(meta_file) as f:
                self.meta = json.load(f)
        else:
            self.meta = {}

    @property
    def page_number(self) -> int:
        return self.meta.get("page_number", 100)

    @property
    def timestamp(self) -> str:
        return self.meta.get("timestamp", "")

    @property
    def notes(self) -> str:
        return self.meta.get("notes", "")

    @property
    def tti_path(self) -> Path:
        return self.path / "page.tti"

    @property
    def thumb_path(self) -> Path:
        return self.path / "thumb.png"

    def load_tti(self) -> str:
        if self.tti_path.exists():
            return self.tti_path.read_text(encoding="latin-1")
        return ""

    def display_name(self) -> str:
        ts = self.timestamp[:16].replace("T", " ") if self.timestamp else "Unknown"
        base = f"P{self.page_number:03d}  {ts}"
        if self.notes:
            base += f"  — {self.notes}"
        return base

    def update_notes(self, notes: str):
        """
        Update the notes field for this entry and persist it to meta.json.
        Updates self.meta in place so the in-memory GalleryEntry (and any
        cached reference to it, e.g. in GalleryManager._entries) reflects
        the change immediately without requiring a full gallery reload.
        """
        self.meta["notes"] = notes
        meta_file = self.path / "meta.json"
        meta_file.write_text(json.dumps(self.meta, indent=2))
        log.info("Updated notes for %s", self.path.name)


# ── Sixel (block graphics) rendering helpers ──────────────────────────────────
# Used by GalleryManager._make_thumbnail to draw graphics cells.  Mirrors
# renderer.TeletextRenderer._draw_gfx_bitmask's reverse-mapping so a thumbnail
# graphics cell renders identically to the fallback bitmask path of the live
# Preview-tab canvas renderer.

def _gfx_bits_from_char(ch: str) -> "int | None":
    """
    Reverse-map a parsed cell's display character back to its 6-bit sixel
    pattern.  ``ch`` is whatever renderer.parse_tti_to_grid put in the
    cell's 'char' field for a graphics cell: a teletext2.ttf PUA codepoint
    (0xE680-0xE6FF) if the font was available when the grid was parsed, or
    the raw 0x20-0x7F sixel byte otherwise.

    Returns None if ch doesn't decode to a sixel pattern.
    """
    if not ch:
        return None
    code = ord(ch)
    if 0xE680 <= code <= 0xE69F:
        return code - 0xE680
    elif 0xE6A0 <= code <= 0xE6BF:
        return code - 0xE6A0
    elif 0xE6C0 <= code <= 0xE6DF:
        return (code - 0xE6C0) | 0x20
    elif 0xE6E0 <= code <= 0xE6FF:
        return (code - 0xE6E0) | 0x20
    elif 0x20 <= code <= 0x3F:
        return code & 0x1F
    elif 0x60 <= code <= 0x7F:
        return code & 0x3F
    return None


def _draw_sixel_block(draw: "ImageDraw.ImageDraw", x0: int, y0: int,
                      x1: int, y1: int, bits: int, fill_hex: str):
    """
    Draw a 2×3 grid of filled rectangles for the set sixel bits within the
    cell bounds (x0,y0)-(x1,y1).

    Bit layout: bit0=TL bit1=TR bit2=ML bit3=MR bit4=BL bit5=BR
    """
    cw   = x1 - x0
    ch_h = y1 - y0
    for bit_idx, (cx, cy) in enumerate(
            [(0, 0), (1, 0), (0, 1), (1, 1), (0, 2), (1, 2)]):
        if bits & (1 << bit_idx):
            rx0 = x0 + (cx * cw) // 2
            ry0 = y0 + (cy * ch_h) // 3
            rx1 = x0 + ((cx + 1) * cw) // 2
            ry1 = y0 + ((cy + 1) * ch_h) // 3
            draw.rectangle([rx0, ry0, rx1, ry1], fill=fill_hex)


class GalleryManager:
    """
    Parameters
    ----------
    gallery_dir : Path
        Root directory for gallery storage.
    """

    def __init__(self, gallery_dir: Path):
        self._dir = Path(gallery_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._entries: list[GalleryEntry] = []
        self._next_page = 100
        self.refresh()

    # ── Public API ────────────────────────────────────────────────────────────

    def refresh(self):
        """Reload the gallery from disk."""
        entries = []
        for p in sorted(self._dir.iterdir()):
            if p.is_dir() and (p / "page.tti").exists():
                entries.append(GalleryEntry(p))
        self._entries = entries

        # Determine next page number
        used = {e.page_number for e in entries}
        n = 100
        while n in used:
            n += 1
            if n > 899:
                n = 100
        self._next_page = n

        log.info("Gallery refreshed: %d entries", len(entries))

    def save(self, tti_content: str, captured_image: "Image.Image | None" = None,
             notes: str = "") -> GalleryEntry:
        """
        Save a TTI page to the gallery.

        Parameters
        ----------
        tti_content : str
            Full TTI file text.
        captured_image : PIL.Image.Image, optional
            Original captured photo (used for thumbnail if provided).
        notes : str
            Optional user notes.

        Returns
        -------
        GalleryEntry
            The newly created entry.
        """
        page_num = self._next_page
        timestamp = datetime.now().isoformat(timespec="seconds")
        folder_name = f"P{page_num:03d}_{timestamp.replace(':', '').replace('-', '').replace('T', '_')}"
        entry_dir = self._dir / folder_name
        entry_dir.mkdir(parents=True)

        # Write TTI
        tti_path = entry_dir / "page.tti"
        tti_path.write_text(tti_content, encoding="latin-1")

        # Write metadata
        meta = {
            "page_number": page_num,
            "timestamp": timestamp,
            "notes": notes,
        }
        (entry_dir / "meta.json").write_text(json.dumps(meta, indent=2))

        # Generate thumbnail
        thumb = self._make_thumbnail(tti_content, captured_image)
        thumb.save(entry_dir / "thumb.png")

        self.refresh()
        log.info("Saved page P%03d to %s", page_num, entry_dir)

        return GalleryEntry(entry_dir)

    def delete(self, entry: GalleryEntry):
        """Permanently delete a gallery entry."""
        shutil.rmtree(entry.path, ignore_errors=True)
        self.refresh()
        log.info("Deleted %s", entry.path)

    def update_notes(self, entry: GalleryEntry, notes: str):
        """Edit the notes field of an existing gallery entry in place."""
        entry.update_notes(notes)

    def entries(self) -> list:
        return list(self._entries)

    def get_entry(self, index: int) -> "GalleryEntry | None":
        if 0 <= index < len(self._entries):
            return self._entries[index]
        return None

    def count(self) -> int:
        return len(self._entries)

    # ── Thumbnail generation ──────────────────────────────────────────────────

    def _make_thumbnail(self, tti_content: str,
                        captured_image: "Image.Image | None") -> Image.Image:
        """
        Generate a 320×240 thumbnail using the renderer's parser so that
        ESC-encoded control codes and colour state are handled correctly.

        Both text (alphanumeric) and graphics (sixel block) cells are
        rendered, matching what the live TeletextRenderer canvas shows in
        the Preview tab.  Graphics cells are drawn as a 2×3 grid of filled
        rectangles for the set sixel bits — the same fallback bitmask
        approach renderer.TeletextRenderer uses when teletext2.ttf isn't
        available, which works fine even at thumbnail-cell pixel sizes.
        """
        from renderer import parse_tti_to_grid, COLOURS

        W, H = 320, 240
        img  = Image.new("RGB", (W, H), (0, 0, 0))
        draw = ImageDraw.Draw(img)

        cell_w = W / 40
        cell_h = H / 25   # rows 0-24

        # Pre-compute boundaries with round() so adjacent rows/cols share the
        # same pixel and there are no 1-px gaps or overlaps.
        row_y = [round(r * cell_h) for r in range(26)]
        col_x = [round(c * cell_w) for c in range(41)]

        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                size=max(6, int(cell_h * 0.75))
            )
        except Exception:
            font = ImageFont.load_default()

        grid = parse_tti_to_grid(tti_content)

        for row_idx in range(25):
            for col_idx in range(40):
                cell   = grid[row_idx][col_idx]
                x0     = col_x[col_idx]
                y0     = row_y[row_idx]
                x1     = col_x[col_idx + 1]
                y1     = row_y[row_idx + 1]

                bg_hex = COLOURS[cell['bg'] & 0x07]
                fg_hex = COLOURS[cell['fg'] & 0x07]

                # Draw background
                draw.rectangle([x0, y0, x1, y1], fill=bg_hex)

                ch = cell['char']
                if not ch or ch == ' ':
                    continue

                if cell['graphics']:
                    bits = _gfx_bits_from_char(ch)
                    if bits:
                        _draw_sixel_block(draw, x0, y0, x1, y1, bits, fg_hex)
                elif 0x20 <= ord(ch) <= 0x7E:
                    draw.text((x0, y0), ch, fill=fg_hex, font=font)

        return img
