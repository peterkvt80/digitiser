"""
gui.py — Tkinter GUI for the TDI620 Digitiser app.

Three tabs:
  CAPTURE  — live camera preview, capture button
  PREVIEW  — teletext renderer + TTI source view
  GALLERY  — saved pages browser

Integrates with HardwareManager (GPIO buttons / LEDs) and LCDManager.
"""

import logging
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from PIL import Image, ImageTk

from config import DIGITISER, GALLERY_DIR, TTI_DEFAULTS, RENDERER
from camera import Camera
from digitiser import digitise, digitise_full, get_warped_image, GridNotFoundError, DigitiserError, load_calibration_for_config
from renderer import TeletextRenderer
from gallery import GalleryManager
from calibration import generate_chart, calibrate_from_image

log = logging.getLogger(__name__)

# ── Colour scheme ─────────────────────────────────────────────────────────────

C = {
    "bg":      "#000000",
    "panel":   "#000080",
    "accent":  "#ff0000",
    "text":    "#ffffff",
    "green":   "#00ff00",
    "yellow":  "#ffff00",
    "cyan":    "#00ffff",
    "dim":     "#444444",
    "entry_bg":"#001100",
}


class TeletextGUI(tk.Tk):
    """
    Main application window.

    Parameters
    ----------
    camera : Camera
        Initialised camera object.
    hardware : HardwareManager
        Hardware I/O manager (may be a no-op if no GPIO).
    lcd : LCDManager
        LCD display manager.
    gallery : GalleryManager
        Gallery storage manager.
    """

    def __init__(self, camera, hardware, lcd, gallery: GalleryManager):
        super().__init__()

        self.is_capturing = False
        self._camera = camera
        self._hw = hardware
        self._lcd = lcd
        self._gallery = gallery

        self._preview_running = False
        self._preview_thread = None
        self._stop_preview = threading.Event()
        self._preview_after_id = None
        self._latest_frame = None
        self._current_tti: str | None = None
        self._current_image: Image.Image | None = None
        self._current_warped_pil: Image.Image | None = None
        self._current_scan_data: list | None = None
        self._gallery_index = 0
        self._processing = False

        # Scanline overlay state
        self._tab_scan      = None
        self._scan_canvas   = None
        self._scan_line_id  = None
        self._scan_done_ids = []

        self.title("TDI620 Digitiser")
        self.configure(bg=C["bg"])
        self.geometry("960x680")
        self.resizable(True, True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Load per-user colour calibration (if any) before first digitise
        _calib_config = dict(DIGITISER)
        _calib_config["gallery_dir"] = GALLERY_DIR
        load_calibration_for_config(_calib_config)

        self._build_ui()
        self._bind_hardware()

        # Bind notebook tab-change to trigger camera warmup on Calibrate tab
        self._calib_warmup_id  = None
        self._calib_warmup_ctr = 0
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self._set_status("Ready — point camera at design sheet and press CAPTURE")
        self._lcd.show_message("ready")
        self._hw.set_ready()

        
    # ═══════════════════════════════════════════════════════════════════════════
    # UI Construction
    # ═══════════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        # ── Title bar ─────────────────────────────────────────────────────────
        title_bar = tk.Frame(self, bg=C["panel"], pady=5)
        title_bar.pack(fill=tk.X)

        tk.Label(
            title_bar, text="■ DIGITISER TDI620 0.0",
            font=("Courier", 18, "bold"), fg=C["text"], bg=C["panel"]
        ).pack(side=tk.LEFT, padx=12)

        # EXIT button — always visible, triggers clean shutdown
        tk.Button(
            title_bar, text="EXIT",
            command=self._on_close,
            font=("Courier", 11, "bold"),
            fg=C["text"], bg="#660000",
            activebackground=C["text"], activeforeground="#660000",
            relief=tk.FLAT, padx=10, pady=3, cursor="hand2"
        ).pack(side=tk.RIGHT, padx=12)

        tk.Label(
            title_bar, text="TELETEXT LEVEL 1  40×24",
            font=("Courier", 12), fg=C["yellow"], bg=C["panel"]
        ).pack(side=tk.RIGHT, padx=12)

        # ── Notebook tabs ──────────────────────────────────────────────────────
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook", background=C["bg"], borderwidth=0)
        style.configure("TNotebook.Tab",
                        background=C["panel"], foreground=C["text"],
                        font=("Courier", 12, "bold"), padding=(14, 5))
        style.map("TNotebook.Tab",
                  background=[("selected", C["accent"])],
                  foreground=[("selected", C["text"])])

        # ── Status bar ─────────────────────────────────────────────────────────
        self._status_var = tk.StringVar(value="Initialising...")
        tk.Label(
            self, textvariable=self._status_var,
            font=("Courier", 10), fg=C["green"], bg=C["bg"],
            anchor=tk.W, padx=8
        ).pack(fill=tk.X, side=tk.BOTTOM, pady=2)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self._tab_capture   = tk.Frame(self.notebook, bg=C["bg"])
        self._tab_preview   = tk.Frame(self.notebook, bg=C["bg"])
        self._tab_gallery   = tk.Frame(self.notebook, bg=C["bg"])
        self._tab_calibrate = tk.Frame(self.notebook, bg=C["bg"])

        self.notebook.add(self._tab_capture,   text="  CAPTURE  ")
        self.notebook.add(self._tab_preview,   text="  PREVIEW  ")
        self.notebook.add(self._tab_gallery,   text="  GALLERY  ")
        self.notebook.add(self._tab_calibrate, text=" CALIBRATE ")

        self._build_capture_tab()
        self._build_preview_tab()
        self._build_gallery_tab()
        self._build_calibrate_tab()

        # Start the camera preview (background grab + main-thread display pump)
        self._start_preview()

        
    # ── Capture Tab ───────────────────────────────────────────────────────────

    def _build_capture_tab(self):
        # Pack fixed-height widgets FIRST so they always claim their space
        # before the camera label is given whatever remains.

        # Button row — at the bottom, packed first so it can never be displaced
        btn_row = tk.Frame(self._tab_capture, bg=C["bg"])
        btn_row.pack(side=tk.BOTTOM, pady=8)

        self._btn_capture = self._make_btn(
            btn_row, "■  CAPTURE PAGE  ■",
            self._do_capture, fg="#000000", bg=C["yellow"]
        )
        self._btn_capture.pack(side=tk.LEFT, padx=8)

        self._make_btn(
            btn_row, "LOAD IMAGE FILE",
            self._load_from_file, fg=C["text"], bg=C["panel"]
        ).pack(side=tk.LEFT, padx=8)

        self._make_btn(
            btn_row, "LIGHTS ON/OFF",
            self._toggle_lights, fg=C["text"], bg="#003300"
        ).pack(side=tk.LEFT, padx=8)

        self._make_btn(
            btn_row, "GENERATE TEMPLATE",
            self._generate_template, fg="#000000", bg=C["cyan"]
        ).pack(side=tk.LEFT, padx=8)

        # Hint label — also fixed height, packed after buttons (i.e. above them)
        self._cam_hint = tk.Label(
            self._tab_capture,
            text="Align the sheet so the grid fills the frame",
            font=("Courier", 10), fg=C["dim"], bg=C["bg"]
        )
        self._cam_hint.pack(side=tk.BOTTOM)

        # Camera preview label — fills ALL remaining space after fixed widgets
        self._cam_label = tk.Label(
            self._tab_capture, bg="#111111",
            text="[ CAMERA INITIALISING... ]",
            font=("Courier", 14), fg=C["green"],
        )
        self._cam_label.pack(side=tk.TOP, fill=tk.BOTH, expand=True,
                             padx=8, pady=(8, 0))

    # ── Preview Tab ───────────────────────────────────────────────────────────

    def _build_preview_tab(self):
        # Split: teletext canvas (top) + TTI source (bottom)
        pane = tk.PanedWindow(self._tab_preview, orient=tk.VERTICAL,
                               bg=C["bg"], sashwidth=6, sashrelief=tk.FLAT)
        pane.pack(fill=tk.BOTH, expand=True)

        # Teletext canvas
        canvas_frame = tk.Frame(pane, bg=C["bg"])
        pane.add(canvas_frame, stretch="always")

        self._tt_canvas = tk.Canvas(
            canvas_frame, bg=C["bg"],
            highlightthickness=2, highlightbackground=C["panel"]
        )
        self._tt_canvas.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self._renderer = TeletextRenderer(
            self._tt_canvas,
            font_path=RENDERER.get("teletext_font")
        )

        # Placeholder text
        self._tt_canvas.create_text(
            320, 200,
            text="[ NO PAGE LOADED ]\n\nCapture a sheet to see it here.",
            font=("Courier", 14), fill=C["green"], justify=tk.CENTER,
            tags="placeholder"
        )

        # TTI source text box
        src_frame = tk.LabelFrame(
            pane, text=" TTI SOURCE ",
            font=("Courier", 9, "bold"), fg=C["green"], bg=C["bg"],
            labelanchor="nw"
        )
        pane.add(src_frame, stretch="never")

        self._tti_text = tk.Text(
            src_frame, height=7, bg=C["entry_bg"], fg=C["green"],
            font=("Courier", 9), insertbackground=C["green"],
            wrap=tk.NONE
        )
        tti_sb_y = tk.Scrollbar(src_frame, command=self._tti_text.yview)
        tti_sb_x = tk.Scrollbar(src_frame, orient=tk.HORIZONTAL,
                                 command=self._tti_text.xview)
        self._tti_text.configure(yscrollcommand=tti_sb_y.set,
                                  xscrollcommand=tti_sb_x.set)
        tti_sb_y.pack(side=tk.RIGHT, fill=tk.Y)
        tti_sb_x.pack(side=tk.BOTTOM, fill=tk.X)
        self._tti_text.pack(fill=tk.BOTH, expand=True)

        # Preview action buttons
        btn_row = tk.Frame(self._tab_preview, bg=C["bg"])
        btn_row.pack(pady=6)

        self._make_btn(btn_row, "SAVE TO GALLERY",
                       self._do_save, fg="#000000", bg=C["green"]
                       ).pack(side=tk.LEFT, padx=6)
        self._make_btn(btn_row, "EXPORT TTI FILE",
                       self._export_tti, fg=C["text"], bg=C["panel"]
                       ).pack(side=tk.LEFT, padx=6)
        self._make_btn(btn_row, "SAVE SCAN IMAGE",
                       self._save_scan_image, fg=C["text"], bg="#334400"
                       ).pack(side=tk.LEFT, padx=6)
        self._make_btn(btn_row, "SHOW WARPED",
                       self._show_warped, fg=C["text"], bg="#333300"
                       ).pack(side=tk.LEFT, padx=6)
        self._make_btn(btn_row, "◄ RECAPTURE",
                       lambda: self.notebook.select(0), fg=C["text"], bg="#440000"
                       ).pack(side=tk.LEFT, padx=6)

    # ── Gallery Tab ───────────────────────────────────────────────────────────

    def _build_gallery_tab(self):
        top = tk.Frame(self._tab_gallery, bg=C["bg"])
        top.pack(fill=tk.X, padx=8, pady=6)

        tk.Label(top, text="SAVED PAGES",
                 font=("Courier", 14, "bold"), fg=C["yellow"], bg=C["bg"]
                 ).pack(side=tk.LEFT)

        self._make_btn(top, "REFRESH", self._refresh_gallery,
                       fg="#000000", bg=C["cyan"]
                       ).pack(side=tk.RIGHT)

        # Listbox
        list_frame = tk.Frame(self._tab_gallery, bg=C["bg"])
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8)

        self._gallery_lb = tk.Listbox(
            list_frame,
            bg=C["entry_bg"], fg=C["green"],
            font=("Courier", 12),
            selectbackground=C["panel"], selectforeground=C["text"],
            activestyle="none", height=12
        )
        lb_sb = tk.Scrollbar(list_frame, command=self._gallery_lb.yview)
        self._gallery_lb.configure(yscrollcommand=lb_sb.set)
        self._gallery_lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lb_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._gallery_lb.bind("<Double-Button-1>", lambda _: self._load_gallery_entry())

        # Gallery thumbnail
        self._thumb_label = tk.Label(
            self._tab_gallery, bg=C["bg"],
            text="", width=32, height=10
        )
        self._thumb_label.pack(pady=4)
        self._gallery_lb.bind("<<ListboxSelect>>", self._on_gallery_select)

        # Gallery buttons
        btn_row = tk.Frame(self._tab_gallery, bg=C["bg"])
        btn_row.pack(pady=6)

        self._make_btn(btn_row, "LOAD & VIEW",
                       self._load_gallery_entry, fg="#000000", bg=C["yellow"]
                       ).pack(side=tk.LEFT, padx=6)
        self._make_btn(btn_row, "EXPORT TTI",
                       self._export_gallery_tti, fg=C["text"], bg=C["panel"]
                       ).pack(side=tk.LEFT, padx=6)
        self._make_btn(btn_row, "DELETE",
                       self._delete_gallery_entry, fg=C["text"], bg="#880000"
                       ).pack(side=tk.LEFT, padx=6)

        self._refresh_gallery()

    # ── Calibrate Tab ─────────────────────────────────────────────────────────

    def _build_calibrate_tab(self):
        """Calibration chart capture and per-user colour reference."""

        # ── Pack fixed-height widgets from the BOTTOM up first ────────────────
        # This ensures they always claim their space before the preview
        # label is given whatever remains — preventing them being pushed off.

        # Result label — very bottom
        self._calib_result = tk.Label(
            self._tab_calibrate, text="",
            font=("Courier", 10), fg=C["green"], bg=C["bg"],
            justify=tk.LEFT, anchor=tk.W
        )
        self._calib_result.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=2)

        # Button row
        btn_row = tk.Frame(self._tab_calibrate, bg=C["bg"])
        btn_row.pack(side=tk.BOTTOM, pady=6)

        self._make_btn(btn_row, "GENERATE CHART",
                       self._generate_calib_chart,
                       fg="#000000", bg=C["cyan"]
                       ).pack(side=tk.LEFT, padx=8)

        self._btn_calib_capture = self._make_btn(
                       btn_row, "■ CAPTURE CHART",
                       self._capture_calib_chart,
                       fg="#000000", bg=C["yellow"])
        self._btn_calib_capture.pack(side=tk.LEFT, padx=8)

        self._make_btn(btn_row, "LOAD IMAGE FILE",
                       self._load_calib_from_file,
                       fg=C["text"], bg=C["panel"]
                       ).pack(side=tk.LEFT, padx=8)

        self._make_btn(btn_row, "RESET TO DEFAULT",
                       self._reset_calibration,
                       fg=C["text"], bg="#440000"
                       ).pack(side=tk.LEFT, padx=8)

        # Colour swatch status row
        swatch_frame = tk.Frame(self._tab_calibrate, bg=C["bg"])
        swatch_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=4)

        self._calib_swatches = {}
        SWATCH_COLOURS = [
            ("RED",     "#ff0000"), ("GREEN",   "#00ff00"),
            ("YELLOW",  "#ffff00"), ("BLUE",    "#0000ff"),
            ("CYAN",    "#00ffff"), ("MAGENTA", "#ff00ff"),
            ("WHITE",   "#ffffff"),
        ]
        for i, (name, hex_col) in enumerate(SWATCH_COLOURS):
            col_frame = tk.Frame(swatch_frame, bg=C["bg"])
            col_frame.grid(row=0, column=i, padx=6)
            tk.Label(col_frame, bg=hex_col, width=6, height=2,
                     relief=tk.GROOVE, bd=2).pack()
            status = tk.Label(col_frame, text=name[:3],
                              font=("Courier", 8), fg=C["dim"], bg=C["bg"])
            status.pack()
            self._calib_swatches[name] = status

        self._update_calib_status()

        # ── Pack top widgets from the TOP down ────────────────────────────────

        # Header
        hdr = tk.Frame(self._tab_calibrate, bg=C["bg"])
        hdr.pack(side=tk.TOP, fill=tk.X, padx=8, pady=4)
        tk.Label(hdr, text="COLOUR CALIBRATION",
                 font=("Courier", 14, "bold"), fg=C["yellow"], bg=C["bg"]
                 ).pack(side=tk.LEFT)

        # Brief instructions
        instr = (
            "1. Press GENERATE to print the calibration chart.\n"
            "2. Colour ONLY the boxes for pencils that scan incorrectly.\n"
            "3. Place chart under camera — preview appears below.\n"
            "4. Wait for camera to adjust, then press CAPTURE CHART.\n"
            "   Unfilled boxes keep their default hue range."
        )
        tk.Label(self._tab_calibrate, text=instr,
                 font=("Courier", 10), fg=C["text"], bg=C["bg"],
                 justify=tk.LEFT, anchor=tk.W
                 ).pack(side=tk.TOP, fill=tk.X, padx=16, pady=4)

        # Camera preview — fills ALL remaining space between top and bottom widgets
        self._calib_cam_label = tk.Label(
            self._tab_calibrate, bg="#111111",
            text="[ Switch to this tab to start camera preview ]",
            font=("Courier", 11), fg=C["green"],
        )
        self._calib_cam_label.pack(side=tk.TOP, fill=tk.BOTH,
                                    expand=True, padx=8, pady=4)

    def _update_calib_status(self):
        """Refresh the per-colour status labels (CALIBRATED / DEFAULT)."""
        from calibration import load_calibration
        calib = load_calibration(GALLERY_DIR)
        for name, label in self._calib_swatches.items():
            if calib and name in calib:
                hue = calib[name]["hue"]
                label.config(text=f"h={hue:.0f}", fg=C["green"])
            else:
                label.config(text="DEF", fg=C["dim"])

    def _generate_calib_chart(self):
        """Generate and save the calibration chart PDF/PNG."""
        try:
            out = GALLERY_DIR / "calibration"
            generate_chart(out)
            self._set_status(f"Chart saved to {out}")
            messagebox.showinfo("Chart Generated",
                                f"Calibration chart saved to:\n{out}\n\n"
                                "Print calibration_chart.pdf at 100% scale.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _capture_calib_chart(self):
        """Capture the calibration chart with the camera."""
        if not self._camera.available:
            messagebox.showerror("No Camera",
                                 "No camera available.\nUse 'Load Image File'.")
            return
        self._set_status("Capturing calibration chart...")
        self.update()
        img = self._camera.grab_capture()
        if img is None:
            messagebox.showerror("Capture Failed", "Camera returned no frame.")
            return
        self._run_calibration(img)

    def _load_calib_from_file(self):
        """Load a photo of the calibration chart from disk."""
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Select photo of calibration chart",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp *.tiff")]
        )
        if path:
            try:
                img = Image.open(path).convert("RGB")
                self._run_calibration(img)
            except Exception as e:
                messagebox.showerror("Load Error", str(e))

    def _run_calibration(self, img: Image.Image):
        """Process calibration image; update hue ranges; refresh status."""
        self._set_status("Processing calibration chart...")
        self.update()
        try:
            calib = calibrate_from_image(img, GALLERY_DIR)
            # Reload hue ranges in the digitiser
            from digitiser import load_calibration_for_config
            cfg = dict(DIGITISER)
            cfg["gallery_dir"] = GALLERY_DIR
            load_calibration_for_config(cfg)

            lines = [f"  {n}: hue={d['hue']:.1f} [{d['hue_lo']:.1f}–{d['hue_hi']:.1f}]"
                     for n, d in calib.items()]
            self._calib_result.config(text="\n".join(lines))
            self._update_calib_status()
            self._set_status(f"Calibration complete — {len(calib)} colour(s) sampled")
            messagebox.showinfo("Calibration Complete",
                                f"Successfully calibrated {len(calib)} colour(s).\n"
                                "Any unfilled boxes will use default hue ranges.\n"
                                "These settings will be used for all future captures.")
        except Exception as e:
            self._set_status(f"Calibration error: {e}")
            messagebox.showerror("Calibration Error", str(e))

    def _reset_calibration(self):
        """Delete calibration.json and revert to default hue ranges."""
        calib_file = GALLERY_DIR / "calibration.json"
        if calib_file.exists():
            if messagebox.askyesno("Reset Calibration",
                                   "Delete calibration data and use defaults?"):
                calib_file.unlink()
                from digitiser import load_calibration_for_config
                cfg = dict(DIGITISER)
                cfg["gallery_dir"] = GALLERY_DIR
                load_calibration_for_config(cfg)
                self._update_calib_status()
                self._calib_result.config(text="")
                self._set_status("Calibration reset to defaults")
        else:
            messagebox.showinfo("No Calibration", "No calibration file found.")

    # ═══════════════════════════════════════════════════════════════════════════
    # Camera Preview
    # ═══════════════════════════════════════════════════════════════════════════

    def _start_preview(self):
        """Launch the background grab thread and the Tk-side display pump."""
        if not self._camera.available:
            self._cam_label.config(
                text="[ NO CAMERA ]\n\nUse 'Load Image File'",
                image="", fg="#ff4444"
            )
            self._cam_hint.config(text="Camera not available")
            return

        self._latest_frame = None
        self._preview_after_id = None
        self._stop_preview = threading.Event()

        self._preview_thread = threading.Thread(
            target=self._preview_grab_loop, daemon=True
        )
        self._preview_thread.start()
        self._preview_display_pump()

    def _preview_grab_loop(self):
        """
        Background thread: grabs frames from the camera as fast as the
        hardware delivers them and stores only the latest PIL image in
        self._latest_frame.  Never touches Tkinter.

        No sleep() here — we let OpenCV's own frame-rate gate the loop.
        When capturing a still we simply skip grabs so the camera isn't
        busy when grab_capture() is called from the worker thread.
        """
        while not self._stop_preview.is_set():
            if not self.is_capturing:
                frame = self._camera.grab_preview()
                if frame is not None:
                    self._latest_frame = frame
            else:
                # Small yield while capturing so we don't spin the CPU
                self._stop_preview.wait(timeout=0.05)

    def _preview_display_pump(self):
        """
        Main-thread pump: picks up the latest frame and updates whichever
        label(s) need it — the Capture tab preview, and the Calibrate tab
        preview when that tab is active.
        """
        if self._stop_preview.is_set():
            return

        frame = self._latest_frame
        if frame is not None and not self.is_capturing:
            # ── Capture tab preview ──────────────────────────────────────────
            w = self._cam_label.winfo_width()
            h = self._cam_label.winfo_height()
            if w < 10:
                w, h = 760, 440
            display = frame.copy()
            display.thumbnail((w, h))
            photo = ImageTk.PhotoImage(display)
            self._cam_label.configure(image=photo, text="")
            self._cam_label.image = photo

            # ── Calibrate tab preview (when that tab is visible) ─────────────
            try:
                active = self.notebook.index(self.notebook.select())
            except Exception:
                active = -1
            if active == 3:   # Calibrate is tab index 3
                cw = self._calib_cam_label.winfo_width()
                ch = self._calib_cam_label.winfo_height()
                if cw < 10:
                    cw, ch = 760, 200
                cal_disp = frame.copy()
                cal_disp.thumbnail((cw, ch))
                cal_photo = ImageTk.PhotoImage(cal_disp)
                self._calib_cam_label.configure(image=cal_photo, text="")
                self._calib_cam_label.image = cal_photo

        self._preview_after_id = self.after(40, self._preview_display_pump)

    def _stop_preview_loop(self):
        """Signal the grab thread to exit and cancel any pending after() call."""
        self._stop_preview.set()
        after_id = getattr(self, "_preview_after_id", None)
        if after_id is not None:
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        self._preview_after_id = None

    # ═══════════════════════════════════════════════════════════════════════════
    # Capture & Digitise
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _do_capture(self):
        if self._processing:
            return
        if not self._camera.available:
            messagebox.showerror("No Camera",
                                 "No camera found.\nUse 'Load Image File'.")
            self.is_capturing = False
            return
        self.is_capturing = True
        self._start_capture_pipeline(source="camera")

    def _load_from_file(self):
        path = filedialog.askopenfilename(
            title="Select photo of design sheet",
            filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp *.tiff *.webp")]
        )
        if path:
            try:
                img = Image.open(path).convert("RGB")
                self._start_capture_pipeline(source="file", image=img)
            except Exception as e:
                messagebox.showerror("Load Error", str(e))

    def _start_capture_pipeline(self, source: str,
                                  image: Image.Image | None = None):
        """Runs the full capture→digitise pipeline in a background thread."""
        self._processing = True
        self._set_status("Capturing...")
        self._btn_capture.config(state=tk.DISABLED)
        self._hw.lights_on()
        self._lcd.show_message("capturing")
        self._hw.set_busy()
        self.update()

        def worker():
            try:
                if source == "camera":
                    self.after(0, self._set_status, "Grabbing still...")
                    img = self._camera.grab_capture()
                    if img is None:
                        raise DigitiserError("Camera returned no frame")
                else:
                    img = image

                self.after(0, self._set_status, "Detecting grid...")
                self.after(0, lambda: self._lcd.show_message("processing"))

                # Build the warped image for the scanline view and show it
                # before OCR starts, so the user sees it as soon as the grid
                # is detected.
                warped_preview = get_warped_image(img, DIGITISER)
                if warped_preview is not None:
                    self.after(0, self._scanline_show, warped_preview)

                self.after(0, self._set_status, "Running OCR...")
                self.after(0, lambda: self._lcd.show_message("ocr"))

                ROWS = DIGITISER.get("rows", 24)

                def _row_cb(row_idx, _warped_pil):
                    # Called from the worker thread after each row completes.
                    self.after(0, self._scanline_advance, row_idx, ROWS)

                tti, warped_pil, scan_data = digitise_full(
                    img, DIGITISER, row_callback=_row_cb
                )

                self.after(0, self._on_digitise_success, img, tti,
                           warped_pil, scan_data)

            except GridNotFoundError:
                self.after(0, self._scanline_hide)
                self.after(0, self._on_digitise_error,
                           "Grid not found in image.\n\n"
                           "Try:\n• Better lighting\n"
                           "• Fill more of the frame\n• Flatter page")
                self.after(0, lambda: self._lcd.show_message("no_grid"))

            except Exception as e:
                self.after(0, self._scanline_hide)
                self.after(0, self._on_digitise_error, str(e))

        threading.Thread(target=worker, daemon=True).start()

    # ── Scanline overlay ──────────────────────────────────────────────────────

    def _scanline_show(self, warped: Image.Image):
        """
        Switch to a dedicated scanline tab showing the warped grid.
        Called on the main thread just before OCR begins.
        """
        # Remove any previous scanline tab
        self._scanline_hide()

        ROWS = DIGITISER.get("rows", 24)
        COLS = DIGITISER.get("cols", 40)

        # Build a new tab and insert it as tab index 1 (after CAPTURE)
        self._tab_scan = tk.Frame(self.notebook, bg=C["bg"])
        self.notebook.insert(1, self._tab_scan, text="  SCANNING  ")
        self.notebook.select(1)

        # Scale the warped image to fit the available window space.
        # Use the notebook's current dimensions as the constraint; fall back
        # to a safe default if the widget hasn't been laid out yet.
        self.update_idletasks()
        avail_w = self.notebook.winfo_width()  - 16
        avail_h = self.notebook.winfo_height() - 40
        if avail_w < 100:
            avail_w = 900
        if avail_h < 100:
            avail_h = 580

        src_w, src_h = warped.size
        scale = min(avail_w / src_w, avail_h / src_h, 1.0)   # never upscale
        disp_w = max(1, int(src_w * scale))
        disp_h = max(1, int(src_h * scale))

        if scale < 1.0:
            display = warped.resize((disp_w, disp_h), Image.BILINEAR)
        else:
            display = warped

        # Canvas sized to the scaled image
        self._scan_canvas = tk.Canvas(
            self._tab_scan, width=disp_w, height=disp_h,
            bg="#000000", highlightthickness=0
        )
        self._scan_canvas.pack(fill=tk.BOTH, expand=True)

        # Draw the scaled image
        self._scan_photo = ImageTk.PhotoImage(display)
        self._scan_canvas.create_image(0, 0, anchor=tk.NW,
                                        image=self._scan_photo)

        # Draw faint grid lines so the scanline rows are clearly visible
        cw = disp_w / COLS;  ch = disp_h / ROWS
        for col in range(1, COLS):
            x = int(col * cw)
            self._scan_canvas.create_line(x, 0, x, disp_h,
                                           fill="#003300", width=1)
        for row in range(1, ROWS):
            y = int(row * ch)
            self._scan_canvas.create_line(0, y, disp_w, y,
                                           fill="#003300", width=1)

        # The scanline rectangle — starts above row 0
        self._scan_line_id  = None
        self._scan_done_ids = []   # completed-row fill rectangles
        self._scan_w = disp_w
        self._scan_h = disp_h
        self._scan_ch = ch

    def _scanline_advance(self, row_idx: int, total_rows: int):
        """
        Move the scanline to sit below the just-completed row.
        Called on the main thread after each row.
        """
        if not hasattr(self, "_scan_canvas") or self._scan_canvas is None:
            return

        c  = self._scan_canvas
        w  = self._scan_w
        ch = self._scan_ch

        # Delete previous scanline
        if self._scan_line_id:
            c.delete(self._scan_line_id)

        # Green tint over the completed row
        y0 = int(row_idx * ch)
        y1 = int((row_idx + 1) * ch)
        done_rect = c.create_rectangle(
            0, y0, w, y1,
            fill="#003300", outline="", stipple="gray25"
        )
        self._scan_done_ids.append(done_rect)

        # Move the bright scanline to the bottom of the completed row
        if row_idx < total_rows - 1:
            self._scan_line_id = c.create_rectangle(
                0, y1 - 2, w, y1 + 2,
                fill="#00ff00", outline=""
            )
        else:
            # Last row done — flash the whole grid green briefly
            self._scan_line_id = c.create_rectangle(
                0, 0, w, int(total_rows * ch),
                fill="#00ff00", outline="", stipple="gray25"
            )

        self._set_status(f"Scanning row {row_idx + 1}/{total_rows}...")

    def _scanline_hide(self):
        """Remove the scanline tab if it exists."""
        if hasattr(self, "_tab_scan") and self._tab_scan is not None:
            try:
                self.notebook.forget(self._tab_scan)
                self._tab_scan.destroy()
            except Exception:
                pass
            self._tab_scan      = None
            self._scan_canvas   = None
            self._scan_line_id  = None
            self._scan_done_ids = []

    def _on_digitise_success(self, img: Image.Image, tti: str,
                              warped_pil: "Image.Image | None" = None,
                              scan_data: "list | None" = None):
        self._current_image      = img
        self._current_tti        = tti
        self._current_warped_pil = warped_pil
        self._current_scan_data  = scan_data
        self._processing    = False
        self.is_capturing   = False   # resume preview

        # Restore UI first — do this before anything that could fail
        self._hw.lights_off_after(2.0)
        self._hw.set_ready()
        self._btn_capture.config(state=tk.NORMAL)

        # Brief pause so the user sees the completed scanline, then switch
        self.after(400, self._finish_digitise_success, img, tti)

    def _finish_digitise_success(self, img: Image.Image, tti: str):
        self._scanline_hide()
        self._display_tti(tti)
        self.notebook.select(1)   # switch to Preview tab (index may have shifted)

        self._set_status("Digitised! Check preview, then save.")
        self._lcd.show_message("done")

        # Auto-save scan images — deferred so UI is fully updated first,
        # and wrapped so any error here cannot affect the preview.
        self.after(100, self._auto_save_scan_deferred)

    def _auto_save_scan_deferred(self):
        """Called via after() so UI is fully updated before we do file I/O."""
        if self._current_image is not None:
            self._auto_save_scan(self._current_image)

    def _auto_save_scan(self, img: Image.Image):
        """
        Save the raw scan and warped grid to ~/digitiser_gallery/scans/.
        Every error is caught and logged — this must never affect the UI.
        """
        import datetime as _dt
        try:
            scan_dir = GALLERY_DIR / "scans"
            scan_dir.mkdir(parents=True, exist_ok=True)
            ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")

            # Raw scan
            raw_path = scan_dir / f"scan_{ts}.jpg"
            img.save(str(raw_path), "JPEG", quality=92)
            log.info("Auto-saved scan: %s", raw_path)
            self._last_scan_path = raw_path

            # Warped grid — wrap separately so a marker-not-found error
            # doesn't prevent the raw scan from being saved
            try:
                warped = get_warped_image(img, DIGITISER)
                if warped is not None:
                    warp_path = scan_dir / f"scan_{ts}_warped.jpg"
                    warped.save(str(warp_path), "JPEG", quality=92)
                    log.info("Auto-saved warped: %s", warp_path)
            except Exception as e:
                log.warning("Warped image save failed: %s", e)

            self._set_status(
                f"Digitised — scan saved as scan_{ts}.jpg"
            )
        except Exception as e:
            log.warning("Auto-save scan failed: %s", e)
            self._last_scan_path = None


    def _on_digitise_error(self, message: str):
        self._processing = False
        self.is_capturing = False   # resume preview
        self._hw.lights_off()
        self._hw.set_ready()
        self._btn_capture.config(state=tk.NORMAL)
        self._scanline_hide()
        self._lcd.show_message("error")
        self._set_status(f"Error: {message.splitlines()[0]}")
        messagebox.showerror("Digitise Error", message)

    # ═══════════════════════════════════════════════════════════════════════════
    # Preview / TTI Display
    # ═══════════════════════════════════════════════════════════════════════════

    def _display_tti(self, tti: str):
        """Render TTI onto the teletext canvas and show source."""
        self._tt_canvas.delete("placeholder")
        self._renderer.render_tti(tti)

        self._tti_text.config(state=tk.NORMAL)
        self._tti_text.delete("1.0", tk.END)
        self._tti_text.insert(tk.END, tti)
        self._tti_text.config(state=tk.DISABLED)

    def _show_warped(self):
        """Open the Cell Inspector showing the exact scan data from the last digitise."""
        if self._current_image is None:
            messagebox.showinfo("No Image", "Capture a page first.")
            return

        # Use the warped image and scan data produced during digitise_full so
        # the inspector shows exactly what went into the TTI — no re-analysis.
        warped    = self._current_warped_pil
        scan_data = self._current_scan_data

        if warped is None:
            # Fallback: re-warp (happens if image was loaded from gallery)
            insp_config = dict(DIGITISER)
            insp_config["warp_width"]  = 640
            insp_config["warp_height"] = 384
            warped = get_warped_image(self._current_image, insp_config)
            if warped is None:
                messagebox.showerror("Error", "Could not extract warped image.")
                return

        _CellInspector(self, warped, DIGITISER, scan_data=scan_data)

    def _save_scan_image(self):
        """Save the raw captured image to a user-chosen location."""
        if self._current_image is None:
            messagebox.showinfo("No Image", "Capture a page first.")
            return
        # Suggest the auto-saved path as default filename
        import datetime as _dt
        default = f"scan_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        path = filedialog.asksaveasfilename(
            title="Save Scan Image",
            defaultextension=".jpg",
            filetypes=[
                ("JPEG image", "*.jpg *.jpeg"),
                ("PNG image",  "*.png"),
                ("All files",  "*.*"),
            ],
            initialfile=default,
            initialdir=str(GALLERY_DIR / "scans"),
        )
        if not path:
            return
        try:
            fmt = "PNG" if path.lower().endswith(".png") else "JPEG"
            kw  = {"quality": 92} if fmt == "JPEG" else {}
            self._current_image.save(path, fmt, **kw)
            self._set_status(f"Scan image saved: {Path(path).name}")
            messagebox.showinfo("Saved", f"Scan image saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Save Error", str(e))

    # ═══════════════════════════════════════════════════════════════════════════
    # Save / Export
    # ═══════════════════════════════════════════════════════════════════════════

    def _do_save(self):
        if self._current_tti is None:
            messagebox.showinfo("Nothing to Save", "Capture a page first.")
            return
        try:
            entry = self._gallery.save(
                self._current_tti, self._current_image
            )
            self._refresh_gallery()
            self._set_status(f"Saved as P{entry.page_number:03d}")
            self._lcd.show_saved(entry.page_number)
            messagebox.showinfo("Saved",
                                f"Page saved as P{entry.page_number:03d}")
        except Exception as e:
            messagebox.showerror("Save Error", str(e))

    def _export_tti(self):
        if self._current_tti is None:
            messagebox.showinfo("Nothing to Export", "Capture a page first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".tti",
            filetypes=[("TTI files", "*.tti"), ("All files", "*.*")],
            initialfile="page.tti"
        )
        if path:
            try:
                Path(path).write_text(self._current_tti, encoding="latin-1")
                self._set_status(f"Exported to {path}")
            except Exception as e:
                messagebox.showerror("Export Error", str(e))

    # ═══════════════════════════════════════════════════════════════════════════
    # Gallery
    # ═══════════════════════════════════════════════════════════════════════════

    def _refresh_gallery(self):
        self._gallery.refresh()
        self._gallery_lb.delete(0, tk.END)
        for entry in self._gallery.entries():
            self._gallery_lb.insert(tk.END, f"  {entry.display_name()}")
        count = self._gallery.count()
        self._set_status(f"Gallery: {count} page{'s' if count != 1 else ''}")

    def _on_gallery_select(self, _event=None):
        idx = self._gallery_lb.curselection()
        if not idx:
            return
        entry = self._gallery.get_entry(idx[0])
        if entry and entry.thumb_path.exists():
            try:
                img = Image.open(entry.thumb_path)
                img.thumbnail((320, 150))
                photo = ImageTk.PhotoImage(img)
                self._thumb_label.config(image=photo, text="")
                self._thumb_label.image = photo
            except Exception:
                pass

    def _load_gallery_entry(self):
        idx = self._gallery_lb.curselection()
        if not idx:
            messagebox.showinfo("Select a Page", "Click a page to select it first.")
            return
        entry = self._gallery.get_entry(idx[0])
        if entry is None:
            return
        tti = entry.load_tti()
        self._current_tti = tti
        self._display_tti(tti)
        self.notebook.select(1)
        self._set_status(f"Loaded P{entry.page_number:03d} from gallery")
        self._lcd.show_gallery_page(idx[0], self._gallery.count(),
                                     entry.display_name())

    def _export_gallery_tti(self):
        idx = self._gallery_lb.curselection()
        if not idx:
            return
        entry = self._gallery.get_entry(idx[0])
        if not entry:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".tti",
            filetypes=[("TTI files", "*.tti")],
            initialfile=f"P{entry.page_number:03d}.tti"
        )
        if path:
            try:
                import shutil
                shutil.copy(entry.tti_path, path)
                self._set_status(f"Exported to {path}")
            except Exception as e:
                messagebox.showerror("Export Error", str(e))

    def _delete_gallery_entry(self):
        idx = self._gallery_lb.curselection()
        if not idx:
            return
        entry = self._gallery.get_entry(idx[0])
        if not entry:
            return
        if messagebox.askyesno("Delete",
                               f"Delete P{entry.page_number:03d}?\nThis cannot be undone."):
            self._gallery.delete(entry)
            self._refresh_gallery()
            self._thumb_label.config(image="", text="")

    # ═══════════════════════════════════════════════════════════════════════════
    # Hardware button bindings
    # ═══════════════════════════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════════════════════════
    # Calibrate tab — camera warmup on tab selection
    # ═══════════════════════════════════════════════════════════════════════════

    def _on_tab_changed(self, _event=None):
        """
        Called whenever the user switches notebook tabs.
        When the Calibrate tab is selected, run a short AWB warmup sequence
        so the camera's auto white-balance has time to settle before capture.
        """
        try:
            active = self.notebook.index(self.notebook.select())
        except Exception:
            return

        if active == 3:   # Calibrate tab
            self._start_calib_warmup()

    def _start_calib_warmup(self):
        """
        Disable CAPTURE CHART and show a countdown while the camera's auto
        white-balance settles.  We wait for AWB_FRAMES × 40ms = ~200ms of
        live preview frames, which is enough for AWB to stabilise on most
        USB webcams.
        """
        AWB_FRAMES = 10   # ~400ms at 25fps — enough for AWB to settle

        # Cancel any existing warmup
        if self._calib_warmup_id is not None:
            try:
                self.after_cancel(self._calib_warmup_id)
            except Exception:
                pass
            self._calib_warmup_id = None

        if not self._camera.available:
            return

        self._calib_warmup_ctr = AWB_FRAMES
        self._btn_calib_capture.config(state=tk.DISABLED)
        self._calib_result.config(
            text="Camera adjusting — please wait...", fg=C["yellow"]
        )
        self._calib_warmup_tick()

    def _calib_warmup_tick(self):
        """Count down AWB frames, re-enabling the capture button when done."""
        if self._calib_warmup_ctr <= 0:
            self._btn_calib_capture.config(state=tk.NORMAL)
            self._calib_result.config(
                text="Camera ready — point at chart and press CAPTURE CHART",
                fg=C["green"]
            )
            self._calib_warmup_id = None
            return

        self._calib_warmup_ctr -= 1
        secs = self._calib_warmup_ctr // 25 + 1
        self._calib_result.config(
            text=f"Camera adjusting... ({self._calib_warmup_ctr} frames)",
            fg=C["yellow"]
        )
        self._calib_warmup_id = self.after(40, self._calib_warmup_tick)

    # ═══════════════════════════════════════════════════════════════════════════
    # Hardware button bindings
    # ═══════════════════════════════════════════════════════════════════════════

    def _bind_hardware(self):
        self._hw.on_button("BTN_CAPTURE", self._do_capture)
        self._hw.on_button("BTN_SAVE",    self._do_save)
        self._hw.on_button("BTN_CANCEL",  self._hw_cancel)
        self._hw.on_button("BTN_NEXT",    self._hw_next)
        self._hw.on_button("BTN_PREV",    self._hw_prev)

        # Also bind keyboard shortcuts for development convenience
        self.bind("<space>",    lambda _: self._do_capture())
        self.bind("<Return>",   lambda _: self._do_save())
        self.bind("<Escape>",   lambda _: self._hw_cancel())
        self.bind("<Right>",    lambda _: self._hw_next())
        self.bind("<Left>",     lambda _: self._hw_prev())

    def _hw_cancel(self):
        """Cancel current action or go back to capture tab."""
        if self._processing:
            self._set_status("Cancel requested (wait for current op to finish)")
            return
        self.notebook.select(0)
        self._lcd.show_message("cancelled")
        self._set_status("Cancelled — ready for next capture")

    def _hw_next(self):
        """Advance gallery selection or switch to next tab."""
        current_tab = self.notebook.index(self.notebook.select())
        if current_tab == 2:   # Gallery tab
            count = self._gallery_lb.size()
            if count == 0:
                return
            idx = self._gallery_lb.curselection()
            new_idx = (idx[0] + 1) % count if idx else 0
            self._gallery_lb.selection_clear(0, tk.END)
            self._gallery_lb.selection_set(new_idx)
            self._gallery_lb.see(new_idx)
            self._on_gallery_select()
        else:
            self.notebook.select(min(current_tab + 1, 2))

    def _hw_prev(self):
        """Retreat gallery selection or switch to previous tab."""
        current_tab = self.notebook.index(self.notebook.select())
        if current_tab == 2:
            count = self._gallery_lb.size()
            if count == 0:
                return
            idx = self._gallery_lb.curselection()
            new_idx = (idx[0] - 1) % count if idx else count - 1
            self._gallery_lb.selection_clear(0, tk.END)
            self._gallery_lb.selection_set(new_idx)
            self._gallery_lb.see(new_idx)
            self._on_gallery_select()
        else:
            self.notebook.select(max(current_tab - 1, 0))

    # ═══════════════════════════════════════════════════════════════════════════
    # Lights toggle
    # ═══════════════════════════════════════════════════════════════════════════

    def _toggle_lights(self):
        # Simple toggle — track state via attribute
        if getattr(self, "_lights_on", False):
            self._hw.lights_off()
            self._lights_on = False
            self._set_status("Lights off")
        else:
            self._hw.lights_on()
            self._lights_on = True
            self._set_status("Lights on")

    def _generate_template(self):
        """Generate and save the capture template PDF/PNG."""
        from tkinter import filedialog
        from template import generate_template
        out_dir = filedialog.askdirectory(
            title="Choose folder to save template"
        )
        if not out_dir:
            return
        try:
            from pathlib import Path
            self._set_status("Generating template...")
            self.update()
            generate_template(Path(out_dir), show=False)
            self._set_status(f"Template saved to {out_dir}")
            messagebox.showinfo(
                "Template Generated",
                f"Template saved to:\n{out_dir}\n\n"
                "Print template.pdf at 100% scale on A4 paper.\n"
                "No configuration changes needed after printing."
            )
        except Exception as e:
            messagebox.showerror("Template Error", str(e))
            self._set_status("Template generation failed")

    # ═══════════════════════════════════════════════════════════════════════════
    # Utilities
    # ═══════════════════════════════════════════════════════════════════════════

    def _make_btn(self, parent, text, cmd, fg=C["text"], bg=C["panel"]):
        return tk.Button(
            parent, text=text, command=cmd,
            font=("Courier", 11, "bold"),
            fg=fg, bg=bg,
            activebackground=fg, activeforeground=bg,
            relief=tk.FLAT, padx=10, pady=5, cursor="hand2"
        )

    def _set_status(self, msg: str):
        self._status_var.set(msg)

    def _on_close(self):
        """Clean shutdown: cancel preview, release hardware, destroy window."""
        if self._processing:
            # Can't use messagebox here safely if the event loop is stressed;
            # just ask via a simple Tk dialog built without blocking.
            if not messagebox.askyesno(
                "Busy", "A capture is in progress.\nExit anyway?"
            ):
                return

        # Stop the preview before destroying widgets
        if hasattr(self, "_stop_preview"):
            self._stop_preview_loop()

        self.is_capturing = False

        try:
            self._hw.cleanup()
        except Exception:
            pass
        try:
            self._camera.release()
        except Exception:
            pass

        self.destroy()


# ═══════════════════════════════════════════════════════════════════════════════
# Cell Inspector — standalone Toplevel for interactive warped-grid analysis
# ═══════════════════════════════════════════════════════════════════════════════

class _CellInspector(tk.Toplevel):
    """
    Interactive cell inspector window.

    When ``scan_data`` is supplied (from ``digitise_full``), every field shown
    is read directly from the scan — identical to what was written into the
    TTI.  No re-analysis is performed on click.

    When ``scan_data`` is None (gallery load / fallback), the inspector falls
    back to re-running the classifiers on the warped image so it still works,
    but the results may differ slightly from the original scan.

    Left panel  — warped grid image with grid overlay and a movable highlight.
    Right panel — magnified cell view, colour swatch, and classification detail.

    Parameters
    ----------
    parent : tk.Tk
        Parent window.
    warped : PIL.Image.Image
        Perspective-corrected grid image.
    config : dict
        DIGITISER config dict.
    scan_data : list | None
        24×40 grid of per-cell dicts from ``digitise_full``.  None → fallback.
    """

    _COLOUR_HEX = {
        "RED":     "#ff0000",
        "GREEN":   "#00ff00",
        "YELLOW":  "#ffff00",
        "BLUE":    "#0000ff",
        "MAGENTA": "#ff00ff",
        "CYAN":    "#00ffff",
        "WHITE":   "#ffffff",
        "BLACK":   "#333333",
        "NONE":    "#222222",
    }
    _CELL_TYPE_LABEL = {0: "EMPTY", 1: "TEXT", 2: "GRAPHICS", 3: "WHITE GFX"}

    COLS = 40
    ROWS = 24
    MAG  = 8

    def __init__(self, parent, warped: "Image.Image", config: dict,
                 scan_data: "list | None" = None):
        super().__init__(parent)
        self.title("Cell Inspector — click a cell to analyse")
        self.configure(bg=C["bg"])
        self.resizable(True, True)

        # Keep the original warped image for pixel-accurate cell crops in the
        # magnified view.  Scale a display copy to fit comfortably on screen
        # alongside the 280px detail panel, leaving room for the header/status.
        self._warped_orig = warped          # full-res, used for cell crops
        MAX_GRID_W = 900
        MAX_GRID_H = 560
        orig_w, orig_h = warped.size
        scale = min(MAX_GRID_W / orig_w, MAX_GRID_H / orig_h, 1.0)
        if scale < 1.0:
            disp_w = max(1, int(orig_w * scale))
            disp_h = max(1, int(orig_h * scale))
            self._warped = warped.resize((disp_w, disp_h), Image.BILINEAR)
        else:
            self._warped = warped

        self.DISP_W, self.DISP_H = self._warped.size
        self._config    = config
        self._scan_data = scan_data          # None → fallback mode
        self._sel_row   = 0
        self._sel_col   = 0
        self._highlight = None

        self._build_ui()
        self._inspect_cell(0, 0)

    # ── UI Construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        hdr = tk.Frame(self, bg=C["panel"], pady=4)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="■ CELL INSPECTOR",
                 font=("Courier", 13, "bold"), fg=C["text"], bg=C["panel"]
                 ).pack(side=tk.LEFT, padx=10)
        source_note = "(scan data)" if self._scan_data is not None else "(re-analysed)"
        tk.Label(hdr, text=f"Click any cell • Arrow keys to step  {source_note}",
                 font=("Courier", 10), fg=C["yellow"], bg=C["panel"]
                 ).pack(side=tk.LEFT, padx=10)
        tk.Button(hdr, text="CLOSE", command=self.destroy,
                  font=("Courier", 10, "bold"),
                  fg=C["text"], bg="#660000",
                  activebackground=C["text"], activeforeground="#660000",
                  relief=tk.FLAT, padx=8, cursor="hand2"
                  ).pack(side=tk.RIGHT, padx=10)

        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self._canvas = tk.Canvas(
            body, width=self.DISP_W, height=self.DISP_H,
            bg="#111111", cursor="crosshair",
            highlightthickness=2, highlightbackground=C["panel"]
        )
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._photo = ImageTk.PhotoImage(self._warped)
        self._canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)
        self._draw_grid_overlay()

        self._canvas.bind("<Button-1>", self._on_click)
        self.bind("<Left>",  lambda _: self._step(0, -1))
        self.bind("<Right>", lambda _: self._step(0,  1))
        self.bind("<Up>",    lambda _: self._step(-1, 0))
        self.bind("<Down>",  lambda _: self._step( 1, 0))
        self.focus_set()

        detail = tk.Frame(body, bg=C["bg"], width=280)
        detail.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        detail.pack_propagate(False)

        self._addr_var = tk.StringVar(value="R00  C00")
        tk.Label(detail, textvariable=self._addr_var,
                 font=("Courier", 14, "bold"), fg=C["yellow"], bg=C["bg"]
                 ).pack(pady=(4, 0))

        mag_frame = tk.LabelFrame(detail, text=" CELL ",
                                   font=("Courier", 9, "bold"),
                                   fg=C["green"], bg=C["bg"], labelanchor="nw")
        mag_frame.pack(fill=tk.X, padx=6, pady=2)
        self._mag_label = tk.Label(mag_frame, bg="#000000", relief=tk.SUNKEN)
        self._mag_label.pack(padx=4, pady=2)

        swatch_frame = tk.LabelFrame(detail, text=" COLOUR ",
                                      font=("Courier", 9, "bold"),
                                      fg=C["green"], bg=C["bg"], labelanchor="nw")
        swatch_frame.pack(fill=tk.X, padx=6, pady=4)
        self._swatch = tk.Label(swatch_frame, width=20, height=3,
                                 bg="#222222", relief=tk.SUNKEN)
        self._swatch.pack(padx=4, pady=4)
        self._colour_var = tk.StringVar(value="—")
        tk.Label(swatch_frame, textvariable=self._colour_var,
                 font=("Courier", 11, "bold"), fg=C["text"], bg=C["bg"]
                 ).pack(pady=(0, 4))

        info_frame = tk.LabelFrame(detail, text=" ANALYSIS ",
                                    font=("Courier", 9, "bold"),
                                    fg=C["green"], bg=C["bg"], labelanchor="nw")
        info_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        self._info_text = tk.Text(
            info_frame, height=14, width=28,
            bg=C["entry_bg"], fg=C["green"],
            font=("Courier", 10), relief=tk.FLAT,
            state=tk.DISABLED, wrap=tk.WORD
        )
        self._info_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        sixel_frame = tk.LabelFrame(detail, text=" SIXEL BITS ",
                                     font=("Courier", 9, "bold"),
                                     fg=C["green"], bg=C["bg"], labelanchor="nw")
        sixel_frame.pack(fill=tk.X, padx=6, pady=4)
        self._sixel_canvas = tk.Canvas(sixel_frame, width=120, height=90,
                                        bg="#000000", highlightthickness=0)
        self._sixel_canvas.pack(padx=4, pady=4)

        self._status_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._status_var,
                 font=("Courier", 9), fg=C["dim"], bg=C["bg"],
                 anchor=tk.W, padx=6
                 ).pack(fill=tk.X, side=tk.BOTTOM, pady=2)

    # ── Grid overlay ──────────────────────────────────────────────────────────

    def _draw_grid_overlay(self):
        cw = self.DISP_W / self.COLS
        ch = self.DISP_H / self.ROWS
        line_col = "#003300"
        accent   = "#005500"
        for col in range(1, self.COLS):
            x = int(col * cw)
            colour = accent if col % 5 == 0 else line_col
            self._canvas.create_line(x, 0, x, self.DISP_H, fill=colour, width=1)
        for row in range(1, self.ROWS):
            y = int(row * ch)
            colour = accent if row % 5 == 0 else line_col
            self._canvas.create_line(0, y, self.DISP_W, y, fill=colour, width=1)

    def _draw_highlight(self, row: int, col: int):
        cw = self.DISP_W / self.COLS
        ch = self.DISP_H / self.ROWS
        x0 = int(col * cw);  y0 = int(row * ch)
        x1 = int((col + 1) * cw);  y1 = int((row + 1) * ch)
        if self._highlight:
            self._canvas.delete(self._highlight)
        self._highlight = self._canvas.create_rectangle(
            x0, y0, x1, y1, outline="#ffff00", width=2
        )

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_click(self, event):
        cw = self.DISP_W / self.COLS
        ch = self.DISP_H / self.ROWS
        col = max(0, min(self.COLS - 1, int(self._canvas.canvasx(event.x) / cw)))
        row = max(0, min(self.ROWS - 1, int(self._canvas.canvasy(event.y) / ch)))
        self._inspect_cell(row, col)

    def _step(self, dr: int, dc: int):
        new_row = max(0, min(self.ROWS - 1, self._sel_row + dr))
        new_col = max(0, min(self.COLS - 1, self._sel_col + dc))
        if new_row != self._sel_row or new_col != self._sel_col:
            self._inspect_cell(new_row, new_col)

    # ── Inspection ────────────────────────────────────────────────────────────

    def _inspect_cell(self, row: int, col: int):
        self._sel_row = row
        self._sel_col = col
        self._draw_highlight(row, col)
        self._addr_var.set(f"R{row:02d}  C{col:02d}")

        if self._scan_data is not None:
            # Fast path: read the exact data that went into the TTI
            self._display_from_scan(row, col)
        else:
            # Fallback: re-analyse on a background thread
            self._status_var.set(f"Analysing R{row:02d} C{col:02d}...")
            self.update_idletasks()
            threading.Thread(
                target=self._analyse_worker, args=(row, col), daemon=True
            ).start()

    def _display_from_scan(self, row: int, col: int):
        """
        Populate the inspector panels directly from the scan_data dict —
        no re-analysis, guaranteed to match the TTI output.
        """
        import numpy as np

        d  = self._scan_data[row][col]
        ct = d["cell_type"]

        # Crop from the original full-res warped image for a sharp magnified view
        warped_np = np.array(self._warped_orig)
        H, W = warped_np.shape[:2]
        cw = W / self.COLS;  ch = H / self.ROWS
        x0 = int(col * cw);  x1 = int((col + 1) * cw)
        y0 = int(row * ch);  y1 = int((row + 1) * ch)
        cell_rgb = warped_np[y0:y1, x0:x1]
        cell_pil = Image.fromarray(cell_rgb)
        # Cap display height to 80px so the cell preview never crowds out the
        # analysis fields below it.  Width scales proportionally.
        MAX_CELL_H = 80
        mag = max(1, MAX_CELL_H // max(1, cell_pil.height))
        cell_mag = cell_pil.resize(
            (max(1, cell_pil.width * mag),
             max(1, cell_pil.height * mag)),
            Image.NEAREST
        )
        photo = ImageTk.PhotoImage(cell_mag)
        self._mag_label.configure(image=photo)
        self._mag_label.image = photo

        # Colour swatch
        colour  = d["colour"]
        hex_col = self._COLOUR_HEX.get(colour, "#222222")
        self._swatch.configure(bg=hex_col)
        self._colour_var.set(colour)

        # Determine what the TTI char is for display
        from digitiser import CELL_EMPTY, CELL_TEXT, CELL_GRAPHICS, CELL_WHITE_GFX
        ocr_char   = d.get("ocr_char", " ")
        sixel_code = d["sixel_code"]
        sixel_hex  = f"0x{sixel_code:02X}"
        sixel_chr  = chr(sixel_code) if 0x20 <= sixel_code <= 0x7E else "?"
        bits       = d["bits"]
        row_bg     = d.get("row_bg")

        # A cell is "bg-inverted" when its colour matches the row background
        # colour set by the preamble — its sixel bits are inverted in the TTI.
        is_bg_cell = (row_bg is not None and colour == row_bg
                      and ct in (CELL_GRAPHICS, CELL_WHITE_GFX))

        if ct == CELL_EMPTY:
            tti_char = "' '  (empty)"
        elif ct == CELL_TEXT:
            tti_char = f"'{ocr_char}'"
        elif ct == CELL_WHITE_GFX:
            tti_char = f"WHITE {sixel_hex} '{sixel_chr}'"
        else:
            inv_note = "  (inv)" if is_bg_cell else ""
            tti_char = f"{sixel_hex} '{sixel_chr}'{inv_note}"

        ct_label = self._CELL_TYPE_LABEL.get(ct, "?")
        sixel_colours   = d.get("sixel_colours",   ["NONE"] * 6)
        sixel_bg_colour = d.get("sixel_bg_colour", None)
        row_bg_str      = row_bg_name if (row_bg_name := {
            0x11:"RED",0x12:"GREEN",0x13:"YELLOW",0x14:"BLUE",
            0x15:"MAGENTA",0x16:"CYAN",0x17:"WHITE",
        }.get(row_bg)) else "—"
        lines = [
            f"TYPE   {ct_label}",
            f"MODE   {d['mode']}",
            f"TTI    {tti_char}",
            f"OCR    '{ocr_char}'",
            "",
            f"HUE    {d['mean_hue']:.1f}°",
            f"SAT    {d['mean_sat']:.1f}",
            f"VAL    {d['mean_val']:.1f}",
            "",
            f"LUM    mean={d['mean_lum']:.0f}  spread={d['lum_spread']:.0f}",
            f"       min={d['min_lum']:.0f}  max={d['max_lum']:.0f}",
            f"INK    {d['ink_pixels']} px  ({d['ink_pct']:.1f}%)",
            "",
            f"BITS   {bits:06b}  ({bits})",
            f"SIXEL  {sixel_hex}  '{sixel_chr}'",
            f"FG     {d['colour']}",
            f"BG     {sixel_bg_colour if sixel_bg_colour is not None else '—'}",
            f"ROW_BG {row_bg_str}",
        ]
        self._info_text.config(state=tk.NORMAL)
        self._info_text.delete("1.0", tk.END)
        self._info_text.insert(tk.END, "\n".join(lines))
        self._info_text.config(state=tk.DISABLED)

        self._draw_sixel_bitmask(d["subcell_fill"], bits, sixel_colours)

        self._status_var.set(
            f"R{row:02d} C{col:02d}  {ct_label}  {colour}  "
            f"ocr='{ocr_char}'  "
            f"spread={d['lum_spread']:.0f}  lum={d['mean_lum']:.0f}  ink={d['ink_pct']:.1f}%"
        )

    # ── Fallback analysis (no scan_data) ──────────────────────────────────────

    def _analyse_worker(self, row: int, col: int):
        """Re-run classifiers when no scan_data is available (fallback only)."""
        import numpy as np
        import cv2

        try:
            warped_np = np.array(self._warped_orig)
            H, W = warped_np.shape[:2]
            cw = W / self.COLS
            ch = H / self.ROWS
            x0 = int(col * cw);  x1 = int((col + 1) * cw)
            y0 = int(row * ch);  y1 = int((row + 1) * ch)

            cell_rgb  = warped_np[y0:y1, x0:x1]
            cell_grey = cv2.cvtColor(cell_rgb, cv2.COLOR_RGB2GRAY)

            mean_lum = float(cell_grey.mean())
            min_lum  = float(cell_grey.min())
            max_lum  = float(cell_grey.max())

            from digitiser import _classify_colour, CELL_EMPTY, CELL_TEXT, CELL_GRAPHICS
            text_spread_thresh = self._config.get("text_spread_threshold", 40)

            colour = _classify_colour(cell_rgb, self._config)
            if colour != "NONE":
                cell_type = CELL_GRAPHICS
            else:
                lum_spread = max_lum - min_lum
                cell_type  = CELL_TEXT if lum_spread >= text_spread_thresh else CELL_EMPTY

            tp  = self._config.get("template_params") or {}
            sc  = tp.get("sixel_cols", 2)
            sr  = tp.get("sixel_rows", 3)
            sixel_thresh = self._config.get("sixel_fill_threshold", 200)

            hsv    = cv2.cvtColor(cell_rgb, cv2.COLOR_RGB2HSV)
            h_ch   = hsv[:, :, 0].astype(float)
            s_ch   = hsv[:, :, 1].astype(float)
            v_ch   = hsv[:, :, 2].astype(float)
            # Saturation-only gate — matches _classify_colour
            min_sat    = self._config.get("min_saturation", 30)
            ink_mask   = s_ch > min_sat
            ink_pixels = int(ink_mask.sum())
            ink_pct    = ink_pixels / max(1, cell_rgb.shape[0] * cell_rgb.shape[1]) * 100
            mean_hue   = float(h_ch[ink_mask].mean()) if ink_pixels else 0.0
            mean_sat   = float(s_ch[ink_mask].mean()) if ink_pixels else 0.0
            mean_val   = float(v_ch.mean())
            lum_spread = max_lum - min_lum

            # Adaptive threshold matching _decode_sixels
            cell_mean_g = float(cell_grey.mean()) if cell_grey.size else sixel_thresh
            thresh_sub  = (min((cell_mean_g + 255.0) / 2.0, 243.0)
                           if cell_mean_g > sixel_thresh else sixel_thresh)

            # Subcell fill — mirrors _decode_sixels: whole-patch check then core gate
            subcell_fill  = []
            SUBCELL_FILL_FRAC = 0.10
            CORE_FILL_FRAC    = 0.10
            CORE_INSET        = 0.25
            sw_s = (x1 - x0) / sc;  sh_s = (y1 - y0) / sr
            bits = 0
            for bit_idx, (sy, sx) in enumerate(
                [(r, c) for r in range(sr) for c in range(sc)]
            ):
                px0 = int(x0 + sx * sw_s);  px1 = int(x0 + (sx + 1) * sw_s)
                py0 = int(y0 + sy * sh_s);  py1 = int(y0 + (sy + 1) * sh_s)
                patch = cell_grey[py0-y0:py1-y0, px0-x0:px1-x0]
                if patch.size == 0:
                    subcell_fill.append(0.0)
                    continue
                frac = float((patch < thresh_sub).sum()) / patch.size
                subcell_fill.append(frac)
                if frac >= SUBCELL_FILL_FRAC:
                    ph, pw = patch.shape
                    cy0 = int(ph * CORE_INSET);  cy1 = max(cy0 + 1, int(ph * (1 - CORE_INSET)))
                    cx0 = int(pw * CORE_INSET);  cx1 = max(cx0 + 1, int(pw * (1 - CORE_INSET)))
                    core = patch[cy0:cy1, cx0:cx1]
                    if core.size > 0 and float((core < thresh_sub).sum()) / core.size >= CORE_FILL_FRAC:
                        bits |= (1 << bit_idx)
            sixel_code = (0x60 + (bits & 0x1F)) if (bits & 0x20) else (0x20 + (bits & 0x1F))

            # Per-sub-cell colour analysis (fallback path)
            sixel_colours   = ["NONE"] * (sc * sr)
            sixel_bg_colour = None
            if cell_type == CELL_GRAPHICS:
                try:
                    from digitiser import _classify_sixel_colours
                    sixel_colours, sixel_bg_colour = _classify_sixel_colours(
                        cell_rgb, cell_grey,
                        bits, colour,
                        sc, sr,
                        y0, y1, x0, x1,
                        sixel_thresh,
                        self._config,
                    )
                except Exception:
                    pass

            ocr_char = " "
            if cell_type == CELL_TEXT:
                try:
                    from PIL import Image as _PIL
                    import pytesseract as _tess
                    m = max(2, int((x1 - x0) * 0.08))
                    crop_pil = _PIL.fromarray(cell_rgb).crop((m, m, (x1-x0)-m, (y1-y0)-m))
                    pw, ph = crop_pil.size
                    if pw > 2 and ph > 2:
                        scale   = max(4, int(120 / max(pw, 1)))
                        crop_up = crop_pil.resize((pw*scale, ph*scale), _PIL.LANCZOS)
                        gn      = cv2.cvtColor(np.array(crop_up), cv2.COLOR_RGB2GRAY)
                        gn      = cv2.GaussianBlur(gn, (3, 3), 0)
                        _, t    = cv2.threshold(gn, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                        cfg     = "--psm 10 --oem 1 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
                        txt     = _tess.image_to_string(_PIL.fromarray(t), config=cfg).strip().upper()
                        ocr_char = txt[0] if txt and 0x20 <= ord(txt[0]) <= 0x7E else "?"
                except Exception:
                    ocr_char = "?"

            cell_pil = Image.fromarray(cell_rgb)
            MAX_CELL_H = 80
            mag = max(1, MAX_CELL_H // max(1, cell_pil.height))
            cell_mag = cell_pil.resize(
                (cell_pil.width * mag, cell_pil.height * mag),
                Image.NEAREST
            )

            result = {
                "row": row, "col": col,
                "colour": colour, "cell_type": cell_type,
                "mode": "GRAPHICS" if cell_type == CELL_GRAPHICS else "ALPHA",
                "mean_lum": mean_lum, "min_lum": min_lum, "max_lum": max_lum,
                "lum_spread": lum_spread,
                "ink_pixels": ink_pixels, "ink_pct": ink_pct,
                "mean_hue": mean_hue, "mean_sat": mean_sat, "mean_val": mean_val,
                "subcell_fill": subcell_fill, "bits": bits,
                "sixel_code": sixel_code,
                "sixel_colours":   sixel_colours,
                "sixel_bg_colour": sixel_bg_colour,
                "ocr_char": ocr_char,
                "cell_mag": cell_mag,
            }
            self.after(0, self._update_display, result)

        except Exception as e:
            self.after(0, self._status_var.set, f"Error: {e}")

    def _update_display(self, r: dict):
        """Called on the main thread with fallback analysis results."""
        if r["row"] != self._sel_row or r["col"] != self._sel_col:
            return   # stale result from a previous click

        from digitiser import CELL_EMPTY, CELL_TEXT, CELL_GRAPHICS, CELL_WHITE_GFX

        # ── Magnified view ────────────────────────────────────────────────────
        photo = ImageTk.PhotoImage(r["cell_mag"])
        self._mag_label.configure(image=photo)
        self._mag_label.image = photo

        # ── Colour swatch ─────────────────────────────────────────────────────
        colour  = r["colour"]
        hex_col = self._COLOUR_HEX.get(colour, "#222222")
        self._swatch.configure(bg=hex_col)
        self._colour_var.set(colour)

        # ── Info text ─────────────────────────────────────────────────────────
        ct_label   = self._CELL_TYPE_LABEL.get(r["cell_type"], "?")
        bits       = r["bits"]
        sixel_code = r["sixel_code"]
        sixel_hex  = f"0x{sixel_code:02X}"
        sixel_chr  = chr(sixel_code) if 0x20 <= sixel_code <= 0x7E else "?"
        ocr_char   = r.get("ocr_char", " ")
        mode       = r.get("mode", "ALPHA")

        if r["cell_type"] == CELL_EMPTY:
            tti_char = "' '  (empty)"
        elif r["cell_type"] == CELL_TEXT:
            tti_char = f"'{ocr_char}'"
        elif r["cell_type"] == CELL_WHITE_GFX:
            tti_char = f"WHITE {sixel_hex} '{sixel_chr}'"
        else:
            tti_char = f"{sixel_hex} '{sixel_chr}'"

        lines = [
            f"TYPE   {ct_label}",
            f"MODE   {mode}",
            f"TTI    {tti_char}",
            f"OCR    '{ocr_char}'",
            "",
            f"HUE    {r['mean_hue']:.1f}°",
            f"SAT    {r['mean_sat']:.1f}",
            f"VAL    {r['mean_val']:.1f}",
            "",
            f"LUM    mean={r['mean_lum']:.0f}  spread={r.get('lum_spread', 0):.0f}",
            f"       min={r['min_lum']:.0f}  max={r['max_lum']:.0f}",
            f"INK    {r['ink_pixels']} px  ({r['ink_pct']:.1f}%)",
            "",
            f"BITS   {bits:06b}  ({bits})",
            f"SIXEL  {sixel_hex}  '{sixel_chr}'",
            f"FG     {r['colour']}",
            f"BG     {r['sixel_bg_colour'] if r.get('sixel_bg_colour') is not None else '—'}",
        ]
        self._info_text.config(state=tk.NORMAL)
        self._info_text.delete("1.0", tk.END)
        self._info_text.insert(tk.END, "\n".join(lines))
        self._info_text.config(state=tk.DISABLED)

        self._draw_sixel_bitmask(
            r["subcell_fill"], bits,
            r.get("sixel_colours", ["NONE"] * 6),
        )

        self._status_var.set(
            f"R{r['row']:02d} C{r['col']:02d}  {ct_label}  {colour}  "
            f"ocr='{ocr_char}'  "
            f"spread={r.get('lum_spread', 0):.0f}  lum={r['mean_lum']:.0f}  ink={r['ink_pct']:.1f}%"
        )

    def _draw_sixel_bitmask(self, subcell_fill: list, bits: int,
                             sixel_colours: "list[str] | None" = None):
        """
        Draw a 2×3 sixel bitmask diagram.

        When ``sixel_colours`` is supplied each sub-cell is filled with its
        detected teletext colour (set sub-cells) or a dimmed version of the
        background colour (unset sub-cells).  When absent the old behaviour
        is used: set=green, unset=dim-green gradient.
        """
        c = self._sixel_canvas
        c.delete("all")
        cw = 120;  ch = 90
        bw = cw // 2;  bh = ch // 3
        gap = 2

        for bit_idx, (row, col) in enumerate(
            [(r, cc) for r in range(3) for cc in range(2)]
        ):
            x0 = col * bw + gap
            y0 = row * bh + gap
            x1 = x0 + bw - gap * 2
            y1 = y0 + bh - gap * 2

            frac = subcell_fill[bit_idx] if bit_idx < len(subcell_fill) else 0.0
            on   = bool(bits & (1 << bit_idx))

            if sixel_colours is not None and bit_idx < len(sixel_colours):
                sc_name = sixel_colours[bit_idx]
                base_hex = self._COLOUR_HEX.get(sc_name, "#222222")
                if on:
                    fill = base_hex
                else:
                    # Dim the colour for background/unset sub-cells
                    try:
                        r_val = int(base_hex[1:3], 16) // 4
                        g_val = int(base_hex[3:5], 16) // 4
                        b_val = int(base_hex[5:7], 16) // 4
                        fill  = f"#{r_val:02x}{g_val:02x}{b_val:02x}"
                    except Exception:
                        fill = "#222222"
            else:
                # Legacy: no colour data — green for set, dim gradient for unset
                if on:
                    fill = "#00ff00"
                else:
                    g_val = int(frac * 120)
                    fill  = f"#{g_val:02x}{g_val:02x}00"

            c.create_rectangle(x0, y0, x1, y1, fill=fill, outline="#444444")

            # Label: colour name abbreviation (top) + fill % (bottom)
            if sixel_colours is not None and bit_idx < len(sixel_colours):
                sc_name = sixel_colours[bit_idx]
                label = sc_name[:3] if sc_name != "NONE" else "—"
            else:
                label = f"{frac*100:.0f}%"

            text_col = "#ffffff" if on else "#666666"
            c.create_text(
                (x0 + x1) // 2, (y0 + y1) // 2,
                text=label,
                font=("Courier", 7),
                fill=text_col,
            )

        # Bit index labels at edges
        for i, label in enumerate(["0", "2", "4"]):
            c.create_text(3, i * bh + bh // 2,
                          text=label, font=("Courier", 7), fill="#444444", anchor=tk.W)
        for i, label in enumerate(["1", "3", "5"]):
            c.create_text(cw - 3, i * bh + bh // 2,
                          text=label, font=("Courier", 7), fill="#444444", anchor=tk.E)
