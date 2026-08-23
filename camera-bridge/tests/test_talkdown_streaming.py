"""Tests for the talkdown transport's streaming encode, pacing, in-place
recovery, certificate pinning and endpoint selection (backlog E5/E6/E7/E10/E11/E12).

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_talkdown_streaming.py

No network and no real doorbell: every test stands up a local fake doorbell that
enforces real HTTP Digest (MD5/qop=auth), on loopback. Requires ffmpeg on PATH
(the transport shells out to it to encode the clip).

The Digest verifier and the clip builder are shared with
test_talkdown_renegotiation.py rather than copied, so there is one definition of
"a doorbell that actually checks the auth".

Scenarios:
  1. pacing         -- THE regression test for the load-bearing design. A paced
                       run against a doorbell with realistic per-PUT latency must
                       finish in audio time, not audio time PLUS one round trip
                       per chunk (the naive `send; sleep(CHUNK_MS)` form, which
                       drifted ~10% live). Asserted for BOTH encode modes.
  2. equivalence    -- streaming and buffered encode put byte-identical audio on
                       the wire, in identically-sized chunks.
  3. encoder death  -- an encoder that dies is fatal and is NEVER renegotiated
                       (a fresh doorbell session cannot fix ffmpeg), mid-stream
                       and at startup.
  4. stale nonce    -- a doorbell that rotates its nonce and answers stale=true
                       is handled by aiohttp's DigestAuthMiddleware alone: the
                       clip still arrives whole, with no reconnect and no
                       renegotiation. Pins the E7 finding.
  5. auth/keepalive -- a 401 the middleware cannot recover from, and a connection
                       dropped more than aiohttp's own retry covers, are fixed by
                       an in-place reconnect on the SAME credentials rather than
                       by burning a renegotiation. Also pins that a SINGLE dropped
                       keep-alive never reaches this transport at all.
  6. cert pin       -- against a real self-signed HTTPS doorbell: the right pin
                       streams, a wrong pin warns-and-streams by default, and
                       only enforce mode refuses -- fast, once, with a message
                       naming the fix.
  7. endpoints      -- reachability picks a live endpoint over a dead preferred
                       one, the static override wins, a bogus override degrades
                       to detection, and nothing reachable still returns a URL.
  8. measurement    -- the E6 latency/lateness numbers are reported, the opening
                       prebuffer burst is not miscounted as lateness, and the
                       adaptive lead only ever raises.
"""
import asyncio
import hashlib
import os
import pathlib
import ssl
import sys
import uuid

from aiohttp import web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import config as c            # noqa: E402
from bridge import talk_fullduplex as tf  # noqa: E402
from test_talkdown_renegotiation import (  # noqa: E402
    REALM, USER, _h, _make_clip, _parse_auth,
)


class Doorbell:
    """A fake PutAudioStream endpoint with the failure modes under test.

    Every knob is one-shot or counted, so a test says "misbehave once, at this
    point in the clip" and then asserts the whole clip still arrived.
    """

    def __init__(self, password="pw", *, latency_s=0.0, rotate_nonce_every=None,
                 bare_401_at=None, drop_conn_at=None, drop_conn_count=1):
        self.password = password
        self.latency_s = latency_s
        # Rotate the nonce every N authenticated requests and answer the old one
        # with `stale=true` -- what a doorbell with a short nonce lifetime does.
        self.rotate_nonce_every = rotate_nonce_every
        # Answer one 401 with NO WWW-Authenticate at this byte offset: the
        # middleware has nothing to re-challenge from, so it surfaces the 401.
        self.bare_401_at = bare_401_at
        # Abort the TCP connection at this byte offset, this many times in a row
        # (a dropped keep-alive). One drop is absorbed a layer below us -- see
        # test_reconnect_beats_renegotiation -- so the count matters.
        self.drop_conn_at = drop_conn_at
        self.drop_conn_count = drop_conn_count
        self.drops = 0
        self.received = bytearray()
        self.chunk_sizes = []
        self.nonce = uuid.uuid4().hex
        self.stale_challenges = 0
        self._authed = 0
        self._fired = set()

    def _challenge(self, stale=False):
        params = [f'realm="{REALM}"', f'nonce="{self.nonce}"', 'qop="auth"',
                  "algorithm=MD5"]
        if stale:
            params.append("stale=true")
            self.stale_challenges += 1
        return web.Response(status=401,
                            headers={"WWW-Authenticate": "Digest " + ", ".join(params)})

    async def handle(self, request):
        if self.latency_s:
            await asyncio.sleep(self.latency_s)

        if self.drop_conn_at is not None and self.drops < self.drop_conn_count \
                and len(self.received) >= self.drop_conn_at:
            self.drops += 1
            request.transport.abort()
            return web.Response(status=500)

        auth = _parse_auth(request.headers.get("Authorization", ""))
        if not auth:
            return self._challenge()
        if auth.get("nonce") != self.nonce:
            # Signed against a nonce we have retired -> stale, here is a new one.
            return self._challenge(stale=True)
        ha1 = _h(USER, REALM, self.password)
        ha2 = _h("PUT", auth.get("uri", ""))
        expected = _h(ha1, auth.get("nonce", ""), auth.get("nc", ""),
                      auth.get("cnonce", ""), auth.get("qop", "auth"), ha2)
        if auth.get("response") != expected:
            return self._challenge()

        body = await request.read()

        if self.bare_401_at is not None and "bare401" not in self._fired \
                and len(self.received) >= self.bare_401_at:
            self._fired.add("bare401")
            # A 401 with no challenge to parse: DigestAuthMiddleware gives up on
            # it (its _authenticate returns False), so the transport sees it.
            return web.Response(status=401, text="no")

        self._authed += 1
        if self.rotate_nonce_every and self._authed % self.rotate_nonce_every == 0:
            self.nonce = uuid.uuid4().hex

        self.received.extend(body)
        self.chunk_sizes.append(len(body))
        return web.json_response({"ok": True})


async def _serve(doorbell, port, ssl_context=None):
    app = web.Application()
    app.router.add_put("/{name}", doorbell.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port, ssl_context=ssl_context).start()
    return runner


class _Mode:
    """Temporarily flip a config knob (these are plain module attributes)."""

    def __init__(self, **kw):
        self._kw = kw
        self._old = {}

    def __enter__(self):
        for k, v in self._kw.items():
            self._old[k] = getattr(c, k)
            setattr(c, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self._old.items():
            setattr(c, k, v)


# ---------------------------------------------------------------------------
# 1. pacing -- the guarantee that must survive every change in this file
# ---------------------------------------------------------------------------
async def test_pacing(tmp):
    """A paced stream finishes in AUDIO time, not audio time + a round trip per
    chunk.

    The doorbell is given 15 ms of per-PUT latency on purpose. On loopback a
    naive `send; sleep(CHUNK_MS)` implementation looks fine because the round
    trip is microseconds; with a realistic one it would take
    clip + chunks*15ms -- for a 6 s clip, 150 chunks, that is ~8.25 s versus the
    ~5.5 s the schedule-based form takes (clip minus the prebuffer lead). The
    assertion sits between those two numbers, so a regression to sleep-after-send
    fails here instead of on the doorbell.
    """
    seconds = 6
    src = await _make_clip(f"{tmp}/pace.wav", seconds=seconds)
    lead_s = c.TALK_PREBUFFER_MS / 1000.0
    naive_s = seconds + (seconds * 1000.0 / c.TALK_CHUNK_MS) * 0.015

    for streaming, port in ((True, 8781), (False, 8782)):
        doorbell = Doorbell(latency_s=0.015)
        runner = await _serve(doorbell, port)
        try:
            with _Mode(TALK_STREAMING_ENCODE=streaming):
                loop = asyncio.get_running_loop()
                t0 = loop.time()
                result = await tf.stream_talkdown(
                    f"http://127.0.0.1:{port}/s1", "pw", src, username=USER,
                    codec="mulaw", audio_filter="", pace=True)
                elapsed = loop.time() - t0
        finally:
            await runner.cleanup()

        expected = seconds - lead_s
        assert result["stopped"] is False, result
        assert bytes(doorbell.received) == await tf._encode(src, "mulaw", ""), \
            "paced run did not deliver the clip intact"
        assert expected - 0.6 <= elapsed <= expected + 1.0, (
            f"{'streaming' if streaming else 'buffered'} paced run took "
            f"{elapsed:.2f}s; expected ~{expected:.2f}s (audio length minus the "
            f"{c.TALK_PREBUFFER_MS}ms lead). sleep-after-send would take ~{naive_s:.1f}s")
        assert elapsed < naive_s - 1.0, (
            f"took {elapsed:.2f}s, which is in sleep-after-each-PUT territory "
            f"(~{naive_s:.1f}s) rather than schedule-based pacing")
        print(f"  ok  pacing ({'streaming' if streaming else 'buffered'}): "
              f"{seconds}s of audio in {elapsed:.2f}s wall "
              f"(schedule target {expected:.2f}s, sleep-after-send would be ~{naive_s:.1f}s)")


# ---------------------------------------------------------------------------
# 2. streaming == buffered
# ---------------------------------------------------------------------------
async def test_streaming_matches_buffered(tmp):
    """Both encode modes put the same bytes on the wire, in the same chunks."""
    src = await _make_clip(f"{tmp}/equiv.wav", seconds=4)
    reference = await tf._encode(src, "mulaw", "")
    out = {}
    for streaming, port in ((True, 8783), (False, 8784)):
        doorbell = Doorbell()
        runner = await _serve(doorbell, port)
        try:
            with _Mode(TALK_STREAMING_ENCODE=streaming):
                result = await tf.stream_talkdown(
                    f"http://127.0.0.1:{port}/s1", "pw", src, username=USER,
                    codec="mulaw", audio_filter="", pace=False)
        finally:
            await runner.cleanup()
        out[streaming] = (bytes(doorbell.received), doorbell.chunk_sizes, result)

    stream_bytes, stream_chunks, stream_result = out[True]
    buf_bytes, buf_chunks, buf_result = out[False]
    assert stream_bytes == reference, "streaming encode changed the audio"
    assert buf_bytes == reference, "buffered encode changed the audio"
    assert stream_chunks == buf_chunks, (
        "streaming produced different chunk SIZES; the doorbell has only ever "
        "been fed whole CHUNK_MS frames")
    chunk_bytes = tf.SAMPLE_RATE * c.TALK_CHUNK_MS // 1000
    assert set(stream_chunks[:-1]) == {chunk_bytes}, stream_chunks[:5]
    assert stream_result["seconds"] == buf_result["seconds"], (stream_result, buf_result)
    assert stream_result["encode"] == "streaming" and buf_result["encode"] == "buffered"
    print(f"  ok  equivalence: {len(reference)} bytes in {len(stream_chunks)} chunks, "
          f"byte-identical and chunk-identical across both encode modes")


# ---------------------------------------------------------------------------
# 3. an encoder that dies is never "fixed" by a new doorbell session
# ---------------------------------------------------------------------------
async def test_encoder_failure_is_not_renegotiated(tmp):
    """ffmpeg dying is fatal, at startup and mid-stream, and never renegotiated."""
    doorbell = Doorbell()
    runner = await _serve(doorbell, 8785)
    swaps = {"n": 0}

    async def renegotiate():
        swaps["n"] += 1
        return {"put_url": "http://127.0.0.1:8785/s2", "password": "pw", "username": USER}

    try:
        with _Mode(TALK_STREAMING_ENCODE=True):
            try:
                await tf.stream_talkdown(
                    "http://127.0.0.1:8785/s1", "pw", f"{tmp}/does-not-exist.wav",
                    username=USER, codec="mulaw", audio_filter="", pace=False,
                    renegotiate=renegotiate)
                raise AssertionError("expected an unusable source to fail the talk")
            except tf.TalkdownError:
                pass
        assert swaps["n"] == 0, (
            "an encode failure must not spend a renegotiation -- a fresh doorbell "
            "session cannot make ffmpeg produce audio")
        assert not doorbell.received, "nothing should have been sent"
    finally:
        await runner.cleanup()

    # Mid-stream death: a stand-in "encoder" that emits a second of audio and
    # then exits non-zero, which is what a truncated/corrupt input looks like.
    chunk_bytes = tf.SAMPLE_RATE * c.TALK_CHUNK_MS // 1000
    src_obj = tf._StreamingSource(
        ["sh", "-c", "head -c 8000 /dev/zero; exit 3"], tf.SAMPLE_RATE, chunk_bytes)
    await src_obj.start()
    try:
        got = 0
        err = None
        try:
            while True:
                chunk = await src_obj.next_chunk(chunk_bytes)
                if not chunk:
                    break
                got += len(chunk)
        except tf.TalkdownError as e:
            err = e
        assert got == 8000, f"decoded audio should still be delivered, got {got}"
        assert err is not None and "exited 3" in str(err), err
    finally:
        await src_obj.aclose()
    print("  ok  encoder death: unusable source fails without renegotiating; a "
          "mid-stream exit yields the decoded audio then fails with the exit code")


# ---------------------------------------------------------------------------
# 4. E7 -- a stale nonce never reaches our code
# ---------------------------------------------------------------------------
async def test_stale_nonce_is_handled_by_the_middleware(tmp):
    """A doorbell rotating its nonce (401 stale=true) costs nothing.

    Pins what reading aiohttp 3.14.3's DigestAuthMiddleware showed: it does not
    inspect `stale` at all -- any 401 carrying a Digest challenge replaces its
    cached challenge and retries the request once, with `nc` restarting at 1,
    which is exactly the right answer to stale=true. So the clip must arrive
    whole with NO reconnect and NO renegotiation.
    """
    doorbell = Doorbell(rotate_nonce_every=10)
    runner = await _serve(doorbell, 8786)
    src = await _make_clip(f"{tmp}/stale.wav", seconds=3)
    swaps = {"n": 0}

    async def renegotiate():
        swaps["n"] += 1
        return {"put_url": "http://127.0.0.1:8786/s1", "password": "pw", "username": USER}

    try:
        result = await tf.stream_talkdown(
            "http://127.0.0.1:8786/s1", "pw", src, username=USER, codec="mulaw",
            audio_filter="", pace=False, renegotiate=renegotiate)
        assert bytes(doorbell.received) == await tf._encode(src, "mulaw", ""), \
            "nonce rotation lost or duplicated audio"
        assert doorbell.stale_challenges > 5, (
            f"the doorbell only issued {doorbell.stale_challenges} stale challenges "
            "-- the test is not exercising the path")
        assert result["renegotiations"] == 0 and result["reconnects"] == 0, result
        assert swaps["n"] == 0
        print(f"  ok  stale nonce: {doorbell.stale_challenges} stale=true challenges "
              "absorbed by the Digest middleware, clip intact, 0 reconnects, 0 renegotiations")
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# 5. E7/E11 -- in-place reconnect instead of a renegotiation
# ---------------------------------------------------------------------------
async def test_reconnect_beats_renegotiation(tmp):
    """An unrecoverable 401 and a repeatedly-dropped connection are both fixed on
    the spot, on the SAME credentials.

    Both cases keep working credentials, so spending a (bounded) renegotiation --
    an MWS teardown, a re-discovery and a re-arm, seconds of dead air -- on them
    would be wasteful and could leave none for a session that really expired.

    The dropped-connection case also pins WHERE that is handled. aiohttp itself
    already retries an idempotent request once when a persistent connection is
    closed under it (client.py, `retry_persistent_connection`; PUT is idempotent),
    so a SINGLE drop never reaches this transport at all. What the reconnect
    covers is the drop that outlives that retry -- asserted both ways below, so
    if a future aiohttp drops its retry the layering change is visible here.
    """
    src = await _make_clip(f"{tmp}/reconnect.wav", seconds=4)
    reference = await tf._encode(src, "mulaw", "")

    cases = (
        ("401 with no challenge", 8787, {"bare_401_at": 6000}, 1),
        ("single dropped keep-alive", 8788, {"drop_conn_at": 6000, "drop_conn_count": 1}, 0),
        ("repeated dropped keep-alive", 8793,
         {"drop_conn_at": 6000, "drop_conn_count": 2}, 1),
    )
    for label, port, kwargs, want_reconnects in cases:
        doorbell = Doorbell(**kwargs)
        runner = await _serve(doorbell, port)
        swaps = {"n": 0}

        async def renegotiate():
            swaps["n"] += 1
            return {"put_url": f"http://127.0.0.1:{port}/s1", "password": "pw",
                    "username": USER}

        try:
            result = await tf.stream_talkdown(
                f"http://127.0.0.1:{port}/s1", "pw", src, username=USER,
                codec="mulaw", audio_filter="", pace=False, renegotiate=renegotiate)
            assert bytes(doorbell.received) == reference, (
                f"{label}: recovery lost or repeated audio "
                f"({len(doorbell.received)} vs {len(reference)} bytes)")
            assert result["reconnects"] == want_reconnects, (label, result)
            assert result["renegotiations"] == 0 and swaps["n"] == 0, (
                f"{label}: recovered by renegotiating when a reconnect would do")
            how = ("absorbed by aiohttp's own persistent-connection retry"
                   if want_reconnects == 0
                   else f"recovered in place ({want_reconnects} reconnect, 0 renegotiations)")
            print(f"  ok  {label}: {how}, clip byte-identical to a clean encode")
        finally:
            await runner.cleanup()


def test_recovery_classification():
    """Which failures are retried in place, and -- more importantly -- which are not.

    The trap this pins: aiohttp models ServerTimeoutError as a SUBCLASS of
    ClientConnectionError, so a naive "is it a connection error?" test would send
    read timeouts down the reconnect path. Each of those costs a full sock_read
    timeout, so two of them is ten seconds of silence mid-announcement before the
    renegotiation that was needed all along even starts.
    """
    import aiohttp as _a
    reconnectable = [
        tf._AuthStale("401"),
        _a.ServerDisconnectedError(),
        _a.ClientOSError(104, "reset"),
    ]
    not_reconnectable = [
        _a.ServerTimeoutError("read"),
        asyncio.TimeoutError(),
        tf.TalkdownError("PUT -> HTTP 500"),
        tf._EncodeFailed("ffmpeg died"),
        tf._CertPinMismatch("bad cert"),
        tf._DeadlineExceeded("too long"),
    ]
    for err in reconnectable:
        assert tf._reconnectable(err), f"{type(err).__name__} should reconnect in place"
    for err in not_reconnectable:
        assert not tf._reconnectable(err), (
            f"{type(err).__name__} must NOT be retried on the same session")
    assert isinstance(_a.ServerTimeoutError("x"), _a.ClientConnectionError), (
        "aiohttp no longer models a server timeout as a connection error -- the "
        "explicit timeout check in _reconnectable may now be redundant")
    print("  ok  classification: resets and unrecoverable 401s reconnect in place; "
          "timeouts (including aiohttp's connection-error-flavoured ones), 5xx, "
          "encoder deaths and pin mismatches do not")


# ---------------------------------------------------------------------------
# 6. E12 -- certificate pinning against a real self-signed HTTPS doorbell
# ---------------------------------------------------------------------------
def _self_signed(tmp):
    """Mint a throwaway self-signed cert, like the doorbell's own."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-doorbell")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cert_path, key_path = f"{tmp}/dev.crt", f"{tmp}/dev.key"
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    der = cert.public_bytes(serialization.Encoding.DER)
    return cert_path, key_path, hashlib.sha256(der).hexdigest()


async def test_cert_pin(tmp):
    """The right pin streams; a wrong pin warns by default and only refuses in
    enforce mode -- fast, and without burning recovery attempts."""
    cert_path, key_path, fingerprint = _self_signed(tmp)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    doorbell = Doorbell()
    runner = await _serve(doorbell, 8789, ssl_context=ctx)
    src = await _make_clip(f"{tmp}/pin.wav", seconds=2)
    reference = await tf._encode(src, "mulaw", "")
    url = "https://127.0.0.1:8789/s1"
    wrong = "00" * 32

    async def run(pin_hex, mode, **kw):
        with _Mode(TALK_DEVICE_CERT_SHA256=pin_hex, TALK_TLS_PIN_MODE=mode):
            pin = tf.make_cert_pin("direct")
            return pin, await tf.stream_talkdown(
                url, "pw", src, username=USER, codec="mulaw", audio_filter="",
                pace=False, cert_pin=pin, **kw)

    try:
        # The device's real certificate, enforced.
        pin, result = await run(fingerprint, "enforce")
        assert bytes(doorbell.received) == reference
        assert pin.observed == fingerprint and not pin.mismatch
        assert result["cert_pinned"] is True, result

        # A wrong pin in the default mode: detected, reported, still plays. This
        # is the direction that matters -- a firmware update that legitimately
        # changes the certificate must not silence the doorbell.
        doorbell.received.clear()
        pin, result = await run(wrong, "warn")
        assert bytes(doorbell.received) == reference, "warn mode must not block audio"
        assert pin.mismatch and pin.observed == fingerprint
        assert result["cert_pinned"] is False, result

        # A wrong pin in enforce mode: refused, immediately, and NOT retried --
        # every reconnect would meet the same certificate.
        doorbell.received.clear()
        swaps = {"n": 0}

        async def renegotiate():
            swaps["n"] += 1
            return {"put_url": url, "password": "pw", "username": USER}

        try:
            await run(wrong, "enforce", renegotiate=renegotiate)
            raise AssertionError("enforce mode must refuse a mismatched certificate")
        except tf.TalkdownError as e:
            assert "TALK_DEVICE_CERT_SHA256" in str(e) and "TALK_TLS_PIN_MODE=warn" in str(e), (
                f"the failure must name the fix, got: {e}")
        assert swaps["n"] == 0, "a pin mismatch must not be retried"
        assert not doorbell.received

        # The relay is a different host with its own CA-signed certificate, so
        # the device's fingerprint must never be applied to it.
        with _Mode(TALK_DEVICE_CERT_SHA256=fingerprint, TALK_TLS_PIN_MODE="enforce"):
            assert tf.make_cert_pin("cloud") is None
            assert tf.make_cert_pin("direct") is not None
        with _Mode(TALK_TLS_PIN_MODE="off"):
            assert tf.make_cert_pin("direct") is None
        print("  ok  cert pin: correct pin streams over the self-signed cert; a wrong "
              "pin warns-and-plays by default, refuses once (never retried) under "
              "enforce, and is never applied to the cloud relay")
    finally:
        await runner.cleanup()


async def test_cert_pin_degrades_without_a_pin(tmp):
    """No pin configured, and a garbage pin, both degrade to learn-and-report."""
    class _Sock:
        def __init__(self, der):
            self._der = der

        def getpeercert(self, binary_form=True):
            return self._der

    class _Transport:
        def __init__(self, der):
            self._sock = _Sock(der)

        def get_extra_info(self, key):
            return {"sslcontext": True, "ssl_object": self._sock,
                    "peername": ("10.0.0.1", 443)}.get(key)

    der = b"pretend-certificate"
    transport = _Transport(der)

    with _Mode(TALK_DEVICE_CERT_SHA256="", TALK_TLS_PIN_MODE="enforce"):
        pin = tf.make_cert_pin("direct")
        pin.check(transport)          # must NOT raise: nothing to enforce against
        assert pin.observed == hashlib.sha256(der).hexdigest()
        assert not pin.mismatch

    with _Mode(TALK_DEVICE_CERT_SHA256="not-hex", TALK_TLS_PIN_MODE="enforce"):
        pin = tf.make_cert_pin("direct")
        pin.check(transport)          # must NOT raise on a malformed knob either
        assert not pin.mismatch
    print("  ok  cert pin degradation: an unset or malformed pin reports the observed "
          "fingerprint and lets the talk through, even in enforce mode")


# ---------------------------------------------------------------------------
# 7. E10 -- endpoint reachability
# ---------------------------------------------------------------------------
async def test_endpoint_selection():
    """Reachability beats a blind walk down the preference order."""
    from bridge.talk import TalkManager

    mgr = TalkManager.__new__(TalkManager)          # no session/auth needed
    doorbell = Doorbell()
    runner = await _serve(doorbell, 8790)
    # 9 is discard/unassigned on loopback: nothing will ever answer there.
    dead = "https://127.0.0.1:9/api/PutAudioStream"
    live = "http://127.0.0.1:8790/api/PutAudioStream"
    try:
        record = {"put_urls": {"direct": dead, "local": live,
                               "public": dead, "cloud": dead}}
        kind, url = await mgr._select_put_url(record)
        assert (kind, url) == ("local", live), (kind, url)

        # The preferred endpoint answering wins, as before.
        record2 = {"put_urls": {"direct": live, "local": dead}}
        assert (await mgr._select_put_url(record2))[0] == "direct"

        # Static override wins outright, even over reachability.
        with _Mode(TALK_PUT_ENDPOINT="public"):
            assert (await mgr._select_put_url(record)) == ("public", dead)
        # An override this doorbell does not advertise degrades to detection.
        with _Mode(TALK_PUT_ENDPOINT="nonsense"):
            assert (await mgr._select_put_url(record))[0] == "local"
        # Probing off -> the pure static walk.
        with _Mode(TALK_ENDPOINT_PROBE_TIMEOUT_S=0):
            assert (await mgr._select_put_url(record))[0] == "direct"
        # Nothing reachable still returns the preferred URL, so the PUT gets to
        # produce the real error rather than detection inventing one.
        allergic = {"put_urls": {"direct": dead, "cloud": dead}}
        assert (await mgr._select_put_url(allergic))[0] == "direct"
        assert await mgr._select_put_url({"put_urls": {}}) == (None, None)
        print("  ok  endpoints: a dead preferred endpoint is skipped for a live one; "
              "override wins; a bogus override, probing-off and nothing-reachable all "
              "degrade to the static order")
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# 8. E6 -- measurement, and the opt-in adaptive lead
# ---------------------------------------------------------------------------
async def test_prebuffer_measurement(tmp):
    """The jitter numbers are reported, and adaptation only ever raises the lead."""
    src = await _make_clip(f"{tmp}/jitter.wav", seconds=6)

    doorbell = Doorbell(latency_s=0.005)
    runner = await _serve(doorbell, 8791)
    try:
        result = await tf.stream_talkdown(
            "http://127.0.0.1:8791/s1", "pw", src, username=USER, codec="mulaw",
            audio_filter="", pace=True)
        for key in ("put_rtt_p50_ms", "put_rtt_p95_ms", "put_rtt_max_ms",
                    "late_chunks", "max_late_ms", "prebuffer_ms"):
            assert key in result, f"{key} missing from {result}"
        assert result["put_rtt_p50_ms"] >= 5.0, result
        # The opening burst that fills the lead is by design, not lateness. If it
        # were counted, EVERY talk would report ~lead/CHUNK_MS late chunks; a real
        # loop hiccup or two is fine and expected on a shared runner.
        lead_chunks = c.TALK_PREBUFFER_MS // c.TALK_CHUNK_MS
        assert result["late_chunks"] < lead_chunks, (
            f"a healthy 5ms-latency run reported {result['late_chunks']} late chunks, "
            f"about what the {lead_chunks}-chunk prebuffer fill would contribute "
            "-- the fill is being miscounted as lateness")
        assert result["max_late_ms"] < c.TALK_PREBUFFER_MS, (
            "a chunk was later than the whole jitter-buffer lead, which would be "
            f"audible: {result['max_late_ms']}ms")
        assert result["prebuffer_ms"] == c.TALK_PREBUFFER_MS, result
        baseline_p95 = result["put_rtt_p95_ms"]
    finally:
        await runner.cleanup()

    # Adaptive: the lead may rise to cover the worst round trip, never fall.
    doorbell = Doorbell(latency_s=0.005)
    runner = await _serve(doorbell, 8792)
    try:
        with _Mode(TALK_PREBUFFER_ADAPTIVE=True):
            adapted = await tf.stream_talkdown(
                "http://127.0.0.1:8792/s1", "pw", src, username=USER, codec="mulaw",
                audio_filter="", pace=True)
        assert adapted["prebuffer_ms"] >= c.TALK_PREBUFFER_MS, (
            "the adaptive lead must never go below the by-ear-tuned value")
        assert adapted["prebuffer_ms"] <= c.TALK_PREBUFFER_MAX_MS
        assert bytes(doorbell.received) == await tf._encode(src, "mulaw", "")
        print(f"  ok  measurement: p95={baseline_p95:.1f}ms reported, no prebuffer-fill "
              f"miscount on a "
              f"healthy run, adaptive lead stayed within "
              f"[{c.TALK_PREBUFFER_MS},{c.TALK_PREBUFFER_MAX_MS}]ms "
              f"(={adapted['prebuffer_ms']}ms)")
    finally:
        await runner.cleanup()


async def main():
    import tempfile
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tests = [test_pacing, test_streaming_matches_buffered,
                 test_encoder_failure_is_not_renegotiated,
                 test_stale_nonce_is_handled_by_the_middleware,
                 test_reconnect_beats_renegotiation, test_cert_pin,
                 test_cert_pin_degrades_without_a_pin,
                 test_endpoint_selection, test_prebuffer_measurement]
        sync_tests = [test_recovery_classification]
        for test in sync_tests:
            try:
                test()
            except AssertionError as exc:
                print(f"  FAIL {test.__name__}: {exc}")
                failures += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ERROR {test.__name__}: {exc!r}")
                failures += 1
        for test in tests:
            try:
                if test is test_endpoint_selection:
                    await test()
                else:
                    await test(tmp)
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
