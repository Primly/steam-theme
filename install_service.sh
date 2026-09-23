#!/usr/bin/env bash
# Steam Theme — systemd user-service installer (Linux / Bazzite).
#
# Creates two user units:
#   steam-theme.service     polling service (themes new games as you play)
#   steam-theme-ui.service  config page at http://127.0.0.1:8765
#
# Usage:  bash install_service.sh        (install + start)
#         bash install_service.sh remove (stop + uninstall)
#
# The units run as your user and import the graphical-session environment so
# the app can talk to Plasma (kscreen/qdbus). On Bazzite the home folder is
# /var/home/<you>; %h resolves it either way.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PYTHON="$(command -v python3)"

if [[ -z "$PYTHON" ]]; then
  echo "python3 not found in PATH" >&2
  exit 1
fi

remove() {
  systemctl --user disable --now steam-theme.service steam-theme-ui.service 2>/dev/null || true
  rm -f "$UNIT_DIR/steam-theme.service" "$UNIT_DIR/steam-theme-ui.service"
  systemctl --user daemon-reload
  echo "removed."
}

if [[ "${1:-}" == "remove" ]]; then
  remove
  exit 0
fi

mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/steam-theme.service" <<EOF
[Unit]
Description=Steam Theme — last-played-game desktop theme
After=graphical-session.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$PYTHON $APP_DIR/main.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/steam-theme-ui.service" <<EOF
[Unit]
Description=Steam Theme — config page (http://127.0.0.1:8765)
After=graphical-session.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$PYTHON $APP_DIR/main.py --ui
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now steam-theme.service steam-theme-ui.service

echo
echo "Installed and started:"
systemctl --user --no-pager --plain status steam-theme.service | head -3 || true
systemctl --user --no-pager --plain status steam-theme-ui.service | head -3 || true
echo
echo "Config page: http://127.0.0.1:8765"
echo
echo "Optional: to keep the services running even when you're not logged in,"
echo "enable lingering:   sudo loginctl enable-linger \$USER"
echo "(On Bazzite this is already enabled for the default user on most images.)"
