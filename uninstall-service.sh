#!/bin/bash
# Remove the rotator-remote systemd service.
set -e

if [ "$EUID" -ne 0 ]; then
    echo "Run with sudo: sudo ./uninstall-service.sh"
    exit 1
fi

SERVICE_NAME="rotator-remote"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo "Stopping $SERVICE_NAME..."
    systemctl stop "$SERVICE_NAME"
fi

if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME"
fi

if [ -f "$SERVICE_FILE" ]; then
    rm "$SERVICE_FILE"
    echo "Removed $SERVICE_FILE"
fi

systemctl daemon-reload
systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true

echo "Service removed. (Node-RED can take the serial port back if you revert the flow.)"
