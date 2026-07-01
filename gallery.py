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

# Teletext-legal page number range (3-digit page numbers; 1xx-8xx is the
# conventional user-page range — 9xx is reserved on many transmission
# systems, so it's excluded here).
PAGE_NUMBER_MIN = 100
PAGE_NUMBER_MAX = 899


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
        """
        Load page.tti's exact text content.

        Opened with newline="" (i.e. universal-newline translation
        disabled) so the file's line endings come back exactly as stored
        on disk — \\r\\n, with no extra translation in either direction.
        Without this, Path.read_text()'s default text-mode behaviour
        normalises any \\r\\n / \\r / \\n in the file to a bare \\n, which
        silently strips the \\r teletext hardware and TTI consumers expect.
        """
        if self.tti_path.exists():
            with open(self.tti_path, "r", encoding="latin-1", newline="") as f:
                return f.read()
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

    def _set_page_number(self, page_number: int):
        """
        Internal: write a new page_number into meta.json without any
        validation.  Validation (range + uniqueness) is the responsibility
        of GalleryManager.update_page_number — this method just persists
        the value once the caller has already confirmed it is legal.
        """
        self.meta["page_number"] = page_number
        meta_file = self.path / "meta.json"
        meta_file.write_text(json.dumps(self.meta, indent=2))
        log.info("Updated page number for %s to P%03d", self.path.name, page_number)


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


def _edit_tti_for_publish(tti_content: str, page_number: int, notes: str) -> str:
    """
    Apply the on-air edits to a TTI file's text for publishing:

      DE,...  →  DE,<notes>
          The description line is replaced with the page's notes, so the
          on-air copy carries a human-readable description (e.g. the
          artist/title) instead of the generic "Captured by TDI620
          Digitiser" text written at capture time.

      PN,...  →  PN,<page_number>00
          The page number field is replaced with the gallery page number
          followed by two trailing zero digits — the standard 5-digit TTI
          PN representation (e.g. page 105 → "PN,10500").

    Only the first DE line and first PN line are edited — TTI files
    produced by this app have exactly one of each, written by
    digitiser._build_tti.  Other lines (DS, SP, CT, PS, MS, SC, CS, OL,...)
    are passed through unchanged.

    ``tti_content`` may use "\\r\\n", "\\n", "\\r", or even a corrupted
    "\\r\\r\\n" (as produced by the pre-fix Windows double-newline-
    translation bug — see GalleryManager.save()/GalleryEntry.load_tti())
    line ending.  "\\r\\r\\n" is normalised to "\\r\\n" first: left as-is,
    ``splitlines()`` would treat the lone leading "\\r" as its own line
    boundary and turn every corrupted line into two (one blank), which
    would propagate the corruption into the published file instead of
    fixing it.  The output is always re-joined with "\\r\\n" (with a
    trailing "\\r\\n"), matching the format digitiser._build_tti produces,
    so on-air files always have correct, consistent TTI line endings
    regardless of the condition of the source file.
    """
    tti_content = tti_content.replace("\r\r\n", "\r\n")   # heal legacy corruption
    lines = tti_content.splitlines()
    de_done = pn_done = False
    new_lines = []
    for line in lines:
        if not de_done and line.startswith("DE,"):
            new_lines.append(f"DE,{notes}")
            de_done = True
            continue
        if not pn_done and line.startswith("PN,"):
            new_lines.append(f"PN,{page_number}00")
            pn_done = True
            continue
        new_lines.append(line)
    return "\r\n".join(new_lines) + "\r\n"


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

        # Write TTI.
        # newline="" disables Python's text-mode newline translation, which
        # would otherwise double every \r\n the content already contains
        # into \r\r\n on Windows (where the platform line separator is
        # \r\n, so each embedded \n gets re-translated to \r\n on top of
        # the existing \r).  On Linux this happens to be a no-op, which is
        # why the bug was Windows-only.  newline="" makes the write
        # byte-identical on every platform.
        tti_path = entry_dir / "page.tti"
        with open(tti_path, "w", encoding="latin-1", newline="") as f:
            f.write(tti_content)

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

    def update_page_number(self, entry: GalleryEntry, new_page_number: int):
        """
        Change the page number of an existing gallery entry.

        Enforces two rules required for a valid teletext page number:
          1. Range — must be between PAGE_NUMBER_MIN (100) and
             PAGE_NUMBER_MAX (899) inclusive.  Teletext page numbers are
             3 hex/decimal digits; 1xx-8xx is the conventional user-page
             range (9xx is reserved on many transmission systems).
          2. Uniqueness — no other entry currently in the gallery may
             already use this page number.

        Raises
        ------
        ValueError
            If the requested page number fails either check.  The message
            is suitable for direct display to the user.

        Notes
        -----
        Only meta.json is updated — the on-disk folder name (which embeds
        the page number purely for human readability when browsing the
        filesystem) is left untouched.  GalleryEntry.page_number always
        reads from meta.json, so this is sufficient to make the change
        take effect everywhere in the app (listbox, thumbnails, next-page
        allocation in refresh()).
        """
        if not isinstance(new_page_number, int) or isinstance(new_page_number, bool):
            raise ValueError("Page number must be a whole number.")

        if not (PAGE_NUMBER_MIN <= new_page_number <= PAGE_NUMBER_MAX):
            raise ValueError(
                f"Page number must be between {PAGE_NUMBER_MIN} and "
                f"{PAGE_NUMBER_MAX} (teletext page numbers are 3 digits, "
                f"1xx-8xx)."
            )

        for other in self._entries:
            if other.path != entry.path and other.page_number == new_page_number:
                raise ValueError(
                    f"Page number {new_page_number} is already used by "
                    f"another saved page ({other.display_name()})."
                )

        entry._set_page_number(new_page_number)
        self.refresh()
        log.info("Renumbered %s → P%03d", entry.path.name, new_page_number)

    def publish_all(self) -> list:
        """
        Publish every saved gallery page to <gallery_dir>/onair/.

        For each entry, page.tti is copied with two edits applied (see
        _edit_tti_for_publish): the DE line is replaced with the page's
        notes, and the PN line is replaced with the page number plus two
        trailing zeros.  The edited copy is written to
        ``onair/p<page_number>.tti`` (e.g. page 105 → ``onair/p105.tti``).

        Republishing is idempotent — existing files for the same page
        number are simply overwritten with the latest content.

        Returns
        -------
        list[Path]
            The onair file paths written, in gallery listing order.
        """
        onair_dir = self._dir / "onair"
        onair_dir.mkdir(parents=True, exist_ok=True)

        written = []
        for entry in self._entries:
            tti_content = entry.load_tti()
            if not tti_content:
                log.warning("Skipping %s — page.tti missing or empty", entry.path.name)
                continue
            edited = _edit_tti_for_publish(tti_content, entry.page_number, entry.notes)
            out_path = onair_dir / f"p{entry.page_number}.tti"
            # newline="" — see the comment in save() for why this matters
            # on Windows.
            with open(out_path, "w", encoding="latin-1", newline="") as f:
                f.write(edited)
            written.append(out_path)
            log.info("Published P%03d -> %s", entry.page_number, out_path)

        log.info("Publish complete: %d page(s) written to %s", len(written), onair_dir)
        return written

    def entries(self) -> list:
        return list(self._entries)

    def get_entry(self, index: int) -> "GalleryEntry | None":
        if 0 <= index < len(self._entries):
            return self._entries[index]
        return None

    def count(self) -> int:
        return len(self._entries)

    def repair_corrupted_line_endings(self) -> list:
        """
        One-time repair for page.tti files saved before the Windows
        double-newline-translation fix (see GalleryManager.save() and
        GalleryEntry.load_tti()).

        On Windows, the old code path opened page.tti in default text
        mode, which translates every "\\n" to the platform line
        separator on write.  Since the TTI content already contained
        "\\r\\n" line endings, that translation turned each one into
        "\\r\\r\\n" — an extra carriage return that most TTI/teletext
        tools render as a blank line after every row.  Files saved on
        Linux/Raspberry Pi OS were never affected (there "\\n" already
        *is* the platform separator, so the translation was a no-op).

        This scans every gallery entry's page.tti and, wherever
        "\\r\\r\\n" is found, rewrites the file with "\\r\\r\\n"
        collapsed back to "\\r\\n" — using the same newline="" binary-
        safe write as save(), so the repair itself cannot reintroduce
        the bug.  Files with no corruption are left untouched.

        Returns
        -------
        list[Path]
            The page.tti paths that were actually rewritten (i.e. were
            found to be corrupted).  An empty list means nothing needed
            repair.
        """
        repaired = []
        for entry in self._entries:
            tti_path = entry.tti_path
            if not tti_path.exists():
                continue
            with open(tti_path, "rb") as f:
                raw = f.read()
            if b"\r\r\n" not in raw:
                continue
            fixed = raw.replace(b"\r\r\n", b"\r\n")
            with open(tti_path, "wb") as f:
                f.write(fixed)
            repaired.append(tti_path)
            log.info("Repaired corrupted line endings in %s", tti_path)

        if repaired:
            log.info("Line-ending repair: fixed %d file(s)", len(repaired))
        else:
            log.info("Line-ending repair: no corrupted files found")
        return repaired

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
