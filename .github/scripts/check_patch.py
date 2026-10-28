"""CI smoke test: prove this integration's monkeypatches still fit the installed
pyalarmdotcomajax.

The integration patches the library in three places, each reaching into private
internals that a version bump can move:

  * telus_auth_patch.py        -- replaces AlarmController.async_login outright
  * telus_ws_heartbeat_patch.py -- replaces WebSocketClient._connect
  * telus_ws_push_patch.py      -- wraps WebSocketClient._async_handle_message

Each module carries an import-time compatibility guard; loading them here fires
those guards. On top of that, the replaced async_login populates roughly a dozen
PRIVATE INSTANCE attributes that no class-level hasattr() can see, so we build a
throwaway controller and assert they exist after __init__. Without that, an
upstream rename would still "pass" and produce a half-populated controller --
a login that succeeds but yields a dead session.

Run from the repo root: python .github/scripts/check_patch.py
"""
import asyncio
import importlib.util
import pathlib
import sys

BASE = pathlib.Path("custom_components/telus_smarthome_alarmdotcom")
MODULES = (
    "telus_auth_patch",
    "telus_ws_heartbeat_patch",
    "telus_ws_push_patch",
)

# Private instance attributes the replacement async_login reads or assigns.
# Keep in sync with telus_auth_patch.py's _REQUIRED_CONTROLLER_ATTRS comment.
REQUIRED_INSTANCE_ATTRS = (
    "_websession",
    "_username",
    "_password",
    "_ajax_headers",
    "_last_session_refresh",
    "_provider_name",
    "_user_id",
    "_user_email",
    "_session_refresh_interval_ms",
    "_keep_alive_url",
    "_user_profile",
)


def _load(name):
    """Import a patch module BY PATH, so the package __init__ (which imports
    Home Assistant, absent in CI) is never executed."""
    spec = importlib.util.spec_from_file_location(name, BASE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # runs that module's compatibility guard
    return mod


def main() -> int:
    loaded = {}
    for name in MODULES:
        try:
            loaded[name] = _load(name)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: {name} did not import/apply cleanly: {exc}")
            return 1
        print(f"  ok: {name} imported and applied")

    from pyalarmdotcomajax import AlarmController
    from pyalarmdotcomajax.websockets.client import WebSocketClient

    # The patches must actually be in place, not merely importable.
    if AlarmController.async_login is not loaded["telus_auth_patch"]._telus_async_login:
        print("FAIL: AlarmController.async_login was not replaced by the SSO patch")
        return 1
    if WebSocketClient._connect is not loaded["telus_ws_heartbeat_patch"]._connect_with_heartbeat:
        print("FAIL: WebSocketClient._connect was not replaced by the heartbeat patch")
        return 1
    if WebSocketClient._async_handle_message is not loaded["telus_ws_push_patch"]._handle_message_and_notify:
        print("FAIL: WebSocketClient._async_handle_message was not replaced by the push patch")
        return 1
    print("  ok: all three patches are installed on the library")

    # Instance-level internals the replacement body depends on.
    async def _check_instance():
        import aiohttp

        async with aiohttp.ClientSession() as session:
            controller = AlarmController(
                username="ci@example.invalid", password="unused", websession=session
            )
            return [a for a in REQUIRED_INSTANCE_ATTRS if not hasattr(controller, a)]

    missing = asyncio.run(_check_instance())
    if missing:
        print(
            "FAIL: AlarmController no longer exposes the private instance "
            f"attributes the SSO patch depends on: {missing}"
        )
        return 1
    print(f"  ok: all {len(REQUIRED_INSTANCE_ATTRS)} private instance attributes present")

    import pyalarmdotcomajax

    print(
        "OK: TELUS patches are compatible with pyalarmdotcomajax "
        f"{getattr(pyalarmdotcomajax, '__version__', 'unknown')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
