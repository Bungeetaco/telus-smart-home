"""Low-latency in-process audio shaping for a live call (backlog E99).

Both call directions want clarity: the caller's voice into a small doorbell
speaker, and a soft outdoor visitor into a phone. The talkdown clip path does
this with an ffmpeg -af chain, but that is a one-shot pass over a whole clip; a
LIVE call is a continuous 8 kHz mono stream where a persistent ffmpeg
filtergraph would add pipe latency and deadlock risk for no benefit at this
tiny rate. So the shaping is done here, per sample, with no lookahead:

  * a one-pole high-pass removes rumble/wind and DC the small speaker cannot
    reproduce anyway (downlink) and that muddies phone speech (uplink);
  * a soft AGC brings quiet speech up toward a target and rides gain DOWN on
    peaks so nothing clips, with fast attack and slow release so it does not
    pump on every syllable.

GENERIC and TUNABLE (see config.CALL_*): every value is a knob, and a Shaper
with highpass_hz=0 and gain bounds [1, 1] is exact passthrough. Nothing here is
specific to one device -- the defaults suit a typical doorbell speaker and mic,
and a deployment with different hardware just changes the knobs.

Operates on little-endian s16 mono PCM (what the RTSP listen decodes to and
what the mu-law encoder consumes), so it drops into the live path on either
side with no format change.
"""

import math


class Shaper:
    """Per-sample high-pass + soft AGC/limiter for one direction of a call.

    Stateful across calls (filter memory and the AGC's running gain persist), so
    one Shaper instance serves one stream for the life of the call. Cheap: a
    handful of float ops per 8 kHz sample.
    """

    def __init__(self, *, sample_rate: int = 8000, highpass_hz: float = 0.0,
                 agc_target: float = 0.5, agc_max_gain: float = 1.0,
                 agc_min_gain: float = 1.0,
                 attack_ms: float = 5.0, release_ms: float = 200.0,
                 gate_threshold: float = 0.0, gate_atten: float = 0.15,
                 gate_release_ms: float = 150.0):
        self.sample_rate = sample_rate
        # One-pole high-pass coefficient. highpass_hz<=0 disables it.
        if highpass_hz and highpass_hz > 0:
            rc = 1.0 / (2 * math.pi * highpass_hz)
            dt = 1.0 / sample_rate
            self._hp_a = rc / (rc + dt)
            self._hp_on = True
        else:
            self._hp_a = 0.0
            self._hp_on = False
        self._hp_prev_in = 0.0
        self._hp_prev_out = 0.0
        # AGC/limiter, in normalized [-1, 1] amplitude.
        self.target = agc_target
        self.max_gain = max(0.0, agc_max_gain)
        # NOT clamped to <=1: min_gain == max_gain == G is a FIXED gain of G --
        # no dynamics at all -- which is the simplest thing that can possibly
        # work for a quiet mic, and simplest is where tuning restarts after a
        # dynamics chain has been heard doing harm. (A fixed gain has no
        # limiter: the hard clip at full scale is the only ceiling. Size G so
        # the loudest expected input times G stays under 1.)
        self.min_gain = max(0.0, min(agc_min_gain, self.max_gain))
        self._gain = 1.0
        # Per-sample smoothing coefficients for gain moves (fast down, slow up).
        self._atk = math.exp(-1.0 / (sample_rate * attack_ms / 1000.0))
        self._rel = math.exp(-1.0 / (sample_rate * release_ms / 1000.0))
        # Noise gate (downward expander). An AGC alone amplifies whatever is
        # there when speech pauses -- on a porch that is wind and traffic, and a
        # live call measured the background riding UP to the target while the
        # visitor's voice sat below it. The gate tracks a fast-attack,
        # slow-release envelope; while it sits below gate_threshold the desired
        # gain is capped at gate_atten, so quiet stretches are pushed DOWN
        # instead of amplified. threshold<=0 disables the gate entirely.
        self.gate_threshold = max(0.0, gate_threshold)
        self.gate_atten = max(0.0, min(1.0, gate_atten))
        self._env = 0.0
        self._env_rel = math.exp(-1.0 / (sample_rate * gate_release_ms / 1000.0))
        self.passthrough = (not self._hp_on
                            and self.max_gain == 1.0 and self.min_gain == 1.0
                            and self.gate_threshold <= 0.0)

    def process(self, pcm: bytes) -> bytes:
        """Shape one chunk of s16le mono PCM, returning s16le mono PCM."""
        if self.passthrough or not pcm:
            return pcm
        n = len(pcm) // 2
        out = bytearray(n * 2)
        a = self._hp_a
        hp_on = self._hp_on
        pin, pout = self._hp_prev_in, self._hp_prev_out
        gain, target = self._gain, self.target
        maxg, ming = self.max_gain, self.min_gain
        atk, rel = self._atk, self._rel
        gate_thr, gate_att = self.gate_threshold, self.gate_atten
        env, env_rel = self._env, self._env_rel
        for i in range(n):
            s = int.from_bytes(pcm[2 * i:2 * i + 2], "little", signed=True)
            x = s / 32768.0
            if hp_on:
                # y[n] = a*(y[n-1] + x[n] - x[n-1])
                y = a * (pout + x - pin)
                pin, pout = x, y
                x = y
            # Desired gain to bring this sample's magnitude to the target,
            # clamped to the configured range; then smoothed (fast down, slow up)
            # so it acts as a soft AGC that also limits.
            mag = abs(x)
            desired = maxg if mag < 1e-6 else min(maxg, max(ming, target / mag))
            if gate_thr > 0.0:
                env = mag if mag > env else env * env_rel
                if env < gate_thr:
                    # Below the gate: attenuate, never amplify. Speech onset
                    # reopens instantly (the envelope attack is per-sample).
                    desired = min(desired, gate_att)
            coef = atk if desired < gain else rel
            gain = coef * gain + (1 - coef) * desired
            v = x * gain
            v = -1.0 if v < -1.0 else (1.0 if v > 1.0 else v)
            iv = int(v * 32767.0)
            out[2 * i] = iv & 0xFF
            out[2 * i + 1] = (iv >> 8) & 0xFF
        self._hp_prev_in, self._hp_prev_out = pin, pout
        self._gain = gain
        self._env = env
        return bytes(out)


def downlink_shaper(c) -> Shaper:
    """The caller -> doorbell-speaker shaper, from config."""
    return Shaper(highpass_hz=c.CALL_DOWNLINK_HIGHPASS_HZ,
                  agc_target=c.CALL_DOWNLINK_AGC_TARGET,
                  agc_max_gain=c.CALL_DOWNLINK_AGC_MAX_GAIN)


def uplink_shaper(c) -> Shaper:
    """The doorbell-mic -> caller shaper, from config. Gated: an outdoor mic's
    background must not be amplified to the AGC target during speech pauses."""
    return Shaper(highpass_hz=c.CALL_UPLINK_HIGHPASS_HZ,
                  agc_target=c.CALL_UPLINK_AGC_TARGET,
                  agc_max_gain=c.CALL_UPLINK_AGC_MAX_GAIN,
                  agc_min_gain=c.CALL_UPLINK_AGC_MIN_GAIN,
                  gate_threshold=c.CALL_UPLINK_GATE_THRESHOLD,
                  gate_atten=c.CALL_UPLINK_GATE_ATTEN)
