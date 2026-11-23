#!/usr/bin/env bash
# Removes UV Tune completely from the system.
#
#   sudo ./uninstall.sh           removes the program, keeps your test results
#   sudo ./uninstall.sh --purge   also removes test results, logs and settings
#
# python3-venv and python3-pip are left installed, since other programs may use them.
set -uo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo:  sudo ./uninstall.sh"
  exit 1
fi
PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

USER_NAME="${SUDO_USER:-}"
USER_HOME=""
[[ -n "$USER_NAME" && "$USER_NAME" != "root" ]] && USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
DATA="${USER_HOME:+$USER_HOME/.local/share/uvtune}"

echo "==> Stopping UV Tune (a running test is aborted and the card is set back to stock)"
pkill -f "uvtune-gui" 2>/dev/null || true
pkill -f "uvtune.gui" 2>/dev/null || true
systemctl disable --now uvtune-gui 2>/dev/null || true
systemctl stop uvtune-worker 2>/dev/null || true
systemctl disable uvtune-worker 2>/dev/null || true

# Safety net: if a test was still marked as active, reset the card directly.
ACTIVE=0
if [[ -n "$DATA" && -f "$DATA/state.json" ]] && grep -q '"active": true' "$DATA/state.json"; then
  ACTIVE=1
fi
if [[ $ACTIVE -eq 1 ]]; then
  echo "==> Resetting the graphics card's voltage and power limit"
  for f in /sys/class/drm/card*/device/pp_od_clk_voltage; do
    [[ -w "$f" ]] || continue
    echo r > "$f" 2>/dev/null; echo c > "$f" 2>/dev/null
  done
  for h in /sys/class/drm/card*/device/hwmon/hwmon*; do
    [[ -f "$h/power1_cap_default" && -w "$h/power1_cap" ]] || continue
    cat "$h/power1_cap_default" > "$h/power1_cap" 2>/dev/null
  done
fi
# Start LACT again if the test paused it.
if systemctl is-enabled lactd >/dev/null 2>&1 && ! systemctl is-active lactd >/dev/null 2>&1; then
  systemctl start lactd 2>/dev/null || true
fi
# The hardware watchdog driver may have been loaded by the test. Fails silently if it is in use.
modprobe -r sp5100_tco 2>/dev/null || true

echo "==> Removing services and program files"
rm -f /etc/systemd/system/uvtune-worker.service /etc/systemd/system/uvtune-gui.service
rm -f /etc/systemd/system/multi-user.target.wants/uvtune-worker.service \
      /etc/systemd/system/multi-user.target.wants/uvtune-gui.service
systemctl daemon-reload
systemctl reset-failed uvtune-worker uvtune-gui 2>/dev/null || true
rm -rf /opt/uvtune
rm -f /usr/local/bin/uvtune-gui
[[ -n "$USER_HOME" ]] && rm -f "$USER_HOME/.local/share/applications/uvtune.desktop"

if [[ $PURGE -eq 1 && -n "$DATA" ]]; then
  echo "==> Deleting test results and settings ($DATA)"
  rm -rf "$DATA"
fi

echo
echo "UV Tune has been removed."
if [[ $PURGE -eq 0 && -n "$DATA" && -d "$DATA" ]]; then
  echo "Your test results are still in $DATA"
  echo "Delete them with:  sudo ./uninstall.sh --purge   (or rm -rf \"$DATA\")"
fi
