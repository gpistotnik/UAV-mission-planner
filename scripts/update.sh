#!/usr/bin/env bash
#
# Update droneva mission planner sklada na zadnjo verzijo iz git remote-a.
#
# Postopek:
#   1. Preveri uncommitted spremembe (ali stash / discard / abort)
#   2. git fetch + pull (ff-only)
#   3. Poskrbi za .venv + pip (requirements*)
#   4. npm install (ce manjka) + npm run build (ce treba)
#   5. migrate + collectstatic
#   6. Namesti systemd unit-e / sudoers iz repoja (ce so se spremenili)
#   7. systemctl restart relevantnih servisov
#   8. Hitri healthcheck (:80)
#
# Idempotenten. Namenjen zagonu na RPi kot uporabnik dron.
#
# Uporaba:
#   bash scripts/update.sh                  # fail ce so lokalne spremembe
#   bash scripts/update.sh --stash          # auto-stash, pull, unstash
#   bash scripts/update.sh --force          # discard lokalne → tocen origin
#   bash scripts/update.sh --sync           # enako kot --force (priporoceno na dronu)
#   bash scripts/update.sh --no-restart     # samo update, ne restartaj
#   bash scripts/update.sh --skip-frontend  # preskoci npm
#   bash scripts/update.sh --frontend       # vedno zgradi frontend
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/mission_planner}"
MODE="default"   # default | stash | force
RESTART=true
DO_FRONTEND=true
FORCE_FRONTEND=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --stash)         MODE="stash"; shift ;;
        --force|--sync)  MODE="force"; shift ;;
        --no-restart)    RESTART=false; shift ;;
        --skip-frontend) DO_FRONTEND=false; shift ;;
        --frontend)      FORCE_FRONTEND=true; shift ;;
        --help|-h)
            sed -n '/^#/p' "$0" | head -35
            exit 0
            ;;
        *) echo "Nepoznan flag: $1 (probaj --help)"; exit 1 ;;
    esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  • %s\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }
ok()   { printf '\033[32m  ✓ %s\033[0m\n' "$*"; }

[[ $EUID -ne 0 ]] || die "NE zazeni kot root. Skripta sama klice sudo, kjer rabi."
[[ -d $REPO_DIR/.git ]] || die "Repo na $REPO_DIR ne obstaja (ali ni git repo)."
cd "$REPO_DIR"

command -v git >/dev/null || die "manjka git"
command -v sudo >/dev/null || die "manjka sudo"

# -------- 1. Preveri uncommitted spremembe --------
bold "1. Preverim git stanje..."

# Poškodovan .git (prazen object) → jasno sporocilo
if ! git rev-parse HEAD >/dev/null 2>&1; then
    die "Git repo je pokvarjen (bad object HEAD).
    Popravilo: premakni mapo in naredi svež clone:
      mv ~/mission_planner ~/mission_planner.broken
      git clone <url> ~/mission_planner
      cp ~/mission_planner.env.bak ~/mission_planner/.env   # ce obstaja"
fi

STATUS=$(git status --porcelain)
STASHED=false

if [[ -n "$STATUS" ]]; then
    warn "Lokalne spremembe (uncommitted):"
    echo "$STATUS" | sed 's/^/    /'
    echo

    case $MODE in
        stash)
            info "Stash-am pred pull-om..."
            git stash push -u -m "update.sh auto-stash $(date +%Y%m%d-%H%M%S)"
            ok "Stashano"
            STASHED=true
            ;;
        force)
            warn "DISCARDING lokalne spremembe (sync na origin)..."
            git reset --hard HEAD
            git clean -fd
            ok "Lokalne spremembe povozene"
            ;;
        default)
            die "Imas lokalne spremembe. Na dronu priporoceno:
    bash $0 --sync          # zavrzi lokalno, potegni origin (varno za appliance)
    bash $0 --stash         # shrani lokalno, pull, poskusi vrniti
    bash $0 --force         # enako kot --sync"
            ;;
    esac
else
    ok "Brez lokalnih sprememb"
fi

# -------- 2. git pull --------
bold "2. git pull..."
BRANCH=$(git branch --show-current)
[[ -n "$BRANCH" ]] || die "Detached HEAD — checkoutaj vejo (npr. main)."
info "Trenutna veja: $BRANCH"
BEFORE=$(git rev-parse HEAD)

git fetch origin "$BRANCH" || die "git fetch ni uspel (mreza / remote)."

if [[ "$MODE" == "force" ]]; then
    # Tocno kot origin — brez lokalnih odstopanj (SCP / rocni editi).
    git reset --hard "origin/$BRANCH"
else
    git pull --ff-only origin "$BRANCH" || \
        die "git pull ni uspel (non-fast-forward?). Poskusi: bash $0 --sync"
fi

AFTER=$(git rev-parse HEAD)

if [[ "$BEFORE" == "$AFTER" ]]; then
    ok "Ze na zadnjem commit-u ($AFTER)."
    NEW_COMMITS=0
else
    NEW_COMMITS=$(git rev-list "$BEFORE..$AFTER" --count)
    ok "Prevzetih $NEW_COMMITS commit-ov → $AFTER"
    info "Spremembe:"
    git log --oneline "$BEFORE..$AFTER" | sed 's/^/    /'
fi

# -------- 3. Stash unwind --------
if [[ "$STASHED" == "true" ]]; then
    bold "3. Vracam stashed spremembe..."
    if git stash pop; then
        ok "Stash uspesno povrnjen"
    else
        warn "MERGE KONFLIKT pri stash pop — rocno: git status"
    fi
fi

changed() {
    # Je bila datoteka/mapa spremenjena med BEFORE..AFTER? Ob 0 commitih → ne.
    [[ "$NEW_COMMITS" -gt 0 ]] || return 1
    git diff --name-only "$BEFORE" "$AFTER" | grep -qE "$1"
}

# -------- 4. Python venv + deps --------
bold "4. Python okolje..."
if [[ ! -x .venv/bin/python ]]; then
    warn ".venv manjka — ustvarjam..."
    python3 -m venv .venv
    ok "venv ustvarjen"
fi
# shellcheck disable=SC1091
source .venv/bin/activate

NEED_PIP=false
if ! python -c "import django" 2>/dev/null; then
    NEED_PIP=true
    warn "Django ni v venv — pip install"
elif changed '^requirements(-rpi)?\.txt$'; then
    NEED_PIP=true
fi

if [[ "$NEED_PIP" == "true" ]]; then
    pip install -q -r requirements.txt
    if [[ -f requirements-rpi.txt ]]; then
        pip install -q -r requirements-rpi.txt || warn "requirements-rpi.txt delno spodletel"
    fi
    ok "Python deps OK"
elif [[ "$NEW_COMMITS" -gt 0 ]]; then
    info "(requirements* nespremenjeni)"
else
    info "(brez novih commit-ov, Python OK)"
fi

# -------- 5. Frontend --------
if [[ "$DO_FRONTEND" == "true" ]]; then
    NEED_BUILD=false
    if [[ "$FORCE_FRONTEND" == "true" ]]; then
        NEED_BUILD=true
    elif [[ ! -f static/js/planner.js ]]; then
        NEED_BUILD=true
        warn "static/js/planner.js manjka → build"
    elif changed '^(frontend/|static/)'; then
        NEED_BUILD=true
    fi

    if [[ "$NEED_BUILD" == "true" ]]; then
        bold "5. Frontend build..."
        command -v npm >/dev/null || die "manjka npm — apt install nodejs npm"
        cd frontend
        if [[ ! -d node_modules ]] || [[ ! -d node_modules/esbuild ]]; then
            info "npm install (node_modules manjka ali nepopoln)..."
            npm install --no-fund --no-audit
        elif changed '^frontend/package.json$' || changed '^frontend/package-lock.json$'; then
            info "package.json spremenjen → npm install..."
            npm install --no-fund --no-audit
        fi
        npm run build
        cd ..
        [[ -f static/js/planner.js ]] && ok "Bundle: $(du -h static/js/planner.js | cut -f1)" \
            || die "Build ni ustvaril static/js/planner.js"
    else
        info "(frontend nespremenjen, preskakam build)"
    fi
else
    info "(--skip-frontend)"
fi

# -------- 6. Django migrate --------
bold "6. Django migrate..."
# Ce so migracije stisnjene v eno 0001_initial, pocisti orfane zapise
# (stari 0002–0005), sicer Django vrze NodeNotFoundError.
PYTHONPATH="$REPO_DIR" python - <<'PY'
import os
from pathlib import Path
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "missionplanner.settings")
import django
django.setup()
from django.core.management import call_command
from django.db import connection
files = {p.stem for p in Path("missions/migrations").glob("0*.py")}
with connection.cursor() as c:
    c.execute("SELECT name FROM django_migrations WHERE app=%s", ["missions"])
    applied = {r[0] for r in c.fetchall()}
orphans = applied - files
if orphans and files == {"0001_initial"}:
    print(f"  • Squash: brisem orfane zapise migracij: {sorted(orphans)}")
    with connection.cursor() as c:
        c.execute("DELETE FROM django_migrations WHERE app=%s", ["missions"])
    # Tabele ze obstajajo — oznaci novo 0001 kot applied.
    call_command("migrate", "missions", "0001", fake=True, verbosity=0)
    print("  • Squash: missions.0001_initial --fake")
PY
python manage.py migrate --noinput
python manage.py collectstatic --noinput 2>&1 | tail -1 || true
ok "DB + static OK"
deactivate

# -------- 7. Sistemske datoteke iz repoja --------
bold "7. Sistemski unit-i / sudoers..."
if [[ -f scripts/usb_storage_admin.py ]]; then
    if [[ ! -f /usr/local/sbin/mission-planner-usb-storage ]] \
       || ! cmp -s scripts/usb_storage_admin.py \
                    /usr/local/sbin/mission-planner-usb-storage; then
        sudo install -m 0755 scripts/usb_storage_admin.py \
            /usr/local/sbin/mission-planner-usb-storage
        ok "helper: mission-planner-usb-storage"
    fi
fi

UNIT_DIR="$REPO_DIR/scripts/systemd"
if [[ -d "$UNIT_DIR" ]]; then
    UNITS_CHANGED=false
    if [[ "$NEW_COMMITS" -eq 0 ]]; then
        # Ob --sync / prvi namestitvi vseeno uskladi, ce unit manjka ali se razlikuje
        for f in "$UNIT_DIR"/*.service; do
            [[ -f "$f" ]] || continue
            name=$(basename "$f")
            if [[ ! -f "/etc/systemd/system/$name" ]] \
               || ! cmp -s "$f" "/etc/systemd/system/$name"; then
                UNITS_CHANGED=true
                break
            fi
        done
    elif changed '^scripts/systemd/'; then
        UNITS_CHANGED=true
    fi

    if [[ "$UNITS_CHANGED" == "true" ]]; then
        for f in "$UNIT_DIR"/*.service; do
            [[ -f "$f" ]] || continue
            name=$(basename "$f")
            sudo install -m 0644 "$f" "/etc/systemd/system/$name"
            info "unit: $name"
        done
        sudo systemctl daemon-reload
        ok "systemd unit-i posodobljeni"
    else
        info "(systemd unit-i nespremenjeni)"
    fi
fi

if [[ -d scripts/sudoers ]]; then
    for rule in scripts/sudoers/*; do
        [[ -f "$rule" ]] || continue
        name=$(basename "$rule")
        if [[ ! -f "/etc/sudoers.d/$name" ]] \
           || ! cmp -s "$rule" "/etc/sudoers.d/$name"; then
            sudo install -m 0440 "$rule" "/etc/sudoers.d/$name"
            sudo visudo -cf "/etc/sudoers.d/$name" >/dev/null
            ok "sudoers: $name"
        fi
    done
fi

# Captive portal na :80 konflikta z mission-planner — naj ostane off
if systemctl is-enabled --quiet drone-captive-portal 2>/dev/null \
   || systemctl is-active --quiet drone-captive-portal 2>/dev/null; then
    sudo systemctl disable --now drone-captive-portal 2>/dev/null || true
    info "drone-captive-portal izklopljen (Django na :80)"
fi

# -------- 8. Restart servisov --------
if [[ "$RESTART" == "true" ]]; then
    bold "8. Restart servisov..."
    for svc in mission-planner drone-camera drone-wifi-gpio-switch; do
        if systemctl cat "$svc.service" >/dev/null 2>&1; then
            sudo systemctl restart "$svc"
            sleep 1
            if sudo systemctl is-active --quiet "$svc"; then
                ok "$svc aktiven"
            else
                warn "$svc NE TECE — sudo journalctl -u $svc -n 30"
            fi
        else
            info "$svc.service ni namescen, preskakam"
        fi
    done
    # WiFi GPIO naj bo enabled, ce unit obstaja
    if systemctl cat drone-wifi-gpio-switch.service >/dev/null 2>&1; then
        sudo systemctl enable drone-wifi-gpio-switch >/dev/null 2>&1 || true
    fi
else
    warn "RESTART preskocen (--no-restart)."
fi

# -------- 9. Healthcheck --------
bold "9. Healthcheck..."
sleep 1
DJ=$(curl -fsS -o /dev/null -w "%{http_code}" http://127.0.0.1/nadzor/ 2>/dev/null || echo "ERR")
CAM=$(curl -fsS -o /dev/null -w "%{http_code}" http://127.0.0.1:8090/health 2>/dev/null || echo "ERR")
NET=$(curl -fsS http://127.0.0.1/api/network/status/ 2>/dev/null | head -c 80 || echo "ERR")
printf "  Django :80            → %s\n" "$DJ"
printf "  Camera :8090          → %s\n" "$CAM"
printf "  Network status        → %s\n" "$NET"

[[ "$DJ" == "200" ]] && ok "Django zdrav" || warn "Django ne odgovarja (pricakovano 200)"
[[ "$CAM" == "200" ]] && ok "Camera zdrava" || warn "Camera ne odgovarja"

echo
bold "✓ Update koncan."
info "HEAD: $(git rev-parse --short HEAD)"
[[ "$NEW_COMMITS" -gt 0 ]] && info "Novih commit-ov: $NEW_COMMITS" || info "Brez novih commit-ov (okolje usklajeno)."
info "Naslednji update na dronu:  bash scripts/update.sh --sync"
