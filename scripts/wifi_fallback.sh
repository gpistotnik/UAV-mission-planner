#!/usr/bin/env bash
#
# Boot-time WiFi fallback. Ce v WAIT_SECONDS sekundah NetworkManager
# uspesno poveze wlan0 na katero koli ZNANO wifi omrezje (klient
# mode), ne naredi nic. Sicer aktivira "dron-ap" profil in zacne
# oddajati svoj SSID, da je pilotu v polju dron dostopen.
#
# Predvidena uporaba: zazenjen kot systemd oneshot ob boot-u,
# After=NetworkManager-wait-online.service.

set -u  # nocemo -e, ker zelimo loop prezimeti tudi posamicne napake

WAIT_SECONDS="${WIFI_FALLBACK_TIMEOUT:-30}"
AP_CON_NAME="${AP_CON_NAME:-dron-ap}"
IFACE="${IFACE:-wlan0}"

log() {
    echo "[wifi-fallback] $*"
    logger -t wifi-fallback -- "$*" 2>/dev/null || true
}

log "Cakam do $WAIT_SECONDS s na klient-povezavo na $IFACE..."

# Sprozimo enkratni scan, da hitreje najde AP-je
nmcli device wifi rescan ifname "$IFACE" 2>/dev/null || true

for ((i = 1; i <= WAIT_SECONDS; i++)); do
    # Aktivna wifi klient povezava na $IFACE? (kar koli razen nas same)
    active=$(nmcli -t -f NAME,DEVICE,TYPE,STATE connection show --active 2>/dev/null \
             | grep ":${IFACE}:" | grep ":activated$" || true)

    if [[ -n "$active" ]]; then
        # Filtriramo profil dron-ap (ce bi ze bil aktiven, kar tu ni
        # pricakovano, ker autoconnect=no, ampak vseeno)
        client=$(echo "$active" | grep -v "^${AP_CON_NAME}:" || true)
        if [[ -n "$client" ]]; then
            name=$(echo "$client" | head -1 | cut -d: -f1)
            log "Klient povezava aktivna: '$name'. Konec."
            exit 0
        fi
    fi

    sleep 1
done

log "V $WAIT_SECONDS s ni klient povezave. Aktiviram AP '$AP_CON_NAME'..."

# Preveri, ze profil obstaja
if ! nmcli -t -f NAME con show 2>/dev/null | grep -qxF "$AP_CON_NAME"; then
    log "NAPAKA: profil '$AP_CON_NAME' ne obstaja. Najprej zazeni setup_ap.sh."
    exit 1
fi

# Aktiviraj — to lahko vzame ~2-3 s
if nmcli con up "$AP_CON_NAME" 2>&1 | logger -t wifi-fallback; then
    log "AP aktiviran. SSID iz profila $(nmcli -t -f 802-11-wireless.ssid con show "$AP_CON_NAME" | cut -d: -f2)"
    exit 0
else
    log "NAPAKA: AP se ni aktiviral. Preveri 'journalctl -u NetworkManager'."
    exit 2
fi
