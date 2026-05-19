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
        return f"P{self.page_number:03d}  {ts}"


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
                # Only draw printable ASCII text characters (skip spaces,
                # control positions, and PUA graphics — too small to render)
                if ch and ch != ' ' and not cell['graphics'] and 0x20 <= ord(ch) <= 0x7E:
                    draw.text((x0, y0), ch, fill=fg_hex, font=font)

        return img
