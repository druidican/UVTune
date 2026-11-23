#!/usr/bin/env bash
# Fjerner UV Tune helt fra systemet.
#
#   sudo ./uninstall.sh           fjerner programmet, beholder dine testresultater
#   sudo ./uninstall.sh --purge   fjerner også testresultater, logs og indstillinger
#
# python3-venv og python3-pip bliver liggende, da andre programmer kan bruge dem.
set -uo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Kør med sudo:  sudo ./uninstall.sh"
  exit 1
fi
PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

USER_NAME="${SUDO_USER:-}"
USER_HOME=""
[[ -n "$USER_NAME" && "$USER_NAME" != "root" ]] && USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
DATA="${USER_HOME:+$USER_HOME/.local/share/uvtune}"

echo "==> Stopper UV Tune (en kørende test afbrydes, og kortet sættes tilbage til standard)"
pkill -f "uvtune-gui" 2>/dev/null || true
pkill -f "uvtune.gui" 2>/dev/null || true
systemctl disable --now uvtune-gui 2>/dev/null || true
systemctl stop uvtune-worker 2>/dev/null || true
systemctl disable uvtune-worker 2>/dev/null || true

# Sikkerhedsnet: hvis en test stadig stod som aktiv, nulstilles kortet direkte.
ACTIVE=0
if [[ -n "$DATA" && -f "$DATA/state.json" ]] && grep -q '"active": true' "$DATA/state.json"; then
  ACTIVE=1
fi
if [[ $ACTIVE -eq 1 ]]; then
  echo "==> Nulstiller grafikkortets spænding og power limit"
  for f in /sys/class/drm/card*/device/pp_od_clk_voltage; do
    [[ -w "$f" ]] || continue
    echo r > "$f" 2>/dev/null; echo c > "$f" 2>/dev/null
  done
  for h in /sys/class/drm/card*/device/hwmon/hwmon*; do
    [[ -f "$h/power1_cap_default" && -w "$h/power1_cap" ]] || continue
    cat "$h/power1_cap_default" > "$h/power1_cap" 2>/dev/null
  done
fi
# Start LACT igen, hvis testen satte den på pause.
if systemctl is-enabled lactd >/dev/null 2>&1 && ! systemctl is-active lactd >/dev/null 2>&1; then
  systemctl start lactd 2>/dev/null || true
fi
# Hardware-watchdog-driveren blev evt. indlæst af testen. Fejler stille, hvis den er i brug.
modprobe -r sp5100_tco 2>/dev/null || true

echo "==> Fjerner tjenester og programfiler"
rm -f /etc/systemd/system/uvtune-worker.service /etc/systemd/system/uvtune-gui.service
rm -f /etc/systemd/system/multi-user.target.wants/uvtune-worker.service \
      /etc/systemd/system/multi-user.target.wants/uvtune-gui.service
systemctl daemon-reload
systemctl reset-failed uvtune-worker uvtune-gui 2>/dev/null || true
rm -rf /opt/uvtune
rm -f /usr/local/bin/uvtune-gui
[[ -n "$USER_HOME" ]] && rm -f "$USER_HOME/.local/share/applications/uvtune.desktop"

if [[ $PURGE -eq 1 && -n "$DATA" ]]; then
  echo "==> Sletter testresultater og indstillinger ($DATA)"
  rm -rf "$DATA"
fi

echo
echo "UV Tune er fjernet."
if [[ $PURGE -eq 0 && -n "$DATA" && -d "$DATA" ]]; then
  echo "Dine testresultater ligger stadig i $DATA"
  echo "Slet dem med:  sudo ./uninstall.sh --purge   (eller rm -rf \"$DATA\")"
fi
