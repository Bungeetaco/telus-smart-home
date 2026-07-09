#!/bin/sh
# Invoked by MediaMTX (see mediamtx.yml's runOnUnDemand) once a camera's RTSP path has
# had zero readers for runOnDemandCloseAfter. Tells the bridge process to stop that
# camera's pipeline (cancels its run_camera_forever task) -- see activate_camera.sh for
# the corresponding startup half of this.
set -eu

CONTROL_URL="http://127.0.0.1:${CONTROL_PORT:-8199}"
SECRET="$(cat "${SHARED_DIR:-/shared}/control_secret")"

curl -s -X POST -H "Authorization: Bearer $SECRET" \
  "$CONTROL_URL/control/cameras/by-path/$MTX_PATH/deactivate" >/dev/null
