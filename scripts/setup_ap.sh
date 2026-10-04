#!/usr/bin/env bash
#
# Pripravi AP profil za onboard wlan0 in namesti captive portal.
# AP se NE aktivira takoj — to opravi wifi_fallback.sh ob boot-u,
# ce nobeno znano omrezje ni v dosegu.
#
# Idempotenten.
set -euo pipefail

SSID="${SSID:-dron-F450}"
PSK="${PSK:-12345678}"
AP_IFACE="${AP_IFACE:-wlan0}"
AP_IP="${AP_IP:-10.0.0.1}"
AP_CON_NAME="${AP_CON_NAME:-dron-ap}"
REPO_DIR="${REPO_DIR:-/home/dron/mission_planner}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  • %s\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[32m  ✓ %s\033[0m\n' "$*"; }

# -------- 0. Predpogoji --------
[[ $EUID -eq 0 ]] || die "Tega skripta zazeni kot root (sudo $0)."

command -v nmcli >/dev/null || die "manjka nmcli — apt install network-manager"
[[ -f "$REPO_DIR/scripts/captive_portal.py" ]] \
    || die "manjka $REPO_DIR/scripts/captive_portal.py — git pull?"
[[ -f "$REPO_DIR/scripts/systemd/drone-captive-portal.service" ]] \
    || die "manjka systemd unit za captive portal"
[[ -f "$REPO_DIR/scripts/networkmanager/captive-dns.conf" ]] \
    || die "manjka captive-dns.conf"

# -------- 1. Preveri wlan0 --------
bold "1. Preverim, da $AP_IFACE obstaja..."
if ! ip link show "$AP_IFACE" >/dev/null 2>&1; then
    die "Naprava $AP_IFACE ne obstaja."
fi
ok "$AP_IFACE OK"

# -------- 2. Namesti dnsmasq override --------
bold "2. Namestim DNS hijack za captive probe domene..."
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
install -m 0644 \
    "$REPO_DIR/scripts/networkmanager/captive-dns.conf" \
    /etc/NetworkManager/dnsmasq-shared.d/captive.conf
ok "/etc/NetworkManager/dnsmasq-shared.d/captive.conf"

# -------- 3. Ustvari / posodobi AP profil --------
bold "3. NetworkManager AP profil '$AP_CON_NAME' (autoconnect=no)..."

if nmcli -t -f NAME con show | grep -qxF "$AP_CON_NAME"; then
    info "Profil ze obstaja, ga prepisem."
    # Ce je trenutno aktiven, ga ne podiraj — uporabnik bi izgubil SSH
    if nmcli -t -f NAME,STATE con show --active | grep -q "^$AP_CON_NAME:activated"; then
        warn "AP profil je trenutno aktiven. Ne podiram — samo prepisem konfig."
    else
        nmcli con delete "$AP_CON_NAME" >/dev/null
        nmcli con add type wifi ifname "$AP_IFACE" con-name "$AP_CON_NAME" \
            autoconnect no ssid "$SSID" >/dev/null
    fi
else
    nmcli con add type wifi ifname "$AP_IFACE" con-name "$AP_CON_NAME" \
        autoconnect no ssid "$SSID" >/dev/null
fi

# Tudi ce smo na novo ali ne, posodobi vse parametre
nmcli con modify "$AP_CON_NAME" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    802-11-wireless.channel 6 \
    802-11-wireless.ssid "$SSID" \
    ipv4.method shared \
    ipv4.addresses "$AP_IP/24" \
    ipv6.method ignore \
    connection.autoconnect no

nmcli con modify "$AP_CON_NAME" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$PSK"

ok "AP profil '$SSID' / WPA2 / IP $AP_IP/24 / autoconnect=NO"
info "AP se NE aktivira takoj — fallback servis odloca ob boot-u."

# -------- 4. Captive portal --------
# Django (mission-planner) na :80 sam dela 302 na http://dron.local/nadzor/
# (core.captive). Ločen drone-captive-portal bi konfliktiral na portu 80.
bold "4. Captive portal (Django middleware + DNS hijack)..."
if systemctl is-enabled --quiet mission-planner 2>/dev/null \
   || systemctl is-active --quiet mission-planner 2>/dev/null; then
    systemctl disable --now drone-captive-portal.service 2>/dev/null || true
    ok "Captive 302: Django → http://dron.local/nadzor/ (ločen servis izklopljen)"
else
    install -m 0644 \
        "$REPO_DIR/scripts/systemd/drone-captive-portal.service" \
        /etc/systemd/system/drone-captive-portal.service
    systemctl daemon-reload
    systemctl enable --now drone-captive-portal.service
    sleep 1
    if systemctl is-active --quiet drone-captive-portal; then
        ok "drone-captive-portal.service deluje na portu 80 → dron.local"
    else
        systemctl status drone-captive-portal --no-pager -l | tail -15
        die "Captive portal se ni zagnal."
    fi
fi

# -------- 5. Fallback servis --------
if [[ -f "$REPO_DIR/scripts/systemd/drone-wifi-fallback.service" ]] \
   && [[ -f "$REPO_DIR/scripts/wifi_fallback.sh" ]]; then
    bold "5. Namestitev drone-wifi-fallback.service..."
    install -m 0644 \
        "$REPO_DIR/scripts/systemd/drone-wifi-fallback.service" \
        /etc/systemd/system/drone-wifi-fallback.service
    systemctl daemon-reload
    systemctl enable drone-wifi-fallback.service
    ok "drone-wifi-fallback.service vklopljen (zagonil se bo ob naslednjem boot-u)"
else
    warn "wifi_fallback.sh / unit ne obstajata — preskakam."
fi

# -------- 6. Povzetek --------
bold "6. Povzetek:"
echo
echo "   AP SSID:      $SSID"
echo "   AP PSK:       $PSK"
echo "   AP IP:        $AP_IP/24"
echo "   AP iface:     $AP_IFACE (onboard, deli wlan0 med client in AP)"
echo "   Captive URL:  probe → http://dron.local/nadzor/"
echo
ok "Pripravljeno. AP se aktivira samo, ce klient mode ne najde znanega omrezja."
info "Trenutno tece klient mode. Sproti tece tudi captive portal na :80."
info "Reboot za test fallback-a (po zaustavi domace Wi-Fi)."
echo
info "Za dodajanje znanih Wi-Fi omrezij:"
info "  $REPO_DIR/scripts/add_wifi.sh <SSID> <PSK> [priority]"
