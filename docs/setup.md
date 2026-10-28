# Installation and Deployment

This guide walks through deploying TELUS Smart Home for Home Assistant from
nothing to working panel, lock, sensor, and live-camera entities. There are two
pieces to install:

1. **The Home Assistant integration** (`custom_components/telus_smarthome_alarmdotcom/`):
   provides the alarm panel, lock, sensors, camera entities, and the config-flow
   UI. Runs inside Home Assistant.
2. **The camera bridge** (`camera-bridge/`): a standalone Docker container that
   logs into the TELUS video platform, pulls WebRTC/Janus camera video, and
   re-serves it as authenticated RTSP for Home Assistant to consume.

The two are linked by a shared host directory and a loopback control endpoint,
which is why the network topology below is a hard requirement rather than a
preference. Read [Prerequisites](#prerequisites) (especially the network
requirement) before you install anything.

For every tunable referenced here (environment variables, integration options,
credential-sync internals), see [configuration.md](configuration.md).

## Contents

- [Prerequisites](#prerequisites)
- [The `--network host` requirement (and why)](#the---network-host-requirement-and-why)
- [Step 1: Install the Home Assistant integration](#step-1-install-the-home-assistant-integration)
- [Step 2: Add the integration in the UI](#step-2-add-the-integration-in-the-ui)
- [Step 3: Deploy the camera bridge](#step-3-deploy-the-camera-bridge)
- [Step 4: Confirm the shared directory is mounted by both containers](#step-4-confirm-the-shared-directory-is-mounted-by-both-containers)
- [How credential sync ties the two together](#how-credential-sync-ties-the-two-together)
- [Optional: Talkdown (two-way / speak-to-camera audio)](#optional-talkdown-two-way--speak-to-camera-audio)
- [Verifying each piece](#verifying-each-piece)
- [The all-in-one installer (`install.sh`)](#the-all-in-one-installer-installsh)

## Prerequisites

| Requirement | Detail |
| --- | --- |
| **Home Assistant** | 2026.8.0 or newer, running in a **container** that uses `--network host` (see below). The integration installs into HA's `custom_components/` directory. |
| **Docker + Compose plugin** | The camera bridge is a Docker container built and run with `docker compose`. The host needs Docker with the `compose` plugin (`docker compose`, not the legacy `docker-compose`). |
| **A GPU render node (recommended)** | The bridge encodes H.264 in hardware by default. On Intel (QuickSync) / AMD hosts this is VAAPI via `/dev/dri/renderD128`; the container's non-root user needs membership in the host `render` group. NVIDIA is supported via `nvidia-container-toolkit` but is unverified against real NVIDIA hardware. With no usable GPU the bridge automatically falls back to software `libx264`: it still works, just at higher CPU cost. See [configuration.md](configuration.md) for `ENCODE_BACKEND`, `VAAPI_DEVICE`, and `RENDER_GID`. |
| **A TELUS Smart Home account** | Username/password. Note the bridge's login is username/password **only**: it has no MFA/2FA support (the HA integration does). If MFA is enabled on the account, camera streaming breaks independently of the panel/sensor entities. |
| **`sudo` on the host** | Needed to create and `chown` the shared directory and to run `docker compose`. |

## The `--network host` requirement (and why)

**Both the Home Assistant container and the camera bridge container must run
with `--network host`.** This is a hard requirement, not a suggestion. Two
independent reasons:

1. **Janus/WebRTC RTP ports.** Pulling camera video negotiates a wide range of
   ephemeral UDP ports for RTP. That range does not work through Docker's
   default bridge networking and NAT.
2. **The credential-sync control endpoint is loopback-only.** The bridge exposes
   its control endpoint on `127.0.0.1:8199` and never off the host. Home
   Assistant can reach it *only* because both containers share the host's
   network namespace: under host networking, the bridge's `127.0.0.1:8199`
   is the host's own loopback, which HA (also host-networked) shares directly.
   No port-publish flags are involved or possible.

A default HA Container / Docker install typically uses bridge networking with
port mapping instead, and the camera bridge will not work against it as-is.
There is no workaround short of a networking redesign that neither component
supports.

You can verify HA's network mode before starting:

```bash
sudo docker inspect <ha-container-name> --format '{{.HostConfig.NetworkMode}}'
# must print: host
```

`install.sh` performs this same check up front and refuses to continue if it
fails, rather than letting you discover a silently-broken setup halfway through.

## Step 1: Install the Home Assistant integration

Choose **one** of the two methods.

### Method A: HACS custom repository (recommended)

1. In Home Assistant, open **HACS → ⋮ (top-right) → Custom repositories**.
2. Add this repository's URL with category **Integration**.
3. Install **"TELUS Smart Home"** from HACS like any other integration.
4. Restart Home Assistant when prompted.

HACS handles the file copy and gives you an update mechanism against this repo's
own git tags. Note this integration is a permanent hand-maintained fork: it is
**not** in the default HACS store and does not sync from any upstream package.

### Method B: Manual copy

Copy the integration directory into your HA config's `custom_components/`:

```bash
cp -r <repo>/custom_components/telus_smarthome_alarmdotcom \
      <ha-config-dir>/custom_components/
```

Then restart Home Assistant.

`<ha-config-dir>` is the directory that contains `configuration.yaml` and a
`custom_components/` folder. The bundled `install.sh` can do this copy for you
if you prefer; see [The all-in-one installer](#the-all-in-one-installer-installsh).

## Step 2: Add the integration in the UI

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for and select **TELUS Smart Home**.
3. Complete the config flow:
   - **`user` step**: enter your TELUS email and password. The integration
     performs TELUS's SSO handoff before falling into the normal Alarm.com
     session code. A failure in that handoff surfaces as `telus_sso_error`
     (distinct from `invalid_auth`, which means a plain wrong-password
     response).
   - **2FA step (if enabled)**: the flow supports selecting an OTP method
     (`otp_select_method`) and submitting the code (`otp_submit`).
4. On success the panel, lock, and sensor entities appear immediately. Camera
   entities appear once the bridge is running (Step 3) and the shared directory
   is wired up (Step 4).

Later credential changes should be done through the integration's **Reconfigure**
flow (Settings → Devices & Services → the integration's card → ⋮ →
*Reconfigure*), **not** by removing and re-adding it. Reconfigure updates the
username/password in place without deleting the config entry, so all existing
entity IDs, history, and automations are preserved, and it is what pushes the
new credentials to the camera bridge (see
[How credential sync ties the two together](#how-credential-sync-ties-the-two-together)).

For the state-change latency options (websocket reconnect timeout, REST poll
interval) exposed under **Configure**, see [configuration.md](configuration.md).

## Step 3: Deploy the camera bridge

The cloned repo directory itself is the deployment location: there is no
separate install path, `docker-compose.yml` runs directly from
`camera-bridge/`.

1. **Change into the bridge directory** (inside your clone):

   ```bash
   cd <repo>/camera-bridge
   ```

2. **Create the config files** from the shipped examples:

   ```bash
   cp .env.example .env
   cp secrets.env.example secrets.env
   ```

   Credentials live in `secrets.env`, ordinary config in `.env`. They are kept
   separate so a gitignored secrets file stays distinct from normal config,
   **not** because Compose processes them differently. Compose interpolates
   `$`-references in the values of *every* `env_file`, `secrets.env` included.

3. **Fill in `secrets.env`** with your TELUS credentials:

   ```ini
   TELUS_EMAIL=<telus-email>
   TELUS_PASSWORD=<telus-password>
   ```

   > **If your password contains a literal `$`, escape it as `$$`.** Compose's
   > value interpolation runs on this file too, and an unescaped `$` is silently
   > corrupted before it reaches the container (this was verified live, not
   > hypothetical). `install.sh` does this escaping automatically if you let it
   > prompt for credentials.

4. **Lock down the secrets file**: it holds your password in plaintext:

   ```bash
   chmod 600 secrets.env
   ```

   This is not optional housekeeping. `cp` creates the file under your umask,
   which on most distributions is world-readable (`0644`), so without this every
   local user could read your TELUS password.

5. **Set any `.env` overrides you need** (all optional; defaults work on the
   common Intel/AMD layout). The most likely ones:

   | Variable | Default | When to set it |
   | --- | --- | --- |
   | `HOST_SHARED_DIR` | `/var/lib/homeassistant/share/telus_cam_bridge` | Your HA share layout differs from the Supervised default. |
   | `RENDER_GID` | `992` | `getent group render` on your host prints a different GID. |
   | `VAAPI_DEVICE` | `/dev/dri/renderD128` | The host has multiple GPUs / different render-node numbering. |
   | `ENCODE_BACKEND` | `vaapi` | Set `nvenc` for NVIDIA (also uncomment the NVIDIA block in `docker-compose.yml`), or `software` to skip hardware encode. |

   Full reference for these and the rest lives in
   [configuration.md](configuration.md).

6. **Prepare the shared directory**: the host path both containers use for
   credential sync. It must be owned by the bridge's non-root container user
   (UID **1500**):

   ```bash
   sudo mkdir -p /var/lib/homeassistant/share/telus_cam_bridge
   sudo chown -R 1500:1500 /var/lib/homeassistant/share/telus_cam_bridge
   ```

   If you set a custom `HOST_SHARED_DIR`, substitute it in both commands.

7. **Build and start the container:**

   ```bash
   sudo docker compose up -d --build
   ```

   The container runs `--network host`, `--restart unless-stopped`, mounts the
   shared directory at `/shared`, and passes through `/dev/dri` for hardware
   encode. On first start it generates the RTSP password and the control secret
   into the shared directory (see below).

8. **Watch the logs** until each camera reports `webrtcup`:

   ```bash
   sudo docker compose logs -f
   ```

   Once you see `webrtcup` for each camera, the integration's camera entities
   are created automatically; it reads the RTSP URLs, credentials included,
   straight from the bridge. Nothing to add by hand.

### Manual recreation later

To rebuild/restart the container after code or config changes:

```bash
cd <repo>/camera-bridge
sudo docker compose up -d --build
```

## Step 4: Confirm the shared directory is mounted by both containers

The entire credential-sync contract rests on **both** containers seeing the
**same** host directory. The bridge writes `control_secret` and
`rtsp_password` there; Home Assistant reads those to authenticate its
credential push and its camera/event polls, and writes `credentials.json`
there as the bridge's fallback copy of the credentials.

Preparing the host path (Step 3.6) says nothing about whether HA's container
actually mounts it, and if it doesn't, **nothing fails loudly**. HA simply
reads no secret, treats the bridge as "not up yet," and silently reports no
cameras forever.

Verify HA mounts the directory:

```bash
sudo docker inspect <ha-container-name> \
  --format '{{range .Mounts}}{{println .Source}}{{end}}' \
  | grep telus_cam_bridge
```

If it does not appear, add a bind mount to the Home Assistant container and
restart it. By convention HA sees the directory at `/share/telus_cam_bridge`
inside its own container:

```
-v /var/lib/homeassistant/share/telus_cam_bridge:/share/telus_cam_bridge
```

(On HA Supervised this is already the case: the `homeassistant` container
mounts the host share directory read-write at `/share/...`.)

## How credential sync ties the two together

Home Assistant's config entry is the **single source of truth** for TELUS
credentials. The camera bridge holds its own separate copy, and the two are
kept in sync automatically so a password change never leaves the bridge stranded
on stale credentials.

When you update credentials via the integration's **Reauth** or **Reconfigure**
flow, the integration does two things, in this order:

1. **Writes a fallback file first.** It writes
   `<shared dir>/credentials.json` (`{"username", "password", "updated_at"}`)
   on *every* successful update, regardless of whether the live push below
   succeeds. This is blocking file I/O done off the event loop.
2. **Pushes to the bridge's control endpoint (best-effort, non-blocking).** It
   `POST`s the new credentials to `http://127.0.0.1:8199/control/credentials`,
   authenticated with a bearer secret read from `<shared dir>/control_secret`.
   The bridge swaps them in and re-logs-in with no container restart.

The bridge picks up a change by whichever path lands first:

- **Live push**: applied immediately.
- **Fallback watcher**: a background task polls `credentials.json` every 30 s
  and re-logs-in if `updated_at` has advanced. Covers a missed push (bridge was
  down, transient loopback hiccup, secret mismatch, etc.).
- **Startup adoption**: a freshly started/recreated container adopts
  `credentials.json` over its `.env` if present, so it picks up the latest
  credentials even if it missed the live push entirely.

Net effect: an HA credential update reaches the bridge immediately, within 30 s,
or at next startup (whichever is first) and HA's own update always
succeeds/fails on HA's TELUS login alone, never blocked on the bridge being
reachable.

The same shared directory also carries `rtsp_password` (generated by the bridge
on first start, mode `0600`), which the integration reads so its camera entities
can authenticate to the RTSP streams without any manual configuration. See
[configuration.md](configuration.md) for the full contents of the shared
directory and how to rotate these secrets.

## Optional: Talkdown (two-way / speak-to-camera audio)

The bridge can push audio *down* to a doorbell-class camera's speaker
(talkdown), exposed in Home Assistant as the `telus_smarthome_alarmdotcom.speak` service,
with `telus_smarthome_alarmdotcom.stop_speaking` to end a clip early. It is **enabled by
default**: nothing ever speaks on its own, the endpoint only plays what a caller
asks it to. Only doorbell-class cameras with an upstream-audio path support it.

1. **To turn it off** (optional), set the environment variable in `.env`; the
   endpoint then returns `403 {"error": "talkdown_disabled"}`:

   ```ini
   TALKDOWN_ENABLED=false
   ```

2. **Recreate the container** so it picks up the change:

   ```bash
   cd <repo>/camera-bridge
   sudo docker compose up -d --build
   ```

3. **Call the service from Home Assistant**: e.g. from Developer Tools →
   Actions, or an automation. The integration registers `telus_smarthome_alarmdotcom.speak`
   once per HA run. Provide either `message` (rendered by HA's TTS engine) *or*
   `media_content_id` (a media-source URI or direct URL); they are mutually
   exclusive. `entity_id` targets a specific TELUS camera; if omitted, the
   doorbell is used.

   ```yaml
   action: telus_smarthome_alarmdotcom.speak
   data:
     entity_id: camera.<doorbell-entity>   # optional; defaults to the doorbell
     message: "Hi, you are being recorded."
     # engine: tts.google_translate        # optional TTS engine override
     # language: en                         # optional TTS language
   ```

   Or play a clip / prerendered TTS instead of `message`:

   ```yaml
   action: telus_smarthome_alarmdotcom.speak
   data:
     media_content_id: "media-source://tts/google_translate?message=Hello"
   ```

The service also takes optional `conditioning` / `audio_filter` fields to
control the bridge's audio-conditioning chain per call; see
[ha-integration.md](ha-integration.md) for the full field reference and
[configuration.md](configuration.md) for the conditioning tunables the bridge
applies before playback.

## Verifying each piece

Work through these in order: each depends on the one before it.

1. **HA integration loaded.**
   Settings → Devices & Services shows the **TELUS Smart Home** card with the
   panel, lock, and sensor entities. The diagnostic **Auth Health** sensor reads
   `ok`.

2. **Panel/sensor push is live.**
   Trip a door/motion sensor and confirm the HA state changes in well under a
   second. To confirm it came over the push websocket rather than the poll,
   check the log: a push updates **one** device
   (`Got update for <name> ... from WebSocket message`), whereas a poll logs
   `Requesting update from Alarm.com` and updates **every** device in the same
   millisecond.

3. **Bridge is up and streaming.**

   ```bash
   sudo docker compose -f <repo>/camera-bridge/docker-compose.yml logs -f
   ```

   Look for `webrtcup` per camera. The bridge's health endpoint answers without
   auth:

   ```bash
   curl -s http://127.0.0.1:8199/control/health
   # {"status": "ok"}
   ```

4. **Shared secrets exist.**

   ```bash
   sudo ls -l /var/lib/homeassistant/share/telus_cam_bridge
   # expect: control_secret, rtsp_password (both mode 0600), and after a
   #         credential update, credentials.json
   ```

5. **Camera entities appear in HA.**
   After the bridge shows `webrtcup` and HA has been restarted (or has re-read
   the shared directory), camera entities show live video. If cameras never
   appear, the most common cause is the shared directory not being mounted by
   HA's container; recheck [Step 4](#step-4-confirm-the-shared-directory-is-mounted-by-both-containers).

6. **RTSP stream reads directly (optional sanity check).**
   The RTSP server requires authentication (user `bridge`, password in
   `rtsp_password`):

   ```bash
   ffplay "rtsp://bridge:$(sudo cat /var/lib/homeassistant/share/telus_cam_bridge/rtsp_password)@127.0.0.1:8554/telus_<camera-id>"
   ```

7. **Credential sync works end to end.**
   Change your password via the integration's **Reconfigure** flow, then confirm
   the bridge re-logs-in (in its log) without a restart, and that
   `credentials.json` in the shared directory has a fresh `updated_at`.

8. **Talkdown (if enabled).**
   Call `telus_smarthome_alarmdotcom.speak` and confirm audio reaches the camera.

## The all-in-one installer (`install.sh`)

The repo root ships an interactive installer that performs the one-time setup:
the network pre-flight check, optional integration copy, bridge credentials, the
shared directory, and the `docker compose` build/up, in a single guided run:

```bash
cd <repo>
./install.sh
```

Requirements: `bash`, Docker with the Compose plugin, and `sudo`. It is safe to
re-run: an existing `secrets.env` and already-copied integration files are left
alone rather than overwritten. Its five steps map directly onto this guide:

| Step | What it does | Manual equivalent |
| --- | --- | --- |
| 1 | Finds the HA container and checks it runs `--network host`; stops if not. | [Network requirement](#the---network-host-requirement-and-why) |
| 2 | Optionally copies `custom_components/telus_smarthome_alarmdotcom/` into your HA config dir (skip if using HACS). | [Step 1, Method B](#method-b-manual-copy) |
| 3 | Prompts for TELUS email/password, `$`-escapes them, writes `secrets.env` mode `0600`. | [Step 3.2–3.4](#step-3-deploy-the-camera-bridge) |
| 4 | Creates and `chown`s the shared directory (UID 1500), records `HOST_SHARED_DIR` in `.env`, and warns if the HA container doesn't mount it. | [Step 3.6](#step-3-deploy-the-camera-bridge) + [Step 4](#step-4-confirm-the-shared-directory-is-mounted-by-both-containers) |
| 5 | Runs `sudo docker compose up -d --build`. | [Step 3.7](#step-3-deploy-the-camera-bridge) |

If you install the integration via HACS and prefer to run the bridge steps by
hand, the manual path in [Step 3](#step-3-deploy-the-camera-bridge) is
equivalent to what the installer does.
