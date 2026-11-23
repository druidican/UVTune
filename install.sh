#!/usr/bin/env bash
# Installs UV Tune: worker service (root, systemd) + GUI (your user).
#
#   sudo ./install.sh            the app is only visible on this machine
#   sudo ./install.sh --remote   the app always runs as a service and can be viewed from other
#                                computers on the network (progress page only)
#   --port 8095                  use a different port than 8090
set -euo pipefail

REMOTE=0
PORT=8090
while [[ $# -gt 0 ]]; do
  case "$1" in
    --remote) REMOTE=1 ;;
    --port) PORT="${2:?Give a port number after --port}"; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo:  sudo ./install.sh"
  exit 1
fi
USER_NAME="${SUDO_USER:-}"
if [[ -z "$USER_NAME" || "$USER_NAME" == "root" ]]; then
  echo "Run the script with sudo from your own user (not as root directly)."
  exit 1
fi
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
DATA="$USER_HOME/.local/share/uvtune"
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing packages"
if command -v apt-get >/dev/null; then
  apt-get install -y python3-venv python3-pip >/dev/null
else
  echo "    apt-get not found: make sure python3-venv and python3-pip are installed"
fi

echo "==> Copying to /opt/uvtune"
rm -rf /opt/uvtune/src
mkdir -p /opt/uvtune/src
cp -r "$SRC/uvtune" "$SRC/pyproject.toml" /opt/uvtune/src/

echo "==> Creating virtual environment"
[[ -d /opt/uvtune/venv ]] || python3 -m venv /opt/uvtune/venv
/opt/uvtune/venv/bin/pip install -q --upgrade pip
/opt/uvtune/venv/bin/pip install -q /opt/uvtune/src

sudo -u "$USER_NAME" mkdir -p "$DATA"

echo "==> Creating worker service"
cat > /etc/systemd/system/uvtune-worker.service <<UNIT
[Unit]
Description=UV Tune worker (undervolt test for AMD GPUs)
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
    echo "ERROR: port $PORT is already used by another program:"
    ss -ltnp "sport = :$PORT" | tail -n +2
    echo "Choose another port, e.g.:  sudo ./install.sh --remote --port 8095"
    exit 1
  fi
  echo "==> Creating GUI service with remote viewing"
  cat > /etc/systemd/system/uvtune-gui.service <<UNIT
[Unit]
Description=UV Tune user interface (remote viewing)
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
    echo "    The ufw firewall is active. Allow access from your local network, e.g.:"
    echo "    sudo ufw allow from 192.168.0.0/16 to any port $PORT proto tcp"
  fi
elif [[ -f /etc/systemd/system/uvtune-gui.service ]]; then
  echo "==> Turning remote viewing off"
  systemctl disable --now uvtune-gui 2>/dev/null || true
  rm -f /etc/systemd/system/uvtune-gui.service
  systemctl daemon-reload
fi

echo "==> Creating command and menu entry"
ln -sf /opt/uvtune/venv/bin/uvtune-gui /usr/local/bin/uvtune-gui
APPS="$USER_HOME/.local/share/applications"
sudo -u "$USER_NAME" mkdir -p "$APPS"
cat > "$APPS/uvtune.desktop" <<DESK
[Desktop Entry]
Name=UV Tune
Comment=Find the lowest stable undervolt
Exec=env UVTUNE_GUI_PORT=$PORT /usr/local/bin/uvtune-gui
Icon=utilities-system-monitor
Terminal=false
Type=Application
Categories=System;Utility;
DESK
chown "$USER_NAME": "$APPS/uvtune.desktop"

echo
echo "Done. Start the app with:  uvtune-gui   (or find 'UV Tune' in the menu)"
echo "It opens in your browser at http://127.0.0.1:$PORT"
if [[ $REMOTE -eq 1 ]]; then
  for ip in $(hostname -I); do
    [[ "$ip" == *:* ]] || echo "From other computers on the network:  http://$ip:$PORT"
  done
fi
