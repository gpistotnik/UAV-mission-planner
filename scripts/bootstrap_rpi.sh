#!/usr/bin/env bash
#
# Sistemski bootstrap za RPi 5 dronov sklad:
#  1. /boot/firmware/config.txt — usb_max_current_enable + kamera overlay
#  2. apt install sistemskih paketov
#  3. dron uporabnik v video + dialout skupine
#
# Po tem skriptu MORA priti reboot, nato pa install_app.sh.
#
# Variables (lahko prepisuje):
#   SENSOR=imx219    # podprto: imx219, imx477, imx378, imx708, imx290, ov9281
#   DRON_USER=dron
#
# Sintaksa Waveshare doc — tabela sensor → dtoverlay:
#   OV9281            dtoverlay=ov9281
#   IMX290 / IMX327   dtoverlay=imx290,clock-frequency=37125000
#   IMX378            dtoverlay=imx378
#   IMX219            dtoverlay=imx219   (privzeto za nas modul)
#   IMX477            dtoverlay=imx477
#   IMX708            dtoverlay=imx708
#
# IDEMPOTENTEN: zazenes lahko vec krat brez stranskih ucinkov.

set -euo pipefail

SENSOR="${SENSOR:-imx219}"
DRON_USER="${DRON_USER:-dron}"
CONFIG="/boot/firmware/config.txt"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  • %s\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[32m  ✓ %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || die "Zazeni kot root: sudo $0"
[[ -f $CONFIG ]] || die "$CONFIG ne obstaja — preveri, da si na Bookworm sistemu"

# -------- 0. Preveri uporabnika --------
bold "0. Preverim uporabnika '$DRON_USER'..."
if ! id "$DRON_USER" >/dev/null 2>&1; then
    die "Uporabnik '$DRON_USER' ne obstaja. Ustvari z 'useradd -m -G sudo -s /bin/bash $DRON_USER'"
fi
ok "$DRON_USER obstaja"

# -------- 1. Backup config.txt --------
bold "1. Backup /boot/firmware/config.txt..."
BACKUP="$CONFIG.bak.$(date +%Y%m%d-%H%M%S)"
cp "$CONFIG" "$BACKUP"
ok "Backup: $BACKUP"

# -------- 2. Power: usb_max_current_enable=1 --------
bold "2. usb_max_current_enable=1 (dovoli 5A USB-C PD profil)..."
if grep -q '^usb_max_current_enable=1' "$CONFIG"; then
    ok "ze nastavljeno"
elif grep -q '^#usb_max_current_enable' "$CONFIG"; then
    sed -i 's/^#usb_max_current_enable=.*/usb_max_current_enable=1/' "$CONFIG"
    ok "odkomentirano"
else
    echo "" >> "$CONFIG"
    echo "# F450 dron magistrska — power" >> "$CONFIG"
    echo "usb_max_current_enable=1" >> "$CONFIG"
    ok "dodano na konec"
fi

# -------- 3. Camera: auto-detect off + dtoverlay --------
bold "3. Kamera setup (sensor: $SENSOR)..."

# Doloci pravi overlay string
case "$SENSOR" in
    imx219)  OVERLAY="dtoverlay=imx219" ;;
    imx477)  OVERLAY="dtoverlay=imx477" ;;
    imx378)  OVERLAY="dtoverlay=imx378" ;;
    imx708)  OVERLAY="dtoverlay=imx708" ;;
    ov9281)  OVERLAY="dtoverlay=ov9281" ;;
    imx290 | imx327)
        OVERLAY="dtoverlay=imx290,clock-frequency=37125000"
        # IMX290 rabi dodatno JSON datoteko
        warn "IMX290 zahteva dodaten IPA JSON profil (Waveshare doc)."
        warn "Po reboot-u: sudo wget https://www.waveshare.net/w/upload/7/7a/Imx290.zip"
        warn "             sudo unzip Imx290.zip && sudo cp imx290.json /usr/share/libcamera/ipa/rpi/pisp/"
        ;;
    *)
        die "Nepoznan sensor '$SENSOR'. Podprto: imx219, imx477, imx378, imx708, ov9281, imx290/imx327"
        ;;
esac

# Onemogoci camera_auto_detect (Waveshare kloni nimajo EEPROM-a)
if grep -q '^camera_auto_detect=1' "$CONFIG"; then
    sed -i 's/^camera_auto_detect=1/camera_auto_detect=0/' "$CONFIG"
    ok "camera_auto_detect spremenjen na 0"
elif grep -q '^camera_auto_detect=0' "$CONFIG"; then
    ok "camera_auto_detect=0 ze nastavljeno"
else
    echo "camera_auto_detect=0" >> "$CONFIG"
    ok "dodano camera_auto_detect=0"
fi

# Dodaj dtoverlay (preveri, da ze ne obstaja)
if grep -q "^${OVERLAY}\$" "$CONFIG"; then
    ok "$OVERLAY ze nastavljeno"
elif grep -qE "^dtoverlay=(imx|ov|hailo)" "$CONFIG"; then
    # Obstaja drugi camera overlay, ga zamenjamo
    sed -i "s|^dtoverlay=\(imx\|ov\).*|$OVERLAY|" "$CONFIG"
    ok "zamenjan obstojeci overlay z $OVERLAY"
else
    echo "# F450 dron magistrska — kamera ($SENSOR)" >> "$CONFIG"
    echo "$OVERLAY" >> "$CONFIG"
    ok "dodano: $OVERLAY"
fi

# -------- 4. Apt update + paketi --------
bold "4. Sistemski paketi..."
export DEBIAN_FRONTEND=noninteractive
apt update -y -qq
PACKAGES=(
    python3 python3-pip python3-venv
    nodejs npm
    git curl
    python3-picamera2
    rpicam-apps
    i2c-tools
    network-manager
    libffi-dev libxml2-dev libxslt1-dev
)
apt install -y -qq "${PACKAGES[@]}"
ok "Namesceno: ${PACKAGES[*]}"

# -------- 5. Uporabnik v skupine --------
bold "5. $DRON_USER v skupine video + dialout + plugdev..."
for grp in video dialout plugdev netdev gpio i2c spi; do
    if getent group "$grp" >/dev/null && ! id -nG "$DRON_USER" | grep -qw "$grp"; then
        usermod -aG "$grp" "$DRON_USER"
        info "dodano v $grp"
    fi
done
ok "Skupine OK"

# -------- 6. Povzetek --------
echo
bold "✓ Sistemski bootstrap koncan."
echo
echo "  Spremembe v $CONFIG:"
grep -E "^(usb_max_current|camera_auto_detect|dtoverlay=(imx|ov))" "$CONFIG" | sed 's/^/    /'
echo
warn "REBOOT JE OBVEZEN, da spremembe pridejo v veljavo:"
echo "    sudo reboot"
echo
info "Po reboot-u zazeni: bash scripts/install_app.sh"
