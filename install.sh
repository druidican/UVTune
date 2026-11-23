#!/usr/bin/env bash
# Installerer UV Tune: worker-tjeneste (root, systemd) + GUI (din bruger).
#
#   sudo ./install.sh            appen kan kun ses på denne maskine
#   sudo ./install.sh --remote   appen kører altid som tjeneste og kan ses fra andre
#                                maskiner på netværket (kun fremskridtsside)
#   --port 8095                  brug en anden port end 8090
set -euo pipefail

REMOTE=0
PORT=8090
while [[ $# -gt 0 ]]; do
  case "$1" in
    --remote) REMOTE=1 ;;
    --port) PORT="${2:?Angiv et portnummer efter --port}"; shift ;;
    *) echo "Ukendt valg: $1"; exit 1 ;;
  esac
  shift
done

if [[ $EUID -ne 0 ]]; then
  echo "Kør med sudo:  sudo ./install.sh"
  exit 1
fi
USER_NAME="${SUDO_USER:-}"
if [[ -z "$USER_NAME" || "$USER_NAME" == "root" ]]; then
  echo "Kør scriptet med sudo fra din egen bruger (ikke som root direkte)."
  exit 1
fi
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
DATA="$USER_HOME/.local/share/uvtune"
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installerer pakker"
apt-get install -y python3-venv python3-pip >/dev/null

echo "==> Kopierer til /opt/uvtune"
rm -rf /opt/uvtune/src
mkdir -p /opt/uvtune/src
cp -r "$SRC/uvtune" "$SRC/pyproject.toml" /opt/uvtune/src/

echo "==> Opretter virtuelt miljø"
[[ -d /opt/uvtune/venv ]] || python3 -m venv /opt/uvtune/venv
/opt/uvtune/venv/bin/pip install -q --upgrade pip
/opt/uvtune/venv/bin/pip install -q /opt/uvtune/src

sudo -u "$USER_NAME" mkdir -p "$DATA"

echo "==> Opretter worker-tjeneste"
cat > /etc/systemd/system/uvtune-worker.service <<UNIT
[Unit]
Description=UV Tune worker (undervolt-test af AMD GPU)
After=local-fs.target systemd-journald.service
RequiresMountsFor=$USER_HOME

[Service]
Type=simple
Environment=UVTUNE_DATA=$DATA
Environment=UVTUNE_USER=$USER_NAME
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/uvtune/venv/bin/uvtune-worker
Restart=on-failure
RestartSec=5
TimeoutStopSec=90

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable uvtune-worker
systemctl restart uvtune-worker

if [[ $REMOTE -eq 1 ]]; then
  systemctl stop uvtune-gui 2>/dev/null || true
  if ss -ltnH "sport = :$PORT" | grep -q .; then
    echo "FEJL: port $PORT bruges allerede af et andet program:"
    ss -ltnp "sport = :$PORT" | tail -n +2
    echo "Vælg en anden port, fx:  sudo ./install.sh --remote --port 8095"
    exit 1
  fi
  echo "==> Opretter GUI-tjeneste med fjernvisning"
  cat > /etc/systemd/system/uvtune-gui.service <<UNIT
[Unit]
Description=UV Tune brugerflade (fjernvisning)
After=network-online.target uvtune-worker.service
Wants=network-online.target
RequiresMountsFor=$USER_HOME

[Service]
Type=simple
User=$USER_NAME
Environment=UVTUNE_DATA=$DATA
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/uvtune/venv/bin/uvtune-gui --host 0.0.0.0 --port $PORT --no-browser
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable uvtune-gui
  systemctl restart uvtune-gui
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    echo "    ufw-firewallen er aktiv. Tillad adgang fra dit lokale net, fx:"
    echo "    sudo ufw allow from 192.168.0.0/16 to any port $PORT proto tcp"
  fi
elif [[ -f /etc/systemd/system/uvtune-gui.service ]]; then
  echo "==> Slår fjernvisning fra"
  systemctl disable --now uvtune-gui 2>/dev/null || true
  rm -f /etc/systemd/system/uvtune-gui.service
  systemctl daemon-reload
fi

echo "==> Opretter kommando og menupunkt"
ln -sf /opt/uvtune/venv/bin/uvtune-gui /usr/local/bin/uvtune-gui
APPS="$USER_HOME/.local/share/applications"
sudo -u "$USER_NAME" mkdir -p "$APPS"
cat > "$APPS/uvtune.desktop" <<DESK
[Desktop Entry]
Name=UV Tune
Comment=Find laveste stabile undervolt
Exec=env UVTUNE_GUI_PORT=$PORT /usr/local/bin/uvtune-gui
Icon=utilities-system-monitor
Terminal=false
Type=Application
Categories=System;Utility;
DESK
chown "$USER_NAME": "$APPS/uvtune.desktop"

echo
echo "Færdig. Start appen med:  uvtune-gui   (eller find 'UV Tune' i menuen)"
echo "Den åbner i browseren på http://127.0.0.1:$PORT"
if [[ $REMOTE -eq 1 ]]; then
  for ip in $(hostname -I); do
    [[ "$ip" == *:* ]] || echo "Fra andre maskiner på netværket:  http://$ip:$PORT"
  done
fi
