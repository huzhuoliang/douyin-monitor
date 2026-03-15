#!/usr/bin/env bash
# Douyin Monitor Installer
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.json"
MONITOR_SCRIPT="$SCRIPT_DIR/monitor.py"
SERVICE_FILE="/etc/systemd/system/douyin-monitor.service"
RECORDINGS_DIR="${RECORDINGS_DIR:-$HOME/recordings}"
SERVICE_USER="${SERVICE_USER:-$(whoami)}"

echo "=== Douyin Monitor Installer ==="

# --- Validate config --------------------------------------------------------
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: config.json not found at $CONFIG_FILE"
    exit 1
fi

if python3 -c "
import json, sys
cfg = json.load(open('$CONFIG_FILE'))
url = cfg.get('streamer_url', '')
if 'REPLACE_WITH_ROOM_ID' in url:
    print('ERROR: streamer_url still contains placeholder. Edit config.json first.')
    sys.exit(1)
if not url.startswith('https://'):
    print('ERROR: streamer_url does not look like a valid URL.')
    sys.exit(1)
print('Config OK: ' + url)
"; then
    echo ""
else
    exit 1
fi

# --- Install ffmpeg ----------------------------------------------------------
echo ""
echo "[1/4] Installing ffmpeg..."
sudo apt-get install -y ffmpeg
echo "ffmpeg installed: $(ffmpeg -version 2>&1 | head -1)"

# --- Install streamlink -------------------------------------------------------
echo ""
echo "[2/4] Installing streamlink..."
sudo pip3 install -U streamlink --break-system-packages
echo "streamlink installed: $(streamlink --version)"

# --- Create recordings dir ---------------------------------------------------
echo ""
echo "[3/4] Creating recordings directory: $RECORDINGS_DIR"
mkdir -p "$RECORDINGS_DIR"

# --- Write systemd service ---------------------------------------------------
echo ""
echo "[4/4] Writing systemd service to $SERVICE_FILE..."
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Douyin Live Stream Monitor & Auto-Recorder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=/usr/bin/python3 $MONITOR_SCRIPT --config $CONFIG_FILE
Restart=always
RestartSec=10
KillMode=mixed
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
echo "Service file written."

# --- Enable and start service ------------------------------------------------
echo ""
echo "Enabling and starting douyin-monitor service..."
sudo systemctl daemon-reload
sudo systemctl enable douyin-monitor
sudo systemctl start douyin-monitor

echo ""
echo "=== Installation complete ==="
echo ""
echo "Check status:   sudo systemctl status douyin-monitor"
echo "Follow logs:    sudo journalctl -u douyin-monitor -f"
echo "Stop service:   sudo systemctl stop douyin-monitor"
echo "Recordings dir: $RECORDINGS_DIR"
