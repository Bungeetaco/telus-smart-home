#!/bin/sh
set -e
/usr/local/bin/mediamtx /app/mediamtx.yml &
MEDIAMTX_PID=$!

# wait for MediaMTX's RTSP listener before starting the bridge
for i in $(seq 1 30); do
  if (echo > /dev/tcp/127.0.0.1/${MEDIAMTX_RTSP_PORT:-8554}) 2>/dev/null; then break; fi
  sleep 0.5
done

python3 -m bridge.main &
BRIDGE_PID=$!

trap "kill $MEDIAMTX_PID $BRIDGE_PID 2>/dev/null" TERM INT
wait $BRIDGE_PID
kill $MEDIAMTX_PID 2>/dev/null
