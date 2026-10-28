"""Tests for the GUI config/reconfigure/reauth flow (`config_flow.py`, backlog E66).

Run from the repository root:

    python3 tests/test_config_flow.py

No Home Assistant, no network, no TELUS: `tests/ha_stubs.py` supplies the
`homeassistant.*` and `pyalarmdotcomajax` names (including a ConfigFlow base
whose data-vs-data_updates semantics are reproduced from upstream exactly
because they are the invariant under test), `tests/fakes.py` supplies hass and
the scripted controller, and the REAL `config_flow.py` steps run.

Why this file exists: the flow is the first thing a new user touches and the
thing an existing user touches at the worst possible moment (a reauth, when
their entities are already dead). Every failure here is silent until a human
is standing in front of the form:

  * the exception-to-error-key mapping decides whether an unreachable TELUS
    reads as "cannot connect" or as a wrong-password accusation;
  * a reauth that REPLACES entry data instead of merging drops
    clip_link_secret, and every clip link already delivered to a phone 401s
    forever -- nothing errors, the links just die;
  * the unique-id guard is what stops a reauth from silently repointing an
    entry at a different TELUS account;
  * each retyped password used to leak an aiohttp session; the flow closes the
    previous controller before building the next;
  * the credential fallback file holds a plaintext password and must never be
    world-readable, and its write must never fail the flow.

Pinned here: the happy path (title, unique id, trimmed credentials, default
options), the full error mapping on user and reconfigure steps, the OTP
branches (app-only skip, email/sms request, submit success and each failure),
the reauth/reconfigure merge-not-replace invariant, the cross-account and
duplicate-account aborts, the first-reauth-after-upgrade unique_id=None
grace, session cleanup per retry, the options flow (including that its
handler never assigns the read-only config_entry property), and the
fallback-file writer's mode, atomicity and never-raise contract.
"""
import asyncio
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402

ha_stubs.install()

import aiohttp  # noqa: E402

import fakes  # noqa: E402
from fakes import (  # noqa: E402
    FakeConfigEntries,
    FakeConfigEntry,
    FakeHass,
    FakeResponse,
    FakeSession,
)

from homeassistant.exceptions import (  # noqa: E402
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
)
from pyalarmdotcomajax.const import OtpType  # noqa: E402
from pyalarmdotcomajax.exceptions import (  # noqa: E402
    AlarmdotcomException,
    AuthenticationFailed,
    ConfigureTwoFactorAuthentication,
    OtpRequired,
    UnexpectedResponse,
)

from custom_components.telus_smarthome_alarmdotcom import config_flow  # noqa: E402
from custom_components.telus_smarthome_alarmdotcom.const import (
    CONF_CALL_ENABLED,  # noqa: E402
    CONF_2FA_COOKIE,
    CONF_ARM_CODE,
    CONF_CLIP_LINK_SECRET,
    CONF_OPTIONS_DEFAULT,
    CONF_UPDATE_INTERVAL,
    CONF_WEBSOCKET_RECONNECT_TIMEOUT,
)

EMAIL = "user@example.com"


def controller_factory(login_outcomes, user_email=EMAIL, otp_outcome="tf-cookie"):
    """A scripted AlarmIntegrationController class plus the instances it made.

    `login_outcomes` is consumed one element per initialize_lite call: an
    exception instance raises, anything else succeeds. `otp_outcome` is the
    two-factor cookie async_submit_otp leaves behind (an exception raises).
    """
    created = []

    class _Api:
        def __init__(self, controller):
            self._controller = controller
            self.two_factor_cookie = None

        async def async_request_otp(self, method):
            self._controller.otp_requests.append(method)

        async def async_submit_otp(self, method, code, device_name):
            self._controller.otp_submissions.append((method, code, device_name))
            if isinstance(otp_outcome, BaseException):
                raise otp_outcome
            self.two_factor_cookie = otp_outcome

    class _Controller:
        def __init__(self, hass, config):
            self.hass = hass
            self.config = config
            self.closed = 0
            self.otp_requests = []
            self.otp_submissions = []
            self.login_calls = []
            self.provider_name = "TELUS"
            self.user_email = user_email
            self.api = _Api(self)
            created.append(self)

        def release_websession(self):
            self.closed += 1

        async def initialize_lite(self, *, username, password, twofactorcookie):
            self.login_calls.append((username, password, twofactorcookie))
            outcome = login_outcomes.pop(0) if login_outcomes else None
            if isinstance(outcome, BaseException):
                raise outcome

    return _Controller, created


def make_flow(entries=(), context=None, login_outcomes=None, share_dir=None, **factory_kwargs):
    """The REAL ADCFlowHandler over a fake hass, with a scripted controller."""
    hass = FakeHass()
    hass.config_entries = FakeConfigEntries(list(entries))
    flow = config_flow.ADCFlowHandler()
    flow.hass = hass
    flow.context = dict(context or {})

    controller_cls, created = controller_factory(list(login_outcomes or []), **factory_kwargs)
    saved = fakes.patch(config_flow, AlarmIntegrationController=controller_cls)
    if share_dir is not None:
        saved.update(fakes.patch(config_flow, CAMERA_BRIDGE_SHARE_DIR=str(share_dir)))
    flow._test_saved = saved
    flow._test_created = created
    return flow


def unpatch(flow):
    fakes.restore(config_flow, flow._test_saved)


CREDS = {"username": EMAIL, "password": "hunter2"}


async def test_user_step_success_creates_entry():
    flow = make_flow()
    try:
        result = await flow.async_step_user(dict(CREDS))
    finally:
        unpatch(flow)
    assert result["type"] == "create_entry", f"expected an entry, got {result!r}"
    assert result["title"] == f"TELUS:{EMAIL}", (
        f"title {result['title']!r}: the entry must be named provider:email so "
        "two accounts stay tellable apart")
    assert result["data"]["username"] == EMAIL
    assert result["data"]["password"] == "hunter2"
    assert result["data"][CONF_2FA_COOKIE] is None
    assert result["options"] == CONF_OPTIONS_DEFAULT, (
        "a fresh entry must start from CONF_OPTIONS_DEFAULT or the options "
        "flow renders empty defaults")
    assert flow.unique_id == EMAIL, (
        "unique_id must be the account email -- it is the duplicate-account "
        "and cross-account guard")
    assert flow._test_created[-1].closed == 1, (
        "the flow's own controller session must be released before the entry is "
        "created; async_setup_entry builds its own")


async def test_submitted_credentials_are_trimmed():
    flow = make_flow()
    try:
        await flow.async_step_user({"username": f"  {EMAIL}\n", "password": "hunter2 \n"})
    finally:
        unpatch(flow)
    username, password, _cookie = flow._test_created[0].login_calls[0]
    assert username == EMAIL and password == "hunter2", (
        f"login used {username!r}/{password!r} -- a pasted trailing newline "
        "must not become a silent wrong-password loop")


async def test_login_failures_map_to_their_error_keys():
    cases = [
        (TimeoutError(), "cannot_connect"),
        (aiohttp.ClientError("down"), "cannot_connect"),
        (ConfigEntryNotReady(), "cannot_connect"),
        (UnexpectedResponse("sso moved"), "telus_sso_error"),
        (ConfigEntryAuthFailed(), "invalid_auth"),
        (AlarmdotcomException("boom"), "unknown"),
    ]
    for outcome, expected in cases:
        flow = make_flow(login_outcomes=[outcome])
        try:
            result = await flow.async_step_user(dict(CREDS))
        finally:
            unpatch(flow)
        assert result["type"] == "form" and result["step_id"] == "user", (
            f"{type(outcome).__name__} must re-show the form, got {result!r}")
        assert result["errors"].get("base") == expected, (
            f"{type(outcome).__name__} mapped to {result['errors']!r}, expected "
            f"{expected!r} -- an unreachable TELUS must never read as a wrong "
            "password (or vice versa)")


async def test_must_enable_2fa_aborts():
    flow = make_flow(login_outcomes=[ConfigureTwoFactorAuthentication()])
    try:
        result = await flow.async_step_user(dict(CREDS))
    finally:
        unpatch(flow)
    assert result["type"] == "abort" and result["reason"] == "must_enable_2fa", (
        f"an account still without 2FA must abort with instructions, got {result!r}")


async def test_each_retry_closes_the_previous_controllers_session():
    flow = make_flow(login_outcomes=[ConfigEntryAuthFailed(), ConfigEntryAuthFailed(), None])
    try:
        await flow.async_step_user(dict(CREDS))
        await flow.async_step_user(dict(CREDS))
        await flow.async_step_user(dict(CREDS))
    finally:
        unpatch(flow)
    created = flow._test_created
    assert len(created) == 3
    assert created[0].closed == 1 and created[1].closed == 1, (
        "each retyped password must close the previous attempt's session -- "
        "unreleased ones outlive the flow, one per retry, for the life of HA")


async def test_otp_app_only_skips_method_selection():
    flow = make_flow(login_outcomes=[OtpRequired([OtpType.app])])
    try:
        result = await flow.async_step_user(dict(CREDS))
    finally:
        unpatch(flow)
    assert result["type"] == "form" and result["step_id"] == "otp_submit", (
        f"app-only 2FA must go straight to code entry, got {result!r}")
    assert flow.otp_method is OtpType.app
    assert flow._test_created[0].otp_requests == [], (
        "an authenticator app is never 'sent' a code; requesting one here "
        "would be a live API call for nothing")


async def test_otp_email_is_requested_then_submitted():
    flow = make_flow(login_outcomes=[OtpRequired([OtpType.email, OtpType.sms])])
    try:
        result = await flow.async_step_user(dict(CREDS))
        assert result["type"] == "form" and result["step_id"] == "otp_select_method", (
            f"two enabled methods must offer a choice, got {result!r}")
        result = await flow.async_step_otp_select_method({"otp_method": "email"})
        assert result["type"] == "form" and result["step_id"] == "otp_submit"
        controller = flow._test_created[0]
        assert controller.otp_requests == [OtpType.email], (
            "choosing email must ask Alarm.com to send the code")
        result = await flow.async_step_otp_submit({"otp": "123456"})
    finally:
        unpatch(flow)
    assert result["type"] == "create_entry", f"expected an entry, got {result!r}"
    assert result["data"][CONF_2FA_COOKIE] == "tf-cookie", (
        "the earned two-factor cookie must be stored, or every restart "
        "re-prompts for OTP")
    method, code, device_name = flow._test_created[0].otp_submissions[0]
    assert method is OtpType.email and code == "123456"
    assert "Test Home" in device_name, (
        f"device_name {device_name!r} should carry the HA location so the "
        "user recognises it in their Alarm.com device list")


async def test_otp_submit_failures():
    # A rejected code, and a submission that earns no cookie: both are
    # invalid_otp, both re-show the code form rather than dead-ending.
    for otp_outcome in (AuthenticationFailed("bad code"), None):
        flow = make_flow(login_outcomes=[OtpRequired([OtpType.app])],
                         otp_outcome=otp_outcome)
        try:
            await flow.async_step_user(dict(CREDS))
            result = await flow.async_step_otp_submit({"otp": "000000"})
        finally:
            unpatch(flow)
        assert result["type"] == "form" and result["step_id"] == "otp_submit"
        assert result["errors"].get("base") == "invalid_otp", (
            f"outcome {otp_outcome!r} mapped to {result['errors']!r}")

    flow = make_flow(login_outcomes=[OtpRequired([OtpType.app])],
                     otp_outcome=TimeoutError())
    try:
        await flow.async_step_user(dict(CREDS))
        result = await flow.async_step_otp_submit({"otp": "000000"})
    finally:
        unpatch(flow)
    assert result["errors"].get("base") == "cannot_connect", (
        "a timeout during OTP submission is a connection problem, not a bad code")


async def test_reauth_merges_data_and_keeps_the_clip_link_secret():
    entry = FakeConfigEntry(
        data={"username": EMAIL, "password": "old", CONF_2FA_COOKIE: "old-cookie",
              CONF_CLIP_LINK_SECRET: "precious-secret"},
        unique_id=EMAIL, title="TELUS:" + EMAIL)
    with tempfile.TemporaryDirectory() as tmp:
        flow = make_flow(entries=[entry], context={"entry_id": entry.entry_id},
                         share_dir=tmp)
        try:
            result = await flow.async_step_reauth({})
            assert result["type"] == "form" and result["step_id"] == "reauth_confirm"
            result = await flow.async_step_reauth_confirm({})  # user pressed submit
            assert result["type"] == "form" and result["step_id"] == "user"
            result = await flow.async_step_user({"username": EMAIL, "password": "newpass"})
        finally:
            unpatch(flow)
    assert result["type"] == "abort" and result["reason"] == "reauth_successful", (
        f"expected reauth_successful, got {result!r}")
    assert entry.data["password"] == "newpass"
    assert entry.data[CONF_CLIP_LINK_SECRET] == "precious-secret", (
        "reauth REPLACED entry data instead of merging -- clip_link_secret is "
        "gone, a fresh secret will be minted, and every clip link already on a "
        "phone 401s forever")
    assert flow.hass.config_entries.reloads == [entry.entry_id], (
        "the entry must be reloaded so the new credentials are actually used")
    assert flow._test_created[-1].closed == 1


async def test_reauth_refuses_a_different_account():
    entry = FakeConfigEntry(data={"username": EMAIL, "password": "old"},
                            unique_id=EMAIL)
    flow = make_flow(entries=[entry], context={"entry_id": entry.entry_id},
                     user_email="other@example.com")
    try:
        await flow.async_step_reauth({})
        await flow.async_step_reauth_confirm({})
        try:
            await flow.async_step_user({"username": "other@example.com", "password": "x"})
        except ha_stubs.AbortFlow as exc:
            reason = exc.reason
        else:
            reason = None
    finally:
        unpatch(flow)
    assert reason == "unique_id_mismatch", (
        f"a reauth with a different account's credentials answered {reason!r} "
        "-- it must abort, not silently repoint the entry (and its entities, "
        "automations and history) at another TELUS account")
    assert entry.data["password"] == "old", "the mismatched credentials must not be saved"


async def test_first_reauth_after_upgrade_tolerates_missing_unique_id():
    # Entries created before the unique_id migration have none; the guard must
    # not fire on them, and the reauth must stamp the id for next time.
    entry = FakeConfigEntry(data={"username": EMAIL, "password": "old"}, unique_id=None)
    with tempfile.TemporaryDirectory() as tmp:
        flow = make_flow(entries=[entry], context={"entry_id": entry.entry_id},
                         share_dir=tmp)
        try:
            await flow.async_step_reauth({})
            await flow.async_step_reauth_confirm({})
            result = await flow.async_step_user(dict(CREDS))
        finally:
            unpatch(flow)
    assert result["type"] == "abort" and result["reason"] == "reauth_successful", (
        f"a legacy entry's first reauth spuriously aborted: {result!r}")
    assert entry.unique_id == EMAIL, "the reauth must stamp the missing unique_id"


async def test_duplicate_account_aborts():
    existing = FakeConfigEntry(data={"username": EMAIL}, unique_id=EMAIL)
    flow = make_flow(entries=[existing])
    try:
        try:
            await flow.async_step_user(dict(CREDS))
        except ha_stubs.AbortFlow as exc:
            reason = exc.reason
        else:
            reason = None
    finally:
        unpatch(flow)
    assert reason == "already_configured_account", (
        f"onboarding the same account twice answered {reason!r} -- a duplicate "
        "entry means a duplicate device/entity tree")


async def test_reconfigure_maps_errors_and_merges_on_success():
    entry = FakeConfigEntry(
        data={"username": EMAIL, "password": "old", CONF_CLIP_LINK_SECRET: "precious"},
        unique_id=EMAIL)
    flow = make_flow(entries=[entry], context={"entry_id": entry.entry_id},
                     login_outcomes=[ConfigEntryAuthFailed()])
    try:
        result = await flow.async_step_reconfigure(None)
        assert result["type"] == "form" and result["step_id"] == "reconfigure"
        result = await flow.async_step_reconfigure({"username": EMAIL, "password": "bad"})
        assert result["errors"].get("base") == "invalid_auth", (
            f"rejected credentials mapped to {result['errors']!r}")
    finally:
        unpatch(flow)

    with tempfile.TemporaryDirectory() as tmp:
        flow = make_flow(entries=[entry], context={"entry_id": entry.entry_id},
                         share_dir=tmp)
        try:
            result = await flow.async_step_reconfigure({"username": EMAIL, "password": "new"})
        finally:
            unpatch(flow)
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert entry.data["password"] == "new"
    assert entry.data[CONF_CLIP_LINK_SECRET] == "precious", (
        "reconfigure dropped clip_link_secret -- same merge-not-replace "
        "invariant as reauth")
    # A reconfigure always re-verifies 2FA from scratch.
    assert entry.data[CONF_2FA_COOKIE] is None


async def test_options_flow_clears_code_and_merges():
    entry = FakeConfigEntry(options={**CONF_OPTIONS_DEFAULT, CONF_ARM_CODE: "1234"})
    handler = config_flow.ADCOptionsFlowHandler(entry)
    handler.hass = FakeHass()
    assert "config_entry" not in vars(handler), (
        "the options handler assigned self.config_entry -- that is a read-only "
        "property on current HA core and broke the gear icon entirely")

    result = await handler.async_step_init(None)
    assert result["type"] == "form" and result["step_id"] == "init"
    result = await handler.async_step_init({
        CONF_ARM_CODE: "CLEAR!", CONF_UPDATE_INTERVAL: 30,
        CONF_WEBSOCKET_RECONNECT_TIMEOUT: 60})
    assert result["type"] == "form" and result["step_id"] == "modes"
    result = await handler.async_step_modes({"arm_home_options": ["arming_option_bypass"]})
    # Modes is no longer the last step: it advances to the doorbell-call step,
    # which is where the entry is created.
    assert result["type"] == "form" and result["step_id"] == "call", (
        f"modes must lead to the call step, got {result!r}")
    with tempfile.TemporaryDirectory() as tmp:
        saved = fakes.patch(config_flow, CAMERA_BRIDGE_SHARE_DIR=str(tmp))
        try:
            result = await handler.async_step_call({CONF_CALL_ENABLED: False})
        finally:
            fakes.restore(config_flow, saved)
    assert result["type"] == "create_entry", f"expected saved options, got {result!r}"
    assert result["data"][CONF_ARM_CODE] == "", (
        "the documented CLEAR! sentinel must empty the arm code -- there is no "
        "other way to remove one from the UI")
    assert result["data"][CONF_UPDATE_INTERVAL] == 30
    assert result["data"]["arm_home_options"] == ["arming_option_bypass"]
    assert result["data"]["arm_away_options"] == [], (
        "options from the entry that the user did not touch must survive the save")


async def test_call_settings_step_writes_the_fallback_and_pushes():
    """The doorbell-call options step must behave like the credential push:
    write the shared-dir fallback (so a restarted bridge still gets it) AND
    live-push /control/call/settings, with the SIP password never leaving the
    two channels the bridge reads. A bridge that is down must not fail the
    save."""
    entry = FakeConfigEntry(options={**CONF_OPTIONS_DEFAULT})
    handler = config_flow.ADCOptionsFlowHandler(entry)
    handler.hass = FakeHass()
    with tempfile.TemporaryDirectory() as tmp:
        share = pathlib.Path(tmp)
        (share / "control_secret").write_text("bridge-secret\n")
        session = FakeSession([FakeResponse(200, {"status": "applied"})])
        saved = fakes.patch(
            config_flow, CAMERA_BRIDGE_SHARE_DIR=str(share),
            async_get_clientsession=fakes.clientsession_for(session))
        try:
            result = await handler.async_step_call({
                CONF_CALL_ENABLED: True,
                "call_sip_server": " montreal10.voip.ms ",
                "call_sip_account": "215317_doorbell",
                "call_sip_password": "s3cret-sip",
                "call_did": "8190000000",
                "call_target": "+18195551234",
            })
        finally:
            fakes.restore(config_flow, saved)
        assert result["type"] == "create_entry", result
        # Fallback file written, private, and shape-correct.
        written = json.loads((share / "call_settings.json").read_text())
        assert written["enabled"] is True
        assert written["sip_server"] == "montreal10.voip.ms", "not stripped"
        assert written["sip_password"] == "s3cret-sip"
        assert "updated_at" in written
        mode = (share / "call_settings.json").stat().st_mode & 0o777
        assert mode == 0o600, f"call settings not private: {mode:o}"
        # Pushed with the bearer secret.
        assert session.requests, "the settings were never pushed to the bridge"
        req = session.requests[-1]
        assert req["headers"]["Authorization"] == "Bearer bridge-secret"
        assert req["json"]["sip_account"] == "215317_doorbell"
    print("  ok  call step wrote the private fallback and pushed with the secret")


async def test_call_settings_step_survives_an_unreachable_bridge():
    entry = FakeConfigEntry(options={**CONF_OPTIONS_DEFAULT})
    handler = config_flow.ADCOptionsFlowHandler(entry)
    handler.hass = FakeHass()
    with tempfile.TemporaryDirectory() as tmp:
        share = pathlib.Path(tmp)
        (share / "control_secret").write_text("bridge-secret\n")
        session = FakeSession([aiohttp.ClientError("bridge down")])
        saved = fakes.patch(
            config_flow, CAMERA_BRIDGE_SHARE_DIR=str(share),
            async_get_clientsession=fakes.clientsession_for(session))
        try:
            result = await handler.async_step_call({CONF_CALL_ENABLED: True})
        finally:
            fakes.restore(config_flow, saved)
        assert result["type"] == "create_entry", (
            "an unreachable bridge failed the options save; the fallback file is "
            "written, so the save must still succeed")
        assert (share / "call_settings.json").exists(), (
            "the fallback file must be written even when the push fails")
    print("  ok  call step survives an unreachable bridge")


async def test_fallback_file_is_private_and_atomic():
    with tempfile.TemporaryDirectory() as tmp:
        share = pathlib.Path(tmp)
        (share / "control_secret").write_text("s3cret\n")
        saved = fakes.patch(config_flow, CAMERA_BRIDGE_SHARE_DIR=str(share))
        try:
            secret = config_flow._sync_write_fallback_and_read_secret(
                {"username": EMAIL, "password": "pw"})
        finally:
            fakes.restore(config_flow, saved)
        assert secret == "s3cret", f"control secret misread: {secret!r}"
        credentials = share / "credentials.json"
        assert credentials.exists(), "the fallback file was not written"
        mode = credentials.stat().st_mode & 0o777
        assert mode == 0o600, (
            f"credentials.json is mode {oct(mode)} -- it holds a plaintext "
            "TELUS password and must never be world-readable")
        assert not (share / "credentials.json.tmp").exists(), (
            "the temp file must be atomically renamed away, or a torn write "
            "leaves a half-written password file for the bridge to trip on")


async def test_fallback_write_failure_never_raises():
    with tempfile.TemporaryDirectory() as tmp:
        blocker = pathlib.Path(tmp) / "not-a-dir"
        blocker.write_text("")
        saved = fakes.patch(
            config_flow, CAMERA_BRIDGE_SHARE_DIR=str(blocker / "share"))
        try:
            secret = config_flow._sync_write_fallback_and_read_secret({"username": EMAIL})
        finally:
            fakes.restore(config_flow, saved)
    assert secret is None, (
        "an unwritable share dir must degrade to None -- the camera bridge "
        "being absent must never fail a login that Alarm.com accepted")


async def test_credential_push_reaches_the_bridge_with_the_secret():
    entry = FakeConfigEntry(data={"username": EMAIL, "password": "old"}, unique_id=EMAIL)
    with tempfile.TemporaryDirectory() as tmp:
        (pathlib.Path(tmp) / "control_secret").write_text("bridge-secret")
        session = FakeSession([FakeResponse(200, {})])
        flow = make_flow(entries=[entry], context={"entry_id": entry.entry_id},
                         share_dir=tmp)
        saved = fakes.patch(config_flow,
                            async_get_clientsession=fakes.clientsession_for(session))
        try:
            result = await flow.async_step_reconfigure(
                {"username": EMAIL, "password": "new"})
        finally:
            fakes.restore(config_flow, saved)
            unpatch(flow)
    assert result["type"] == "abort" and result["reason"] == "reconfigure_successful"
    assert len(session.requests) == 1, "exactly one live push per credential change"
    request = session.requests[0]
    assert request["url"].endswith("/control/credentials")
    assert request["json"] == {"username": EMAIL, "password": "new"}
    assert request["headers"].get("Authorization") == "Bearer bridge-secret", (
        "the push must be authed with the on-disk control secret or the "
        "bridge rejects it and keeps hammering TELUS with the old password")


async def test_credential_push_failures_never_fail_the_flow():
    entry = FakeConfigEntry(data={"username": EMAIL, "password": "old"}, unique_id=EMAIL)
    for script in ([FakeResponse(500, {})], [aiohttp.ClientConnectionError("refused")]):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "control_secret").write_text("bridge-secret")
            session = FakeSession(list(script))
            flow = make_flow(entries=[entry], context={"entry_id": entry.entry_id},
                             share_dir=tmp)
            saved = fakes.patch(config_flow,
                                async_get_clientsession=fakes.clientsession_for(session))
            try:
                result = await flow.async_step_reconfigure(
                    {"username": EMAIL, "password": "new"})
            finally:
                fakes.restore(config_flow, saved)
                unpatch(flow)
        assert result["type"] == "abort" and result["reason"] == "reconfigure_successful", (
            f"a bridge answering {script!r} failed the flow: {result!r} -- HA's "
            "own successful login is the sole determinant; the fallback file "
            "already carries the credentials")


TESTS = (
    test_user_step_success_creates_entry,
    test_submitted_credentials_are_trimmed,
    test_login_failures_map_to_their_error_keys,
    test_must_enable_2fa_aborts,
    test_each_retry_closes_the_previous_controllers_session,
    test_otp_app_only_skips_method_selection,
    test_otp_email_is_requested_then_submitted,
    test_otp_submit_failures,
    test_reauth_merges_data_and_keeps_the_clip_link_secret,
    test_reauth_refuses_a_different_account,
    test_first_reauth_after_upgrade_tolerates_missing_unique_id,
    test_duplicate_account_aborts,
    test_reconfigure_maps_errors_and_merges_on_success,
    test_options_flow_clears_code_and_merges,
    test_call_settings_step_writes_the_fallback_and_pushes,
    test_call_settings_step_survives_an_unreachable_bridge,
    test_fallback_file_is_private_and_atomic,
    test_fallback_write_failure_never_raises,
    test_credential_push_reaches_the_bridge_with_the_secret,
    test_credential_push_failures_never_fail_the_flow,
)


async def main():
    failures = 0
    for test in TESTS:
        try:
            await test()
            print(f"  ok   {test.__name__}")
        except AssertionError as exc:
            print(f"  FAIL {test.__name__}: {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {test.__name__}: {exc!r}")
            failures += 1
    print("\nRESULT:", "PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
