#!/usr/bin/env bash
# Interactive installer for TELUS Smart Home (Home Assistant integration +
# camera bridge). Requires: bash, docker with the compose plugin, and sudo
# access (for mkdir/chown on the shared directory and running docker compose).
#
# Safe to re-run -- existing secrets.env / already-copied integration files
# are left alone rather than overwritten.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_UID=1500

echo "TELUS Smart Home -- installer"
echo "=============================="
echo

# --- Step 1: pre-flight network check -----------------------------------
# Both the Home Assistant container and the camera bridge container must run
# with --network host. Janus/WebRTC negotiates a wide ephemeral UDP port
# range for RTP that doesn't play well with Docker's default bridge
# networking + NAT, and the bridge's credential-sync endpoint is loopback-only
# by design, reachable from HA's container only because they share a network
# namespace. There's no way around this short of a bigger networking redesign
# neither container's software supports; if this check fails, this installer
# stops rather than proceeding into a setup that silently won't work.
echo "[1/5] Checking network requirements..."
HA_CONTAINER=""
for candidate in homeassistant home-assistant hass; do
  if sudo docker inspect "$candidate" >/dev/null 2>&1; then
    HA_CONTAINER="$candidate"
    break
  fi
done

if [ -z "$HA_CONTAINER" ]; then
  echo "  WARNING: couldn't find a Home Assistant container (tried: homeassistant," \
       "home-assistant, hass)."
  echo "  This project REQUIRES Home Assistant's own container to run with"
  echo "  --network host, or the camera bridge's credential-sync endpoint won't be"
  echo "  reachable from it. Verify this yourself (sudo docker inspect <your HA container>"
  echo "  --format '{{.HostConfig.NetworkMode}}' should print 'host') before continuing."
  read -r -p "  Continue anyway? [y/N] " REPLY
  [[ "$REPLY" =~ ^[Yy]$ ]] || exit 1
else
  NETMODE=$(sudo docker inspect "$HA_CONTAINER" --format '{{.HostConfig.NetworkMode}}')
  if [ "$NETMODE" != "host" ]; then
    echo "  ERROR: '$HA_CONTAINER' is running with network mode '$NETMODE', not 'host'."
    echo "  This project requires --network host on BOTH the Home Assistant container"
    echo "  and the camera bridge container (see README's 'Network requirement'"
    echo "  section) -- without it, the credential-sync endpoint at 127.0.0.1:8199"
    echo "  won't be reachable between them. This is a Docker networking constraint,"
    echo "  not something this script can work around for you."
    exit 1
  fi
  echo "  OK: '$HA_CONTAINER' is running with --network host."
fi
echo

# --- Step 2: Home Assistant integration ----------------------------------
echo "[2/5] Home Assistant integration"
echo "  If you're installing via HACS (add this repo as a custom repository,"
echo "  category 'Integration'), skip this step -- HACS handles the file copy."
read -r -p "  Copy the integration into your HA config directory now instead? [y/N] " REPLY
if [[ "$REPLY" =~ ^[Yy]$ ]]; then
  read -r -p "  Path to your Home Assistant config directory (the one containing custom_components/): " HA_CONFIG_DIR
  if [ ! -d "$HA_CONFIG_DIR" ]; then
    echo "  ERROR: $HA_CONFIG_DIR does not exist."
    exit 1
  fi
  mkdir -p "$HA_CONFIG_DIR/custom_components"
  cp -r "$SCRIPT_DIR/custom_components/telus_smarthome" "$HA_CONFIG_DIR/custom_components/"
  echo "  Copied. Restart Home Assistant, then add the integration via Settings >"
  echo "  Devices & Services > Add Integration > TELUS Smart Home."
fi
echo

# --- Step 3: camera bridge credentials -----------------------------------
echo "[3/5] Camera bridge credentials"
cd "$SCRIPT_DIR/camera-bridge"
if [ ! -f secrets.env ]; then
  read -r -p "  TELUS account email: " INPUT_EMAIL
  read -r -s -p "  TELUS account password: " INPUT_PASSWORD
  echo
  # Docker Compose interpolates $-references inside env_file values (this is
  # real, documented Compose behavior, not specific to a filename) -- a
  # password containing a literal "$" gets silently corrupted unless escaped
  # as "$$" here. Verified live during development: an unescaped "$" in a
  # real password reached the container mangled; this escaping is what fixes
  # it, not just defensive paranoia.
  ESCAPED_PASSWORD="${INPUT_PASSWORD//\$/\$\$}"
  printf 'TELUS_EMAIL=%s\nTELUS_PASSWORD=%s\n' "$INPUT_EMAIL" "$ESCAPED_PASSWORD" > secrets.env
  chmod 600 secrets.env
  echo "  Wrote secrets.env (mode 600)."
else
  echo "  secrets.env already exists, leaving it as-is."
fi
echo

# --- Step 4: shared directory ---------------------------------------------
echo "[4/5] Shared directory (credential sync with Home Assistant)"
DEFAULT_SHARED_DIR="/var/lib/homeassistant/share/telus_cam_bridge"
read -r -p "  Host path for the shared directory [$DEFAULT_SHARED_DIR]: " INPUT_DIR
HOST_SHARED_DIR="${INPUT_DIR:-$DEFAULT_SHARED_DIR}"
sudo mkdir -p "$HOST_SHARED_DIR"
sudo chown -R "$BRIDGE_UID:$BRIDGE_UID" "$HOST_SHARED_DIR"
if ! grep -q '^HOST_SHARED_DIR=' .env 2>/dev/null; then
  echo "HOST_SHARED_DIR=$HOST_SHARED_DIR" >> .env
fi
echo "  Prepared $HOST_SHARED_DIR (owned by the bridge's non-root container user,"
echo "  uid $BRIDGE_UID)."
echo

# --- Step 5: build and start -----------------------------------------------
echo "[5/5] Building and starting the camera bridge..."
sudo docker compose up -d --build
echo
echo "Done."
echo "  Logs:    sudo docker compose -f $SCRIPT_DIR/camera-bridge/docker-compose.yml logs -f"
echo "  Once you see 'webrtcup' for each camera, add it in Home Assistant as a"
echo "  Generic Camera entity using rtsp://127.0.0.1:8554/<camera-path>"
echo "  (see camera-bridge/README.md for exact path names and next steps)."
