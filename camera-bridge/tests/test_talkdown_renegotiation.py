"""Tests for talkdown session renegotiation (talk_fullduplex.stream_talkdown).

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_talkdown_renegotiation.py

No network and no real doorbell: each test stands up a local fake doorbell that
enforces real HTTP Digest (MD5/qop=auth) and can be told to die mid-clip, which is
the failure this feature exists to survive. Requires ffmpeg on PATH (the transport
shells out to it to encode the clip).

Scenarios:
  1. resume  -- session dies mid-stream; a renegotiated session with a DIFFERENT
                password takes over. Asserts the bytes both sessions received,
                concatenated, are byte-identical to a clean single-pass encode:
                the resume happened at the exact offset, losing and repeating
                nothing.
  2. bounded -- every session dies. Asserts the transport gives up after exactly
                config.TALK_MAX_RENEGOTIATIONS swaps rather than looping.
  3. opt-in   -- with no renegotiate callback, a mid-stream failure stays fatal,
                so callers that don't opt in see no behaviour change.
  4. stop     -- setting the stop event mid-clip ends the stream promptly, at a
                chunk boundary, reporting partial stats rather than raising.
"""
import asyncio
import hashlib
import os
import pathlib
import re
import sys
import uuid

from aiohttp import web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import config as c            # noqa: E402
from bridge import talk_fullduplex as tf  # noqa: E402

REALM = "Alarm.com"
USER = "vieweruser"


def _h(*parts):
    return hashlib.md5(":".join(parts).encode()).hexdigest()


def _parse_auth(header):
    out = {}
    if not header or not header.lower().startswith("digest "):
        return out
    for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,]+))', header[7:]):
        out[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3).strip()
    return out


def _challenge():
    return web.Response(status=401, headers={
        "WWW-Authenticate":
            f'Digest realm="{REALM}", nonce="{uuid.uuid4().hex}", qop="auth", algorithm=MD5'})


class FakeDoorbell:
    """One endpoint per session name; each has its own password and byte budget."""

    def __init__(self, passwords, fail_after=None):
        self.passwords = passwords          # {session_name: password}
        # {session_name: bytes accepted before that session dies}. A session
        # absent from this map never dies -- which is what lets one test model
        # "the first session expires, its replacement is healthy".
        self.fail_after = fail_after or {}
        self.received = {}

    async def handle(self, request):
        name = request.match_info["name"]
        self.received.setdefault(name, bytearray())
        password = self.passwords.get(name)
        if password is None:
            return web.Response(status=404, text="no such session")

        auth = _parse_auth(request.headers.get("Authorization", ""))
        if not auth:
            return _challenge()
        # Verify against THIS session's password, so a renegotiated session only
        # succeeds if the client actually adopted the new credentials.
        ha1 = _h(USER, REALM, password)
        ha2 = _h("PUT", auth.get("uri", ""))
        expected = _h(ha1, auth.get("nonce", ""), auth.get("nc", ""),
                      auth.get("cnonce", ""), auth.get("qop", "auth"), ha2)
        if auth.get("response") != expected:
            return _challenge()

        body = await request.read()
        budget = self.fail_after.get(name)
        if budget is not None and len(self.received[name]) >= budget:
            return web.Response(status=500, text="session expired")
        self.received[name].extend(body)
        return web.json_response({"ok": True})


async def _serve(doorbell, port):
    app = web.Application()
    app.router.add_put("/{name}", doorbell.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


async def _make_clip(path, seconds=12):
    import shutil
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "these tests require ffmpeg on PATH -- the talkdown transport shells "
            "out to it to encode the clip (the bridge image ships it; install it "
            "locally or in CI)")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", f"sine=frequency=600:duration={seconds}", "-ac", "1", "-ar", "8000",
        "-c:a", "pcm_s16le", path)
    await proc.communicate()
    if not os.path.exists(path):
        raise RuntimeError("ffmpeg did not produce the test clip (is ffmpeg installed?)")
    return path


async def test_resume(tmp):
    """A dead session is replaced and the stream resumes at the exact offset."""
    doorbell = FakeDoorbell({"s1": "pw-one", "s2": "pw-two"}, fail_after={"s1": 8000})
    runner = await _serve(doorbell, 8771)
    src = await _make_clip(f"{tmp}/resume.wav")
    swaps = {"n": 0}

    async def renegotiate():
        swaps["n"] += 1
        return {"put_url": "http://127.0.0.1:8771/s2",
                "password": "pw-two", "username": USER}

    try:
        result = await tf.stream_talkdown(
            "http://127.0.0.1:8771/s1", "pw-one", src, username=USER,
            codec="mulaw", audio_filter="", pace=False, renegotiate=renegotiate)
        expected = await tf._encode(src, "mulaw", "")
        got = bytes(doorbell.received["s1"]) + bytes(doorbell.received.get("s2", b""))
        assert swaps["n"] == 1, f"expected 1 renegotiation, got {swaps['n']}"
        assert result["renegotiations"] == 1, result
        assert doorbell.received.get("s2"), "renegotiated session received nothing"
        assert got == expected, (
            f"stream mismatch: got {len(got)} bytes, expected {len(expected)} "
            "-- audio was lost, repeated or reordered")
        print(f"  ok  resume: session1={len(doorbell.received['s1'])}B "
              f"session2={len(doorbell.received['s2'])}B, reassembled stream is "
              "byte-identical to a clean encode")
    finally:
        await runner.cleanup()


async def test_bounded_and_opt_in(tmp):
    """Renegotiation is bounded, and is opt-in (no callback -> unchanged behaviour)."""
    names = [f"gen{i}" for i in range(6)] + ["solo"]
    # EVERY session dies here -- that is the point: renegotiation must stop.
    doorbell = FakeDoorbell({n: "pw" for n in names}, fail_after={n: 4000 for n in names})
    runner = await _serve(doorbell, 8772)
    src = await _make_clip(f"{tmp}/bounded.wav", seconds=10)
    swaps = {"n": 0}

    async def renegotiate():
        swaps["n"] += 1
        return {"put_url": f"http://127.0.0.1:8772/gen{swaps['n']}",
                "password": "pw", "username": USER}

    try:
        try:
            await tf.stream_talkdown("http://127.0.0.1:8772/gen0", "pw", src,
                                     username=USER, codec="mulaw", audio_filter="",
                                     pace=False, renegotiate=renegotiate)
            raise AssertionError("expected TalkdownError once renegotiations were exhausted")
        except tf.TalkdownError:
            assert swaps["n"] == c.TALK_MAX_RENEGOTIATIONS, (
                f"made {swaps['n']} swaps, expected {c.TALK_MAX_RENEGOTIATIONS}")
        print(f"  ok  bounded: gave up after exactly {swaps['n']} renegotiation(s) "
              f"(TALK_MAX_RENEGOTIATIONS={c.TALK_MAX_RENEGOTIATIONS})")

        try:
            await tf.stream_talkdown("http://127.0.0.1:8772/solo", "pw", src,
                                     username=USER, codec="mulaw", audio_filter="",
                                     pace=False)
            raise AssertionError("expected a mid-stream failure to stay fatal without a callback")
        except tf.TalkdownError:
            pass
        print("  ok  opt-in: without a callback a mid-stream failure is still fatal")
    finally:
        await runner.cleanup()


async def test_stop(tmp):
    """A stop event ends the stream promptly, with partial stats and no error."""
    doorbell = FakeDoorbell({"s1": "pw"})          # healthy: only the stop ends it
    runner = await _serve(doorbell, 8773)
    src = await _make_clip(f"{tmp}/stop.wav", seconds=20)
    stop_event = asyncio.Event()

    async def stop_soon():
        # Let a little audio through, then ask it to stop. Real time, because the
        # transport paces to real time here (pace=True) exactly as in production.
        await asyncio.sleep(1.0)
        stop_event.set()

    try:
        asyncio.get_running_loop().create_task(stop_soon())
        started = asyncio.get_running_loop().time()
        result = await tf.stream_talkdown(
            "http://127.0.0.1:8773/s1", "pw", src, username=USER, codec="mulaw",
            audio_filter="", pace=True, stop_event=stop_event)
        elapsed = asyncio.get_running_loop().time() - started

        assert result["stopped"] is True, result
        assert 0 < result["bytes"] < len(await tf._encode(src, "mulaw", "")), (
            f"expected a partial stream, got {result['bytes']} bytes")
        # Must react at a chunk boundary, not play out the remaining ~19s.
        assert elapsed < 5, f"stop took {elapsed:.1f}s -- should be near-immediate"
        assert result["played_seconds"] < result["seconds"], result
        print(f"  ok  stop: ended {elapsed:.2f}s in after "
              f"{result['played_seconds']}s of {result['seconds']}s audio "
              f"({result['bytes']} bytes), no exception")
    finally:
        await runner.cleanup()


async def main():
    import tempfile
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for test in (test_resume, test_bounded_and_opt_in, test_stop):
            try:
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
