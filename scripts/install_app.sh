#!/usr/bin/env bash
#
# Aplikacijski bootstrap: po bootstrap_rpi.sh + reboot.
#
# Zazeni kot 'dron' (NE sudo) — venv in npm tecejo v user contextu.
# Skripta sama z 'sudo' poklice systemd dele.
#
# Idempotenten.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/mission_planner}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  • %s\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[32m  ✓ %s\033[0m\n' "$*"; }

[[ $EUID -ne 0 ]] || die "NE zazeni kot root — skripta sama klice sudo, kjer rabi."
[[ -d $REPO_DIR ]] || die "Repo na $REPO_DIR ne obstaja."
cd "$REPO_DIR"

# -------- 1. Python venv + deps --------
bold "1. Python venv + pip install..."
if [[ ! -d .venv ]]; then
    python3 -m venv --system-site-packages .venv
    info "ustvarjen .venv"
fi
# Obstoječemu okolju omogoči distro paket picamera2.
if [[ -f .venv/pyvenv.cfg ]]; then
    sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' \
        .venv/pyvenv.cfg
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip wheel
pip install -q -r requirements.txt -r requirements-rpi.txt
deactivate
ok "Python deps OK"

# -------- 2. Frontend build --------
bold "2. Frontend build..."
(
    cd frontend
    npm install --silent
    npm run build
)
[[ -f static/js/planner.js ]] || die "static/js/planner.js ni nastal po npm run build"
ok "Bundle: $(du -h static/js/planner.js | cut -f1)"

# -------- 3. .env --------
bold "3. .env konfiguracija..."
if [[ ! -f .env ]]; then
    cp .env.example .env
    # Generiraj sveze geslo, da ni privzeto
    SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
    sed -i "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=$SECRET|" .env
    sed -i 's|^DJANGO_ALLOWED_HOSTS=.*|DJANGO_ALLOWED_HOSTS=*|' .env
    sed -i 's|^DJANGO_DEBUG=.*|DJANGO_DEBUG=True|' .env
    # Bench: USB Pixhawk; samodejna povezava tudi ob DEBUG=True.
    if ! grep -q '^UAV_AUTOCONNECT=' .env; then
        printf '\nUAV_AUTOCONNECT=True\nUAV_SERIAL_DEVICE=auto\nUAV_SERIAL_BAUD=115200\n' >> .env
    fi
    printf '\n# Podatki letov: samodejno zaznan USB ključek\nUAV_STORAGE_ROOT=auto\n' >> .env
    info "ustvarjen z svezim SECRET_KEY"
else
    info "obstojec .env, ne dotikam se"
    # Obstoječim namestitvam dodaj autoconnect, ce manjka.
    if ! grep -q '^UAV_AUTOCONNECT=' .env; then
        printf '\nUAV_AUTOCONNECT=True\n' >> .env
        info "dodan UAV_AUTOCONNECT=True v obstojeci .env"
    fi
fi
ok ".env OK"

# -------- 4. DB --------
bold "4. Baza podatkov..."
# shellcheck disable=SC1091
source .venv/bin/activate
python manage.py migrate --noinput
python manage.py loaddata missions/fixtures/drones.json 2>&1 | tail -1 || true
python manage.py collectstatic --noinput 2>&1 | tail -1 || true
deactivate
ok "Migracije + fixtures OK"

# -------- 4b. sudoers: varen poweroff iz GUI (brez gesla) --------
bold "4b. sudoers za poweroff (GUI gumb)..."
sudo tee /etc/sudoers.d/dron-poweroff > /dev/null << 'EOF'
# Omogoči Django uporabniku varno ugasniti RPi (Nadzor → Ugasni RPi).
dron ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /bin/systemctl poweroff, /sbin/poweroff, /usr/sbin/poweroff
EOF
sudo chmod 440 /etc/sudoers.d/dron-poweroff
ok "sudoers: dron → poweroff"

# -------- 4c. USB storage helper: root-owned + ozko sudo pravilo --------
bold "4c. USB storage helper (izbira prek GUI)..."
sudo install -m 0755 scripts/usb_storage_admin.py \
    /usr/local/sbin/mission-planner-usb-storage
sudo install -m 0440 scripts/sudoers/dron-usb-storage \
    /etc/sudoers.d/dron-usb-storage
sudo visudo -cf /etc/sudoers.d/dron-usb-storage > /dev/null
ok "sudoers: dron → varna izbira USB shrambe"

# -------- 5. systemd unit: mission-planner (port 80) --------
bold "5. mission-planner.service (0.0.0.0:80)..."
# Captive portal tudi želi :80 — izklopimo ga, Django prevzame port.
if systemctl list-unit-files drone-captive-portal.service >/dev/null 2>&1; then
    sudo systemctl disable --now drone-captive-portal.service 2>/dev/null || true
    info "drone-captive-portal izklopljen (konflikt na portu 80)"
fi
sudo install -m 0644 scripts/systemd/mission-planner.service \
    /etc/systemd/system/mission-planner.service
# Prilagodi poti, ce REPO_DIR ni /home/dron/mission_planner
if [[ "$REPO_DIR" != "/home/dron/mission_planner" ]]; then
    sudo sed -i \
        -e "s|/home/dron/mission_planner|$REPO_DIR|g" \
        -e "s|User=dron|User=$USER|g" \
        -e "s|Group=dron|Group=$USER|g" \
        /etc/systemd/system/mission-planner.service
fi
sudo systemctl daemon-reload
sudo systemctl enable --now mission-planner.service
sleep 2
if sudo systemctl is-active --quiet mission-planner; then
    ok "mission-planner.service deluje na :80"
else
    sudo systemctl status mission-planner --no-pager | tail -10
    die "mission-planner.service ni zagnan"
fi

# -------- 5b. MAVLink mux (UART/USB → UDP za Django + camera_trigger) --------
bold "5b. drone-mavlink-mux.service..."
if [[ -f scripts/systemd/drone-mavlink-mux.service ]]; then
    sudo install -m 0644 scripts/systemd/drone-mavlink-mux.service \
        /etc/systemd/system/
    if [[ "$REPO_DIR" != "/home/dron/mission_planner" ]]; then
        sudo sed -i \
            -e "s|/home/dron/mission_planner|$REPO_DIR|g" \
            -e "s|User=dron|User=$USER|g" \
            /etc/systemd/system/drone-mavlink-mux.service
    fi
    sudo systemctl daemon-reload
    sudo systemctl enable --now drone-mavlink-mux.service
    sleep 1
    if sudo systemctl is-active --quiet drone-mavlink-mux; then
        ok "drone-mavlink-mux.service deluje"
    else
        warn "mavlink mux ni zagnan — preveri Pixhawk USB/UART"
    fi
    # Django naj gre na UDP 14550 (mux drži serijski port).
    if grep -q '^UAV_SERIAL_DEVICE=' .env; then
        sed -i 's|^UAV_SERIAL_DEVICE=.*|UAV_SERIAL_DEVICE=udp:127.0.0.1:14550|' .env
    else
        printf '\nUAV_SERIAL_DEVICE=udp:127.0.0.1:14550\n' >> .env
    fi
    info ".env: UAV_SERIAL_DEVICE=udp:127.0.0.1:14550"
    sudo systemctl restart mission-planner.service || true
else
    warn "scripts/systemd/drone-mavlink-mux.service ne obstaja"
fi

# -------- 6. Camera services --------
bold "6. camera stream + trigger services..."
if [[ -f scripts/systemd/drone-camera.service ]]; then
    sudo install -m 0644 scripts/systemd/drone-camera.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now drone-camera.service
    sleep 2
    if sudo systemctl is-active --quiet drone-camera; then
        ok "drone-camera.service deluje"
    else
        warn "drone-camera ni zagnan — preveri 'sudo journalctl -u drone-camera -n 20'"
    fi
else
    warn "scripts/systemd/drone-camera.service ne obstaja"
fi
if [[ -f scripts/systemd/drone-camera-trigger.service ]]; then
    sudo install -m 0644 scripts/systemd/drone-camera-trigger.service \
        /etc/systemd/system/
    if [[ "$REPO_DIR" != "/home/dron/mission_planner" ]]; then
        sudo sed -i \
            -e "s|/home/dron/mission_planner|$REPO_DIR|g" \
            -e "s|User=dron|User=$USER|g" \
            -e "s|Group=dron|Group=$USER|g" \
            /etc/systemd/system/drone-camera-trigger.service
    fi
    sudo systemctl daemon-reload
    sudo systemctl enable --now drone-camera-trigger.service
    if sudo systemctl is-active --quiet drone-camera-trigger; then
        ok "drone-camera-trigger.service deluje"
    else
        warn "camera trigger ni zagnan — preveri USB in journal"
    fi
fi

# -------- 7. AP + captive portal + fallback (preko obstojece setup_ap.sh) --------
bold "7. AP profil + captive portal + WiFi fallback..."
if [[ -f scripts/setup_ap.sh ]]; then
    sudo bash scripts/setup_ap.sh
    ok "AP setup koncan"
else
    warn "scripts/setup_ap.sh ne obstaja"
fi

# -------- 8. Povzetek --------
echo
bold "✓ Aplikacijski bootstrap koncan."
echo
info "Servisi:"
for svc in drone-mavlink-mux mission-planner drone-camera drone-camera-trigger drone-captive-portal drone-wifi-fallback; do
    state=$(sudo systemctl is-active "$svc" 2>/dev/null || echo "missing")
    enabled=$(sudo systemctl is-enabled "$svc" 2>/dev/null || echo "missing")
    printf "    %-30s state=%-10s enabled=%s\n" "$svc" "$state" "$enabled"
done
echo
info "Hitri preverbi:"
echo "    curl -I http://localhost/nadzor/         # 200 OK"
echo "    curl -I http://localhost:8090/health        # 200 OK"
echo "    curl -I http://localhost/                   # 302 (captive)"
echo
info "Dodaj domace Wi-Fi (po zelji):"
echo "    sudo bash scripts/add_wifi.sh Area51 'tvoje-geslo'"
echo
info "Reboot test:"
echo "    sudo reboot                                 # vse mora startati"
