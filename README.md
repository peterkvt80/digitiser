# TDI620 Digitiser

A Raspberry Pi application that photographs a filled-in **Teletext Design Sheet**
(40×24 grid, Level 1 spec), converts it to a TTI file using offline computer
vision, and lets you preview and save it to a gallery.

No internet connection required. No AI API calls. All processing is local.

---

## How it works

```
Photo → ArUco corner detection → Perspective warp → Cell classify → OCR/Sixel → TTI
        (4 markers, direct grid   (sub-pixel        (EMPTY skip,    (Tesseract     (ESC-
         corner measurement)       homography)        hue matching)   for text)      encoded)
```

Four small ArUco markers printed at the grid corners give the digitiser precise,
scale-invariant corner coordinates regardless of camera angle or distance.
Colour pencil strokes are classified using a hue-histogram approach tuned for
the low-saturation pigments typical of coloured pencils; run the calibration
chart to tune classification to your specific pencils.

---

## Hardware

### Required
- Raspberry Pi 4 or 5 (or Pi Zero 2W with reduced preview framerate)
- USB webcam or Pi Camera Module
- Copy-stand or overhead mount to hold the camera above the sheet

### Camera configuration

The default backend is **OpenCV (USB webcam)**. Edit `config.py` to change:

```python
CAMERA = {
    "backend": "opencv",    # "opencv" | "picamera2" | "auto" | "none"
    ...
}
```

Use `"picamera2"` only if a Pi Camera Module is physically connected.
`"auto"` is unreliable when both picamera2 and a USB webcam are present.

### Optional I/O

#### Buttons (GPIO BCM, active LOW with internal pull-up)

| Function      | Default Pin | Notes                        |
|---------------|-------------|------------------------------|
| CAPTURE       | GPIO 17     | Main capture trigger         |
| SAVE          | GPIO 27     | Save current page to gallery |
| CANCEL / BACK | GPIO 22     | Return to capture screen     |
| NEXT          | GPIO 23     | Gallery: next page / tab     |
| PREV          | GPIO 24     | Gallery: prev page / tab     |

Wire each button between the GPIO pin and GND. No external resistors needed.

#### Outputs

| Function          | Default Pin | Notes                             |
|-------------------|-------------|-----------------------------------|
| LIGHT_RELAY       | GPIO 18     | Active HIGH — drives a relay      |
| LED_READY (green) | GPIO 25     | Lit when system is idle/ready     |
| LED_BUSY  (red)   | GPIO 8      | Lit during capture/processing     |

#### 2×32 LCD Panel (I2C, optional)

Any HD44780-compatible LCD with a PCF8574 I2C backpack.

| LCD signal | Pi pin         |
|------------|----------------|
| VCC        | 5V (pin 2)     |
| GND        | GND (pin 6)    |
| SDA        | GPIO 2 (pin 3) |
| SCL        | GPIO 3 (pin 5) |

Default I2C address: `0x27`. Find yours with `i2cdetect -y 1`.
Change in `config.py`: `LCD = { "i2c_addr": 0x3F, ... }`

---

## Installation

### 1. System packages

```bash
sudo apt update
sudo apt install -y \
    tesseract-ocr \
    tesseract-ocr-eng \
    python3-tk \
    python3-pip \
    python3-venv \
    i2c-tools \
    fonts-dejavu-mono
```

For Pi Camera Module support also install:

```bash
sudo apt install -y libcamera-apps
```

### 2. Enable I2C (for the LCD panel)

```bash
sudo raspi-config nonint do_i2c 0
```

A reboot is required. Confirm with `i2cdetect -y 1`.

### 3. Create the virtual environment

On Raspberry Pi OS Bookworm the system Python is externally managed.
The `--system-site-packages` flag gives the venv access to `python3-tk`
and `picamera2`, which cannot be pip-installed.

```bash
cd teletext_app
python3 -m venv --system-site-packages venv
```

### 4. Activate the virtual environment

```bash
source venv/bin/activate
```

Add to `~/.bashrc` to activate automatically on login.

### 5. Install Python dependencies

```bash
pip install --upgrade pip
pip install \
    opencv-contrib-python \
    Pillow \
    pytesseract \
    RPLCD
```

> **Important:** Use `opencv-contrib-python`, not `opencv-python` or
> `opencv-python-headless`. The `-contrib` variant includes the ArUco
> marker module required for grid corner detection.
>
> Install Pillow inside the venv even if it exists system-wide — the
> system copy on Raspberry Pi OS often lacks `ImageTk`.

Pi-specific (skip on desktop):

```bash
pip install RPi.GPIO
# picamera2 is available via --system-site-packages; no pip install needed
```

### 6. Verify

```bash
python3 -c "import cv2, PIL, pytesseract; print('OK')"
tesseract --version
```

### Automated installer

```bash
bash install.sh
```

---

## Running

```bash
source venv/bin/activate

python3 main.py                          # full hardware mode
python3 main.py --debug                  # verbose logging
python3 main.py --no-hardware --no-lcd   # desktop / development
python3 main.py --no-lcd                 # Pi with GPIO, no LCD
```

### Keyboard shortcuts

| Key     | Action                   |
|---------|--------------------------|
| Space   | Capture                  |
| Enter   | Save to gallery          |
| Escape  | Cancel / back            |
| ← / →   | Prev / next gallery item |

### Convenience launcher

```bash
cat > ~/start_digitiser.sh <<'EOF'
#!/bin/bash
cd ~/teletext_app
source venv/bin/activate
exec python3 main.py "$@"
EOF
chmod +x ~/start_digitiser.sh
```

---

## The template sheet

### Layout

```
┌──────────────────────┬──[M0]─────────────────────────────[M1]──┐
│                      │                                          │
│  Instructions        │   40×24 Teletext grid                    │
│  panel               │   Each cell subdivided 2×3 (sixels)      │
│  (2mm black border)  │                                          │
│                      │                                          │
└──────────────────────┴──[M3]─────────────────────────────[M2]──┘
```

Four 4×4 ArUco markers (IDs 0–3) sit at the grid corners:
- M0 (TL) and M3 (BL): left edges flush with the grid left border
- M1 (TR) and M2 (BR): right edges flush with the grid right border

The inward-facing corner of each marker is exactly coincident with the
corresponding grid corner — so detected marker corners give grid corners
directly, with no offset calculation.

The instructions panel on the left is boxed with a 2mm black border.
The title "MAKE YOUR OWN TELETEXT PAGE" is printed in `teletext2.ttf`
between the two top markers.

### Generating the template

```bash
source venv/bin/activate
python3 template.py --output ~/Desktop
```

This writes `template.png` and `template.pdf`. Print `template.pdf` at
**100% scale** (no fit-to-page scaling) on A4 paper.

No configuration changes are needed after printing — the corner marker
IDs and dictionary are already set in `config.py`.

### teletext2.ttf

Place `teletext2.ttf` in the same directory as the Python files to enable
authentic teletext font rendering in both the template title and the
preview pane. Without it, Courier is used as a fallback.

### Filling in the sheet

The instructions are printed on the sheet itself, but in brief:

1. Use **one colour per rectangle** (character cell).
2. To change colour, **leave one horizontal rectangle empty** — this becomes
   the colour control code position.
3. Add text using **one rectangle per letter**, written in block capitals.
4. **Do not colour in cells you want black** — the teletext background is
   black by default; the software handles this.
5. To switch between text and block graphics, annotate a cell with
   `[GFX]` or `[ALF]`.

---

## Colour calibration

The CALIBRATE tab lets you tune colour detection to your specific pencils.

### Why calibrate?

Different pencil brands produce different hue values. A "red" coloured
pencil from one brand may have a noticeably different hue to another.
Calibration measures your actual pencils and adjusts the detection ranges
to match.

### Steps

1. In the CALIBRATE tab, press **GENERATE CHART** — this saves
   `calibration_chart.png/pdf` to `~/digitiser_gallery/calibration/`.
2. Print the chart on any printer (monochrome laser is fine — the chart
   is black-and-white with hatch patterns).
3. Colour each labelled swatch with the matching pencil.
4. Place the chart under the camera and press **CAPTURE CHART**.
5. The calibration is saved to `~/digitiser_gallery/calibration.json`
   and used automatically for all future captures.

Press **RESET TO DEFAULT** to remove calibration and revert to the
built-in hue ranges.

---

## TTI file format

Generated files follow the standard TTI (Teletext Interchange) format,
compatible with Teletext Designer (Windows), wxTED (Linux/Windows),
and vbit2 (Pi broadcast inserter).

Control codes use ESC encoding: `ESC (0x1B)` + `(code + 0x40)`.
For example, Red alphanumeric (0x01) → bytes `1B 41`.

Row 0 is a header line (configured in `config.py` under `"header_line"`).
Rows 1–24 are the 24 teletext character rows.

---

## Configuration reference

All settings are in `config.py`. Key values:

```python
CAMERA = {
    "backend": "opencv",       # camera backend
    "rotation": 0,             # 0, 90, 180, 270
}

DIGITISER = {
    "empty_brightness_threshold": 245,   # cells brighter than this = blank
    "min_saturation": 12,                # HSV saturation floor for ink detection
    "dark_value_threshold": 160,         # HSV value below this = dark ink
    "header_line": "        BLOCK PARTY 26 Pmpp     %H:%M.%S",
    "template_params": {
        "aruco_dict": "DICT_4X4_50",
        "corner_ids": [0, 1, 2, 3],      # TL, TR, BR, BL
    },
}

RENDERER = {
    "teletext_font": "teletext2.ttf",    # path to teletext2.ttf or None
}
```

---

## File layout

```
teletext_app/
├── main.py          Entry point
├── config.py        All pin / CV / LCD settings
├── camera.py        Camera abstraction (picamera2 / OpenCV / none)
├── hardware.py      GPIO buttons, relay, indicator LEDs
├── lcd.py           2×32 LCD manager (RPLCD / I2C)
├── template.py      Template sheet generator (run once to print)
├── calibration.py   Colour calibration chart generator and processor
├── digitiser.py     CV pipeline: ArUco → warp → classify → OCR → TTI
├── renderer.py      Teletext page renderer (Tkinter canvas + teletext2.ttf)
├── gallery.py       Save / load / delete TTI gallery
├── gui.py           Tkinter GUI (Capture / Preview / Gallery / Calibrate tabs)
├── install.sh       Dependency installer
└── README.md        This file
```

Gallery: `~/digitiser_gallery/`
Log: `~/digitiser.log`
