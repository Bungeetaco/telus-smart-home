#!/bin/bash
# bash (not /bin/sh -> dash) is required for `wait -n` below, which is what
# actually supervises both child processes -- previously this only ever
# waited on the bridge, so a MediaMTX crash/OOM went unnoticed here (the
# bridge would eventually cascade into repeated publish failures and hit its
# own MAX_CONSECUTIVE_SESSION_FAILURES restart, but only after several failed
# sessions -- this makes MediaMTX dying trigger an immediate, direct restart
# of the whole container instead of relying on that indirect path).
# NOT `set -e`: with errexit on, a non-zero exit from `wait -n` below aborts the
# script immediately, making every line after it -- the diagnostic "which child
# died" message and the cleanup kill -- unreachable dead code. That is exactly the
# path the bridge's own MAX_CONSECUTIVE_SESSION_FAILURES escape hatch takes
# (os._exit(1)), so the one failure mode most in need of a clear log line was the
# one guaranteed not to produce it. Errors are handled explicitly instead.
set -uo pipefail

# RTSP auth. MediaMTX's default is a single anonymous user with publish+read on
# every path, so before this any local process (or any other host-networked
# container -- this one runs with --network host) could read the security-camera
# feeds off 127.0.0.1:8554, or publish to the same well-known path names
# (config.rtsp_path_for is a pure function of the camera id) and replace the live
# feed. The generated password is shared with the Python bridge through the same
# /shared mount already used for the control secret, and gets embedded in the RTSP
# URLs handed to Home Assistant via /control/cameras -- so nothing needs to be
# configured by hand on either side.
RTSP_PASS_FILE=/shared/rtsp_password
if [ ! -s "$RTSP_PASS_FILE" ]; then
  python3 -c 'import secrets; print(secrets.token_hex(16))' > "$RTSP_PASS_FILE"
  chmod 600 "$RTSP_PASS_FILE"
  echo "entrypoint.sh: generated a new RTSP password at $RTSP_PASS_FILE" >&2
fi
RTSP_PASSWORD="$(cat "$RTSP_PASS_FILE")"
export RTSP_PASSWORD

# mediamtx.yml is baked into the image read-only and MediaMTX does not interpolate
# environment variables inside it, so render a runtime copy with the password
# substituted in. /tmp is container-local, never the bind mount.
RUNTIME_CONF=/tmp/mediamtx.runtime.yml
sed "s|__RTSP_PASSWORD__|${RTSP_PASSWORD}|" /app/mediamtx.yml > "$RUNTIME_CONF"

/usr/local/bin/mediamtx "$RUNTIME_CONF" &
MEDIAMTX_PID=$!

# wait for MediaMTX's RTSP listener before starting the bridge
for i in $(seq 1 30); do
  if (echo > /dev/tcp/127.0.0.1/${MEDIAMTX_RTSP_PORT:-8554}) 2>/dev/null; then break; fi
  sleep 0.5
done

# jemalloc, for the PYTHON process only. glibc's arenas fragment under the
# bridge's mixed allocation sizes (3.11 MB frame buffers at 15 fps beside ~15/s
# small audio allocations) until RSS grows without bound while the in-use heap
# stays flat -- measured, not inferred; see backlog E83 and the Dockerfile note.
# Deliberately NOT exported globally: MediaMTX is Go (its runtime never touches
# glibc malloc for the heap) and ffmpeg has shown no growth (~127 MB flat), so
# scoping the preload to the one process with the pathology keeps the change
# auditable and the blast radius minimal. If the .so is ever missing the
# preload degrades to glibc with a warning on stderr rather than failing.
JEMALLOC_SO=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2
if [ -e "$JEMALLOC_SO" ]; then
  LD_PRELOAD="$JEMALLOC_SO" python3 -m bridge.main &
else
  echo "entrypoint.sh: $JEMALLOC_SO not found, running the bridge on glibc malloc (E83 fragmentation will return)" >&2
  python3 -m bridge.main &
fi
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
