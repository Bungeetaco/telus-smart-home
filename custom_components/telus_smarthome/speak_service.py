"""telus_smarthome.speak -- speak a phrase (or play an audio clip) through a
TELUS camera's speaker via the camera bridge's two-way-audio (talkdown) endpoint.

The service renders `message` to a media URL using HA's own TTS (or takes a
`media_content_id` / direct URL as given), then POSTs {"audio_url": ...} to the
bridge's POST /control/cameras/{id}/talk, authed with the same on-disk shared
secret every other bridge call uses (bridge_client.read_control_secret). The
bridge fetches that loopback URL and plays it -- HA never streams the bytes
itself, so a large clip is the bridge's concern (it caps the fetch).

Registered once per HA run in __init__.py (guarded by has_service, the same
spirit as the clip_view registration guard) and removed when the last config
entry unloads. The bridge endpoint is enabled by default (config.TALKDOWN_ENABLED); if it has
been turned off the bridge returns 403 and this service surfaces that as a clear
HomeAssistantError rather than appearing to succeed.
"""
from __future__ import annotations

import logging

import aiohttp
import voluptuous as vol
from homeassistant.components import media_source
from homeassistant.components.tts import generate_media_source_id
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .bridge_client import read_control_secret
from .const import (
    CAMERA_BRIDGE_TALK_STOP_URL_TEMPLATE,
    CAMERA_BRIDGE_TALK_URL_TEMPLATE,
    DOMAIN,
    SERVICE_SPEAK,
    SERVICE_STOP_SPEAKING,
)

LOGGER = logging.getLogger(__name__)

# One of message / media_content_id is required (checked in the handler so we can
# raise a friendly HomeAssistantError rather than a schema error). entity_id is
# optional -- when omitted we fall back to the doorbell (see _resolve_targets).
_SPEAK_SCHEMA = vol.Schema(
    {
        vol.Optional("entity_id"): cv.entity_ids,
        vol.Exclusive("message", "speak_source"): cv.string,
        vol.Exclusive("media_content_id", "speak_source"): cv.string,
        vol.Optional("engine"): cv.entity_id,
        vol.Optional("language"): cv.string,
    }
)

# > the bridge's own TALK_SESSION_TIMEOUT_S (60s) so HA doesn't give up on a
# slow-but-succeeding talk before the bridge itself would.
_POST_TIMEOUT_S = 70


_STOP_SCHEMA = vol.Schema({vol.Optional("entity_id"): cv.entity_ids})


def async_register_speak_service(hass: HomeAssistant) -> None:
    """Register telus_smarthome.speak + .stop_speaking once.

    Idempotent (guarded by has_service)."""
    if hass.services.has_service(DOMAIN, SERVICE_SPEAK):
        return

    async def _handle_speak(call: ServiceCall) -> None:
        data = _SPEAK_SCHEMA(dict(call.data))
        message = data.get("message")
        media_content_id = data.get("media_content_id")
        if not message and not media_content_id:
            raise HomeAssistantError("speak requires either 'message' or 'media_content_id'")

        audio_url = await _resolve_audio_url(hass, data, message, media_content_id)

        targets = _resolve_targets(hass, data.get("entity_id"))
        if not targets:
            raise HomeAssistantError(
                "speak found no target TELUS camera (pass entity_id, or ensure a "
                "doorbell camera exists)")

        secret = await hass.async_add_executor_job(read_control_secret)
        if secret is None:
            raise HomeAssistantError("Camera bridge not reachable (no control secret on the shared mount)")

        session = async_get_clientsession(hass)
        for entity_id, camera_id in targets:
            await _post_talk(session, secret, entity_id, camera_id, audio_url)

    async def _handle_stop(call: ServiceCall) -> None:
        """Stop whatever is currently playing through the camera's speaker.

        Stopping something that isn't playing is not an error -- the bridge
        answers 404 and we simply log it, so an automation can fire this
        unconditionally (e.g. when a door opens) without needing to know whether
        a clip is in flight."""
        data = _STOP_SCHEMA(dict(call.data))
        targets = _resolve_targets(hass, data.get("entity_id"))
        if not targets:
            raise HomeAssistantError(
                "stop_speaking found no target TELUS camera (pass entity_id, or "
                "ensure a doorbell camera exists)")

        secret = await hass.async_add_executor_job(read_control_secret)
        if secret is None:
            raise HomeAssistantError(
                "Camera bridge not reachable (no control secret on the shared mount)")

        session = async_get_clientsession(hass)
        for entity_id, camera_id in targets:
            url = CAMERA_BRIDGE_TALK_STOP_URL_TEMPLATE.format(camera_id=camera_id)
            try:
                async with session.post(
                    url,
                    headers={"Authorization": f"Bearer {secret}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 404:
                        LOGGER.debug("stop_speaking: nothing playing on %s", entity_id)
                        continue
                    if resp.status == 403:
                        raise HomeAssistantError(
                            "stop_speaking: talkdown is disabled on the camera bridge")
                    resp.raise_for_status()
                    LOGGER.info("stop_speaking: stopping talk on %s", entity_id)
            except HomeAssistantError:
                raise
            except (aiohttp.ClientError, TimeoutError) as err:
                raise HomeAssistantError(
                    f"stop_speaking failed for {entity_id}: {err}") from err

    hass.services.async_register(DOMAIN, SERVICE_SPEAK, _handle_speak, schema=_SPEAK_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_STOP_SPEAKING, _handle_stop,
                                 schema=_STOP_SCHEMA)
    LOGGER.debug("Registered %s.%s and %s.%s services",
                 DOMAIN, SERVICE_SPEAK, DOMAIN, SERVICE_STOP_SPEAKING)


def async_remove_speak_service(hass: HomeAssistant) -> None:
    """Remove the services (called when the last config entry unloads)."""
    for service in (SERVICE_SPEAK, SERVICE_STOP_SPEAKING):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


async def _resolve_audio_url(hass, data, message, media_content_id) -> str:
    """Turn the service data into a single absolute http(s) URL the bridge can
    fetch. A `message` is rendered via HA TTS to a media-source URL; a
    `media_content_id` is resolved if it is a media-source URI, or passed through
    if it is already an http(s) URL."""
    if message:
        engine = data.get("engine") or _default_tts_entity(hass)
        if engine is None:
            raise HomeAssistantError("No TTS engine available to render 'message' for speak")
        media_id = generate_media_source_id(
            hass, message, engine=engine, language=data.get("language"))
    elif media_content_id.startswith(("http://", "https://")):
        # Already a fetchable URL -- hand it straight to the bridge.
        return media_content_id
    else:
        media_id = media_content_id

    try:
        resolved = await media_source.async_resolve_media(hass, media_id, None)
    except Exception as err:
        raise HomeAssistantError(f"speak could not resolve media for playback: {err}") from err

    url = resolved.url
    if url.startswith("/"):
        # media_source returns an app-relative URL (e.g. /api/tts_proxy/...). The
        # bridge runs on the same host (--network host) but is a separate process,
        # so it needs an absolute URL. Use HA's internal base URL.
        try:
            base = get_url(hass, allow_internal=True, prefer_external=False)
        except NoURLAvailableError as err:
            raise HomeAssistantError(
                "speak could not determine HA's internal URL to hand the bridge; "
                "set an internal URL under Settings > System > Network") from err
        url = f"{base}{url}"
    return url


def _default_tts_entity(hass: HomeAssistant) -> str | None:
    """First available tts.* entity (google_translate / Nabu Casa cloud on this
    estate) when the caller didn't pass an explicit engine."""
    for entity_id in hass.states.async_entity_ids("tts"):
        return entity_id
    return None


def _resolve_targets(hass: HomeAssistant, entity_ids: list[str] | None) -> list[tuple[str, str]]:
    """Resolve service targets to (entity_id, bridge camera_id) pairs.

    When no entity_id is given, default to the doorbell: the TELUS bridge camera
    entities whose device looks like a doorbell (model/name), falling back to all
    bridge cameras if none obviously match. camera_id is the entity's unique_id
    (TelusCamera sets _attr_unique_id = camera_id -- see camera.py)."""
    registry = er.async_get(hass)
    if entity_ids:
        pairs = []
        for entity_id in entity_ids:
            camera_id = _camera_id_for_entity(registry, entity_id)
            if camera_id is None:
                raise HomeAssistantError(f"{entity_id} is not a TELUS bridge camera")
            pairs.append((entity_id, camera_id))
        return pairs

    all_cams = [
        ent for ent in registry.entities.values()
        if ent.platform == DOMAIN and ent.domain == "camera"
    ]
    doorbells = [
        ent for ent in all_cams
        if "doorbell" in (ent.original_name or ent.name or "").lower()
        or "doorbell" in (ent.entity_id or "").lower()
    ]
    chosen = doorbells or all_cams
    return [(ent.entity_id, ent.unique_id) for ent in chosen if ent.unique_id]


def _camera_id_for_entity(registry, entity_id: str) -> str | None:
    ent = registry.async_get(entity_id)
    if ent is None or ent.platform != DOMAIN or ent.domain != "camera":
        return None
    return ent.unique_id


async def _post_talk(session, secret, entity_id, camera_id, audio_url) -> None:
    url = CAMERA_BRIDGE_TALK_URL_TEMPLATE.format(camera_id=camera_id)
    try:
        async with session.post(
            url,
            json={"audio_url": audio_url},
            headers={"Authorization": f"Bearer {secret}"},
            timeout=aiohttp.ClientTimeout(total=_POST_TIMEOUT_S),
        ) as resp:
            if resp.status == 409:
                # A talk is already in flight for this camera -- harmless, skip.
                LOGGER.warning("speak: a talk is already in progress for %s; skipping", entity_id)
                return
            if resp.status == 403:
                raise HomeAssistantError(
                    "speak: talkdown is disabled on the camera bridge "
                    "(set TALKDOWN_ENABLED=true and restart the bridge)")
            if resp.status == 422:
                raise HomeAssistantError(f"speak: {entity_id} has no upstream (talk) audio path")
            resp.raise_for_status()
    except HomeAssistantError:
        raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise HomeAssistantError(f"speak failed for {entity_id}: {err}") from err
