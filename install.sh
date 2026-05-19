#!/bin/bash
# install.sh — Set up TDI620 Digitiser on Raspberry Pi OS (Bookworm)
# Run with: bash install.sh

set -e

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   TELETEXT CREATOR — INSTALLER       ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── System packages ───────────────────────────────────────────────────────────

echo "[1/5] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-eng \
    python3-tk \
    python3-pip \
    python3-venv \
    libcamera-apps \
    i2c-tools \
    python3-smbus \
    fonts-dejavu-mono \
    adb

# ── Enable I2C (for LCD) ──────────────────────────────────────────────────────

echo "[2/5] Enabling I2C interface..."
if ! grep -q "^dtparam=i2c_arm=on" /boot/firmware/config.txt 2>/dev/null; then
    echo "dtparam=i2c_arm=on" | sudo tee -a /boot/firmware/config.txt
    echo "  → I2C enabled (reboot required)"
else
    echo "  → I2C already enabled"
fi

# ── ADB udev rules (so Pi can talk to Android without root) ──────────────────

echo "[3/5] Configuring ADB udev rules..."
RULES_FILE="/etc/udev/rules.d/51-android.rules"
# Huawei vendor ID is 0x12d1
HUAWEI_RULE='SUBSYSTEM=="usb", ATTR{idVendor}=="12d1", MODE="0666", GROUP="plugdev"'
if ! grep -q "12d1" "$RULES_FILE" 2>/dev/null; then
    echo "$HUAWEI_RULE" | sudo tee -a "$RULES_FILE" > /dev/null
    sudo chmod a+r "$RULES_FILE"
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "  → Huawei udev rule added (idVendor 0x12d1)"
else
    echo "  → Huawei udev rule already present"
fi

# Add current user to plugdev group if not already a member
if ! groups "$USER" | grep -q plugdev; then
    sudo usermod -aG plugdev "$USER"
    echo "  → Added $USER to plugdev group (re-login required to take effect)"
else
    echo "  → $USER already in plugdev group"
fi

# ── Python virtual environment ────────────────────────────────────────────────

echo "[4/5] Creating Python virtual environment..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

python3 -m venv --system-site-packages venv
source venv/bin/activate

pip install --upgrade pip -q

# Core dependencies
pip install \
    opencv-contrib-python \
    Pillow \
    pytesseract \
    RPLCD

# Pi-specific (skip gracefully on desktop)
pip install RPi.GPIO 2>/dev/null || echo "  → RPi.GPIO skipped (not on Pi)"
pip install picamera2 2>/dev/null || echo "  → picamera2 skipped (not on Pi)"

# ── Desktop shortcut ──────────────────────────────────────────────────────────

echo "[5/5] Creating desktop shortcut..."
DESKTOP="$HOME/Desktop/TeletextCreator.desktop"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Name=TDI620 Digitiser
Comment=Digitise teletext design sheets
Exec=bash -c "cd $SCRIPT_DIR && source venv/bin/activate && python3 main.py"
Icon=camera
Terminal=false
Type=Application
Categories=Utility;
EOF
chmod +x "$DESKTOP"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   INSTALLATION COMPLETE!             ║"
echo "╠══════════════════════════════════════╣"
echo "║  Run:  source venv/bin/activate      ║"
echo "║        python3 main.py               ║"
echo "║                                      ║"
echo "║  Flags:                              ║"
echo "║    --debug        verbose logging    ║"
echo "║    --no-hardware  skip GPIO          ║"
echo "║    --no-lcd       skip LCD           ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ANDROID PHONE (IP WEBCAM) SETUP"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  One-time setup on your Huawei P9:"
echo ""
echo "  1. Install 'IP Webcam' (Pavel Khlebovich)"
echo "     from the Google Play Store."
echo ""
echo "  2. Enable Developer Options:"
echo "     Settings → About Phone"
echo "     → tap Build Number 7 times."
echo ""
echo "  3. Enable USB Debugging:"
echo "     Settings → Developer Options"
echo "     → USB Debugging ON."
echo ""
echo "  4. Connect phone to Pi with USB-C cable."
echo "     Accept 'Allow USB debugging?' on phone."
echo ""
echo "  5. In IP Webcam app:"
echo "     - Photo settings → Resolution: Maximum"
echo "     - Photo settings → Quality: 95"
echo "     - Port: 8080"
echo "     - Scroll to bottom → Start server"
echo ""
echo "  6. Verify connection:"
echo "     adb devices"
echo "     (should list your phone as 'device')"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
if [ -n "$(adb devices 2>/dev/null | grep -v 'List of' | grep 'device')" ]; then
    echo "  ✓ Phone detected via ADB right now!"
else
    echo "  ℹ  No phone detected yet — connect it"
    echo "     and accept the USB debugging prompt."
fi
echo ""
if [ -n "$(grep -c 'plugdev' /etc/group 2>/dev/null)" ]; then
    if ! groups "$USER" | grep -q plugdev; then
        echo "NOTE: Log out and back in for plugdev"
        echo "      group membership to take effect."
        echo ""
    fi
fi
if grep -q "^dtparam=i2c_arm=on" /boot/firmware/config.txt 2>/dev/null && \
   ! (vcgencmd get_config int 2>/dev/null | grep -q "dtparam=i2c_arm=1"); then
    echo "NOTE: If I2C was just enabled, reboot before using the LCD."
    echo ""
fi
