#!/usr/bin/env bash
#
# Doda znano Wi-Fi omrezje v NetworkManager. Pri boot-u bo
# wifi_fallback.sh sprva preverjal vsa znana omrezja; ce nobeno
# ni v dosegu, preklopi v AP nacin.
#
# Uporaba:
#   sudo ./add_wifi.sh <SSID> <PSK> [priority]
#
# priority je opcijska celostevilcna vrednost (visja = bolj prednostna).
# Privzeto 10.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Zazeni kot root: sudo $0 <SSID> <PSK>"; exit 1; }

if [[ $# -lt 2 ]]; then
    echo "Uporaba: sudo $0 <SSID> <PSK> [priority]"
    echo "  Primer: sudo $0 Area51 'mojeGeslo' 100"
    exit 1
fi

SSID="$1"
PSK="$2"
PRIORITY="${3:-10}"
IFACE="${IFACE:-wlan0}"

CON_NAME="$SSID"

# Idempotentno: ce profil ze obstaja, ga prepisi
if nmcli -t -f NAME con show | grep -qxF "$CON_NAME"; then
    echo "  Profil '$CON_NAME' ze obstaja, ga prepisem..."
    nmcli con delete "$CON_NAME" >/dev/null
fi

nmcli con add type wifi \
    ifname "$IFACE" \
    con-name "$CON_NAME" \
    ssid "$SSID" \
    autoconnect yes >/dev/null

nmcli con modify "$CON_NAME" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$PSK" \
    connection.autoconnect-priority "$PRIORITY" \
    ipv6.method auto

echo "  ✓ Dodal '$SSID' (priority $PRIORITY, autoconnect=yes)"
echo "  ✓ Ob naslednjem boot-u (ali rescan-u) bo NetworkManager poskusil to omrezje."
echo
echo "  Trenutni seznam znanih Wi-Fi omrezij:"
nmcli -t -f NAME,TYPE,AUTOCONNECT-PRIORITY connection show \
    | awk -F: '$2 == "802-11-wireless" {printf "    - %s (prio %s)\n", $1, $3}'
