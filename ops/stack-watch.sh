#!/bin/bash
# Whole-stack watch for the TELUS smart-home deployment.
# One "ok"/"FAIL" line per run, appended. To read it: grep FAIL first, then skim.
# Installed from a Claude Code session on 2026-08-25 to validate the E60/E62
# audio-pacing fix overnight; it is not audio-specific.
LOG=/home/bungee/telus-stack-watch.log
STATE=/home/bungee/.telus-stack-watch.state
TS=$(date -Is)

emit() { echo "$TS $*" >> "$LOG"; }

# --- containers -------------------------------------------------------------
CSTAT=""
for c in telus-cam-bridge homeassistant; do
  running=$(sudo -n docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)
  restarts=$(sudo -n docker inspect -f '{{.RestartCount}}' "$c" 2>/dev/null)
  started=$(sudo -n docker inspect -f '{{.State.StartedAt}}' "$c" 2>/dev/null | cut -c1-19)
  if [ "$running" != "true" ]; then
    emit "FAIL container-down $c running=$running"
  fi
  CSTAT="$CSTAT ${c%%-*}=${running:-?}/r${restarts:-?}/$started"
done

# --- bridge memory against its 768MiB limit ---------------------------------
# Threshold is 93%, not 85%. Measured 2026-08-25: this workload plateaus at
# 83-90% of the 768MiB limit and stays there. Over 30 minutes and ~11 session
# rotations the python process held flat at 119 threads and 485-493MB RSS, with
# no OOM kill and no restart in 6 hours. 85% was reporting the normal operating
# range as a failure. Anything above 93% is genuinely close to the limit.
MEM=$(sudo -n docker stats --no-stream --format '{{.MemPerc}}' telus-cam-bridge 2>/dev/null | tr -d ' %')
case "$MEM" in
  ''|*[!0-9.]*) MEM="?" ;;
  *) awk -v m="$MEM" 'BEGIN{exit !(m>93)}' && emit "FAIL bridge-memory-high ${MEM}%" ;;
esac

# --- bridge control status --------------------------------------------------
S=$(sudo -n cat /home/bungee/homeassistant/share/telus_cam_bridge/control_secret 2>/dev/null | tr -d '\r\n')
J=$(curl -s --max-time 40 -H "Authorization: Bearer $S" http://127.0.0.1:8199/control/status 2>/dev/null)
if [ -z "$J" ]; then
  emit "FAIL bridge-unreachable containers:$CSTAT mem=${MEM}%"
else
  echo "$J" | STATE="$STATE" TS="$TS" CSTAT="$CSTAT" MEM="$MEM" LOG="$LOG" python3 -c '
import json,os,sys
ts=os.environ["TS"]; log=open(os.environ["LOG"],"a")
state_path=os.environ["STATE"]
try: d=json.load(sys.stdin)
except Exception as e:
    log.write("%s FAIL bridge-bad-json %s\n"%(ts,e)); raise SystemExit
if d.get("error"):
    log.write("%s FAIL bridge-error %s\n"%(ts,d["error"])); raise SystemExit
cams=d.get("cameras") or {}
if not cams:
    log.write("%s FAIL bridge-no-cameras\n"%ts); raise SystemExit
try: prev=json.load(open(state_path))
except Exception: prev={}
cur={}; bad=[]; parts=[]
for cid,c in sorted(cams.items()):
    short=cid[-4:]
    st=c.get("state"); drops=c.get("dropped_frames") or 0
    aud=c.get("decoded_audio_frames") or 0; adrop=c.get("audio_dropped")
    alive=c.get("publisher_alive"); fails=c.get("consecutive_failures") or 0
    lag=((c.get("last_frame_at") or 0)-(c.get("last_publish_at") or 0))*1000
    p=prev.get(short) or {}
    # Counters reset when the bridge restarts; a decrease is a reset, not progress.
    ddrop = drops-p["drops"] if "drops" in p and drops>=p["drops"] else None
    daud  = aud-p["aud"]     if "aud"   in p and aud  >=p["aud"]   else None
    cur[short]={"drops":drops,"aud":aud}
    parts.append("%s st=%s fps=%s drop=%s(+%s) aud=%s(+%s) alive=%s lag=%.0fms"%(
        short,st,c.get("fps"),drops,ddrop,aud,daud,alive,lag))
    if st!="streaming": bad.append("%s:state=%s"%(short,st))
    if alive is False:  bad.append("%s:publisher-dead"%short)
    if adrop:           bad.append("%s:audio-pipe-dropped(%s)"%(short,c.get("audio_dropped_reason")))
    if fails:           bad.append("%s:consecutive_failures=%s"%(short,fails))
    if c.get("last_error"): bad.append("%s:last_error=%s"%(short,c["last_error"]))
    # A publisher that stopped being fed shows as a growing frame/publish gap.
    if lag > 2000:      bad.append("%s:publish-lag=%.0fms"%(short,lag))
    # Audio was asked for on this camera but the counter has not moved.
    if c.get("audio_consumed") and daud==0: bad.append("%s:audio-not-advancing"%short)
talk=d.get("talk") or {}
if talk.get("failed"): bad.append("talk_failed=%s(%s)"%(talk["failed"],talk.get("last_failure_reason")))
try: json.dump(cur,open(state_path,"w"))
except Exception: pass
head = "FAIL "+",".join(bad) if bad else "ok"
log.write("%s %s %s || containers:%s mem=%s%% talk=%s/%s\n"%(
    ts,head," || ".join(parts),os.environ["CSTAT"],os.environ["MEM"],
    talk.get("succeeded"),talk.get("attempts")))
log.close()
'
fi

# --- Home Assistant: integration errors and unavailable entities ------------
ERRS=$(sudo -n docker logs homeassistant --since 5m 2>&1 | grep -ci "telus_smarthome_alarmdotcom.*\(error\|traceback\|exception\)" || true)
[ "${ERRS:-0}" -gt 0 ] && emit "FAIL ha-integration-errors count=$ERRS (docker logs homeassistant --since 10m | grep -i telus)"

# Name the entities that matter rather than counting: this account has ~15
# permanently-unavailable entities by design (the privacy switches, because no
# camera here is privacy-capable), so a count threshold is noise.
DEAD=$(timeout 45 sudo -n docker exec homeassistant python3 -c "
import sqlite3
WATCH=('sensor.doorbell_stream_state','sensor.living_room_stream_state',
       'sensor.doorbell_audio_state','sensor.living_room_audio_state',
       'sensor.doorbell_dropped_frames','sensor.living_room_dropped_frames',
       'binary_sensor.telus_smart_home_integration_camera_bridge',
       'camera.doorbell','camera.living_room')
# event.doorbell_button_press is deliberately NOT watched: an EventEntity reads
# 'unknown' until its first event, which is the normal resting state here.
c=sqlite3.connect('file:/config/home-assistant_v2.db?mode=ro',uri=True)
q='''SELECT sm.entity_id,s.state FROM states s JOIN states_meta sm ON s.metadata_id=sm.metadata_id
WHERE s.state_id IN (SELECT MAX(state_id) FROM states GROUP BY metadata_id)
AND sm.entity_id IN (%s)'''%','.join('?'*len(WATCH))
rows=dict(c.execute(q,WATCH))
# 'unknown' is an honest, documented state for the audio sensors: the first
# poll of a camera that already has frames has nothing to compare against. Only
# 'unavailable' or a missing entity is a fault there. Everything else treats
# 'unknown' as a fault, and a fresh HA restart can show it for one sample.
SOFT=('sensor.doorbell_audio_state','sensor.living_room_audio_state')
bad=[k+'='+(rows.get(k) or 'MISSING') for k in WATCH
     if rows.get(k) in (None,'unavailable') or
        (rows.get(k)=='unknown' and k not in SOFT)]
print(','.join(bad))
" 2>/dev/null)
[ -n "$DEAD" ] && emit "FAIL ha-key-entities $DEAD"

# keep the file bounded (~2 weeks at 5-minute samples)
tail -n 5000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
