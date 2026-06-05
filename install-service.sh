#!/bin/bash
# Install rotator-remote as a systemd service and start it.
# Run as: sudo ./install-service.sh
set -e

if [ "$EUID" -ne 0 ]; then
    echo "This script must be run with sudo:"
    echo "  sudo ./install-service.sh"
    exit 1
fi

WORK_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="${SUDO_USER:-$(logname 2>/dev/null || echo "")}"

if [ -z "$USER_NAME" ] || [ "$USER_NAME" = "root" ]; then
    echo "Could not determine the regular user to run as."
    echo "Re-run with sudo from a normal shell, e.g.:"
    echo "  sudo ./install-service.sh"
    exit 1
fi

if [ ! -x "$WORK_DIR/venv/bin/python" ]; then
    echo "Virtual environment not found at $WORK_DIR/venv"
    echo "Run ./setup.sh first to create it."
    exit 1
fi

if ! id "$USER_NAME" >/dev/null 2>&1; then
    echo "User '$USER_NAME' does not exist."
    exit 1
fi

# Make sure the user can read/write the serial port (best effort).
if id "$USER_NAME" | grep -qv "dialout"; then
    echo "Note: user $USER_NAME is not in the 'dialout' group."
    echo "      Adding it now (takes effect after next login)."
    usermod -aG dialout "$USER_NAME"
fi

SERVICE_NAME="rotator-remote"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
TEMPLATE="$WORK_DIR/systemd/rotator-remote.service.template"

if [ ! -f "$TEMPLATE" ]; then
    echo "Service template not found at $TEMPLATE"
    exit 1
fi

echo "Installing $SERVICE_NAME service:"
echo "  User:           $USER_NAME"
echo "  Working dir:    $WORK_DIR"
echo "  Service file:   $SERVICE_FILE"
echo

# Stop the running service if we're re-installing.
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo "Stopping existing $SERVICE_NAME..."
    systemctl stop "$SERVICE_NAME"
fi

# Render the template.
sed \
    -e "s|{{USER}}|${USER_NAME}|g" \
    -e "s|{{WORKDIR}}|${WORK_DIR}|g" \
    "$TEMPLATE" >"$SERVICE_FILE"

chmod 644 "$SERVICE_FILE"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

# Brief pause then status check.
sleep 1
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo
    echo "Service is running."
else
    echo
    echo "WARNING: service did not start cleanly. Check:"
    echo "  sudo systemctl status $SERVICE_NAME"
    echo "  sudo journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi

cat <<EOF

Useful commands:
  sudo systemctl status $SERVICE_NAME
  sudo systemctl restart $SERVICE_NAME
  sudo systemctl stop $SERVICE_NAME
  sudo journalctl -u $SERVICE_NAME -f       # live logs
  sudo journalctl -u $SERVICE_NAME -n 100   # last 100 lines

Health check:  curl http://$(hostname -I | awk '{print $1}'):8090/healthz
WebSocket:     ws://$(hostname -I | awk '{print $1}'):8090/ws

To remove the service later:
  sudo ./uninstall-service.sh
EOF
