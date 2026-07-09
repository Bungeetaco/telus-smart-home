#!/bin/sh
# Invoked by MediaMTX (see mediamtx.yml's runOnDemand) whenever a reader connects to a
# camera's RTSP path and no publisher is active yet. $MTX_PATH (set by MediaMTX in this
# process's environment) is the path name, e.g. telus_<camera_id-with-dashes-as-
# underscores> -- see bridge/config.py's rtsp_path_for().
#
# This script does NOT publish anything itself -- it just asks the already-running
# bridge process (which owns the one shared TELUS session, camera registry, etc.) to
# start that camera's pipeline via the local control endpoint. The actual RTSP publish
# connection that satisfies the waiting reader comes from that process's own
# ffmpeg_sink.py, moments later.
#
# MediaMTX expects the runOnDemand command's process to represent the on-demand
# source's lifetime (it SIGINTs this process when torn down), but we don't rely on
# catching that signal -- teardown is driven by the separate runOnUnDemand hook
# (deactivate_camera.sh), which MediaMTX guarantees to run once readers have been gone
# for runOnDemandCloseAfter. This process just needs to stay alive until then.
set -eu

CONTROL_URL="http://127.0.0.1:${CONTROL_PORT:-8199}"
SECRET="$(cat "${SHARED_DIR:-/shared}/control_secret")"

curl -s -X POST -H "Authorization: Bearer $SECRET" \
  "$CONTROL_URL/control/cameras/by-path/$MTX_PATH/activate" >/dev/null

# `exec` replaces this shell's process image with `sleep` itself, so the signal
# MediaMTX sends to this PID on teardown (see runOnUnDemand) lands directly on
# sleep -- which terminates immediately on SIGINT/SIGTERM by default. The
# previous `sh -c 'while true; do sleep 3600 & wait $!; done'` backgrounded the
# sleep as a *child*; a signal delivered to the parent shell (not the process
# group) killed the shell but left the background sleep running as an orphan,
# reparented to PID 1, for up to the remaining hour -- confirmed live via
# `docker top` showing 17 stray `sleep 3600` processes after 11 hours of normal
# dashboard use. GNU coreutils' `sleep infinity` (confirmed present in this
# image's Debian trixie base) sleeps far longer than any realistic container
# uptime, so there's no need for a loop at all.
exec sleep infinity
