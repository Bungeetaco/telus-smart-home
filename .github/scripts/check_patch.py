"""CI smoke test: load telus_auth_patch.py against the installed
pyalarmdotcomajax and confirm the TELUS SSO monkeypatch still applies.

The patch reaches into pyalarmdotcomajax internals (it replaces
AlarmController.async_login and reads several class-level attributes). A
dependency bump that renames or removes any of them would otherwise break login
silently -- this fails the PR instead. The patch module runs its own guard
assertions at import time; loading it here triggers them.
"""
import importlib.util
import pathlib
import sys

PATCH = pathlib.Path("custom_components/telus_smarthome/telus_auth_patch.py")

# Load the module by file path so we do NOT trigger the integration package's
# __init__ (which imports Home Assistant, not installed in this job).
spec = importlib.util.spec_from_file_location("telus_auth_patch", PATCH)
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)  # runs the in-patch compatibility guard
except Exception as exc:  # noqa: BLE001
    print(f"FAIL: telus_auth_patch did not import/apply cleanly: {exc}")
    sys.exit(1)

from pyalarmdotcomajax import AlarmController

if AlarmController.async_login is not mod._telus_async_login:
    print("FAIL: AlarmController.async_login was not replaced by the patch")
    sys.exit(1)

import pyalarmdotcomajax
print(f"OK: TELUS SSO patch applies against pyalarmdotcomajax "
      f"{getattr(pyalarmdotcomajax, '__version__', 'unknown')}")
