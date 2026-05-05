#!/bin/sh
set -eu

SERVICE_NAME="waveforward-daemon"
WAVEFORWARD_BIN="${WAVEFORWARD_BIN:-$HOME/.local/bin/waveforward}"
WORKSPACE=""
SERVER=""
TOKEN=""
MACHINE=""
POLL_INTERVAL="2.0"

usage() {
  cat <<'USAGE'
Usage: scripts/install-daemon-service.sh --workspace DIR --server URL --token TOKEN [options]

Install and start a systemd user service for the WaveForward daemon.

Options:
  --workspace DIR       Workspace where agent turns should run.
  --server URL          WaveForward service URL.
  --token TOKEN         Setup token from Settings -> Machines.
  --machine NAME        Human-readable machine name.
  --poll-interval SEC   Poll interval. Defaults to 2.0.
  --name NAME           systemd service name. Defaults to waveforward-daemon.
  --waveforward PATH    WaveForward executable. Defaults to ~/.local/bin/waveforward.
  -h, --help            Show this help.

The setup token is stored in a 0600 env file. After first registration, the
daemon stores its long-lived machine token in WORKSPACE/.waveforward/daemon.json.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --workspace)
      WORKSPACE="$2"
      shift 2
      ;;
    --server)
      SERVER="$2"
      shift 2
      ;;
    --token)
      TOKEN="$2"
      shift 2
      ;;
    --machine)
      MACHINE="$2"
      shift 2
      ;;
    --poll-interval)
      POLL_INTERVAL="$2"
      shift 2
      ;;
    --name)
      SERVICE_NAME="$2"
      shift 2
      ;;
    --waveforward)
      WAVEFORWARD_BIN="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ -z "$WORKSPACE" ] || [ -z "$SERVER" ] || [ -z "$TOKEN" ]; then
  echo "error: --workspace, --server, and --token are required" >&2
  usage >&2
  exit 2
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "error: systemctl is required for systemd user services" >&2
  exit 1
fi

WORKSPACE="$(CDPATH= cd -- "$WORKSPACE" && pwd)"
SERVICE_DIR="$HOME/.config/systemd/user"
ENV_DIR="$HOME/.config/waveforward"
SERVICE_FILE="$SERVICE_DIR/$SERVICE_NAME.service"
ENV_FILE="$ENV_DIR/$SERVICE_NAME.env"

mkdir -p "$SERVICE_DIR" "$ENV_DIR"
umask 077
{
  printf 'WAVEFORWARD_DAEMON_SERVER=%s\n' "$SERVER"
  printf 'WAVEFORWARD_DAEMON_TOKEN=%s\n' "$TOKEN"
  printf 'WAVEFORWARD_DAEMON_INTERVAL=%s\n' "$POLL_INTERVAL"
  if [ -n "$MACHINE" ]; then
    printf 'WAVEFORWARD_DAEMON_MACHINE=%s\n' "$MACHINE"
  fi
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=WaveForward daemon
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$WORKSPACE
EnvironmentFile=$ENV_FILE
ExecStart=$WAVEFORWARD_BIN daemon
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME.service"

echo "Installed and started $SERVICE_NAME.service"
echo "Status: systemctl --user status $SERVICE_NAME.service"
