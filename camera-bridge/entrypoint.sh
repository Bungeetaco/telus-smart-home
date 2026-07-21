#!/bin/bash
# bash (not /bin/sh -> dash) is required for `wait -n` below, which is what
# actually supervises both child processes -- previously this only ever
# waited on the bridge, so a MediaMTX crash/OOM went unnoticed here (the
# bridge would eventually cascade into repeated publish failures and hit its
# own MAX_CONSECUTIVE_SESSION_FAILURES restart, but only after several failed
# sessions -- this makes MediaMTX dying trigger an immediate, direct restart
# of the whole container instead of relying on that indirect path).
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

trap 'kill $MEDIAMTX_PID $BRIDGE_PID 2>/dev/null' TERM INT

# Block until EITHER process exits, not just the bridge -- whichever dies
# first, take the whole container down (`restart: unless-stopped` in
# docker-compose.yml brings both back up clean together) rather than leaving
# a half-alive container running just the survivor.
wait -n "$MEDIAMTX_PID" "$BRIDGE_PID"
EXIT_CODE=$?
if ! kill -0 "$MEDIAMTX_PID" 2>/dev/null; then
  echo "entrypoint.sh: mediamtx exited (code $EXIT_CODE), stopping bridge and restarting container" >&2
else
  echo "entrypoint.sh: bridge exited (code $EXIT_CODE), stopping mediamtx and restarting container" >&2
fi
kill $MEDIAMTX_PID $BRIDGE_PID 2>/dev/null
wait $MEDIAMTX_PID $BRIDGE_PID 2>/dev/null
exit "$EXIT_CODE"
