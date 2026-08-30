"""ICEBOX core: ring buffer, crash trigger, crash-report codec, Iridium SBD framing.

Everything that would run ONBOARD the robot (Jetson Thor) lives here plus
telemetry.py. Nothing in this file may assume a network — it produces bytes.
"""
from __future__ import annotations

import math
import struct
import time
import zlib
from collections import deque
from dataclasses import dataclass, field

# ---------------------------------------------------------------- samples

@dataclass
class Sample:
    t: float                    # unix seconds
    quat: tuple = (1.0, 0.0, 0.0, 0.0)   # w, x, y, z (body -> world)
    gyro: tuple = (0.0, 0.0, 0.0)        # rad/s body frame
    accel: tuple = (0.0, 0.0, 1.0)       # g, body frame (specific force)
    joints: tuple = ()                    # optional joint positions, rad
    currents: tuple = ()                  # optional joint currents, A
    vbatt: float = 25.2                   # V
    temp: float = 21.0                    # C


class RingBuffer:
    """Fixed-duration ring of Samples. append() is O(1); snapshots copy."""

    def __init__(self, seconds: float = 60.0, nominal_hz: float = 200.0):
        self.buf: deque[Sample] = deque(maxlen=int(seconds * nominal_hz))
        self.nominal_hz = nominal_hz

    def append(self, s: Sample):
        self.buf.append(s)

    def window(self, t_start: float, t_end: float) -> list[Sample]:
        return [s for s in self.buf if t_start <= s.t <= t_end]

    @property
    def latest(self) -> Sample | None:
        return self.buf[-1] if self.buf else None

    def rate_hz(self) -> float:
        if len(self.buf) < 20:
            return self.nominal_hz
        span = self.buf[-1].t - self.buf[-20].t
        return 19.0 / span if span > 1e-6 else self.nominal_hz


# ---------------------------------------------------------------- trigger

def _mag(v) -> float:
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def tilt_deg(quat) -> float:
    """Angle between body 'up' (+z) and world up, from quaternion (w,x,y,z)."""
    w, x, y, z = quat
    # world z component of body z axis = 1 - 2(x^2 + y^2)
    cz = 1.0 - 2.0 * (x * x + y * y)
    cz = max(-1.0, min(1.0, cz))
    return math.degrees(math.acos(cz))


@dataclass
class TriggerState:
    armed: bool = True
    last_fire: float = 0.0
    baseline: deque = field(default_factory=lambda: deque(maxlen=1000))


class CrashTrigger:
    """Fires on impact (jerk + |a| spike), sustained tilt, or joint fault.

    Tuned so a shove/fall fires and walking/gait noise does not:
      - impact: |a| peak > peak_g AND jerk z-score > jerk_z
      - orientation: tilt > tilt_deg sustained > tilt_hold seconds
      - joints: |current| z-score > joint_z on any joint (arm mode)
    Debounced: one incident per `debounce` seconds.
    """

    def __init__(self, peak_g=3.0, jerk_z=8.0, tilt_limit=60.0, tilt_hold=0.3,
                 joint_z=7.0, debounce=6.0):
        self.peak_g, self.jerk_z = peak_g, jerk_z
        self.tilt_limit, self.tilt_hold = tilt_limit, tilt_hold
        self.joint_z, self.debounce = joint_z, debounce
        self.st = TriggerState()
        self._prev: Sample | None = None
        self._tilt_since: float | None = None
        self._joint_base: deque = deque(maxlen=1000)
        # orientation trigger re-arms only after the robot is upright again,
        # otherwise a fallen robot refires every debounce interval forever
        self._upright_since_fire = True

    def arm(self, on: bool):
        self.st.armed = on

    def check(self, s: Sample) -> str | None:
        """Returns a cause string when an incident fires, else None."""
        prev, self._prev = self._prev, s
        if not self.st.armed or (s.t - self.st.last_fire) < self.debounce:
            return None

        cause = None
        a = _mag(s.accel)

        # --- impact: jerk vs rolling baseline
        if prev is not None:
            dt = max(1e-3, s.t - prev.t)
            jerk = abs(a - _mag(prev.accel)) / dt          # g/s
            base = self.st.baseline
            if len(base) > 100:
                mean = sum(base) / len(base)
                var = sum((j - mean) ** 2 for j in base) / len(base)
                sd = math.sqrt(var) or 1e-6
                if a > self.peak_g and (jerk - mean) / sd > self.jerk_z:
                    cause = f"impact {a:.1f} g, jerk z={((jerk - mean) / sd):.0f}"
            base.append(jerk)

        # --- sustained tilt (fall ended lying down even without a hard hit)
        td = tilt_deg(s.quat)
        if td < 30.0:
            self._upright_since_fire = True
        if td > self.tilt_limit:
            if self._tilt_since is None:
                self._tilt_since = s.t
            elif (cause is None and self._upright_since_fire
                    and (s.t - self._tilt_since) > self.tilt_hold):
                cause = f"orientation loss, tilt {td:.0f} deg"
        else:
            self._tilt_since = None

        # --- joint fault (arm mode)
        if s.currents:
            cm = max(abs(c) for c in s.currents)
            base = self._joint_base
            if len(base) > 100 and cause is None:
                mean = sum(base) / len(base)
                sd = math.sqrt(sum((c - mean) ** 2 for c in base) / len(base)) or 1e-6
                if (cm - mean) / sd > self.joint_z:
                    j = max(range(len(s.currents)), key=lambda i: abs(s.currents[i]))
                    cause = f"joint {j} overcurrent {cm:.2f} A, z={((cm - mean) / sd):.0f}"
            base.append(cm)

        if cause:
            self.st.last_fire = s.t
            self._tilt_since = None
            self._upright_since_fire = False
        return cause


# ---------------------------------------------------------------- codec
#
# Crash report = per-tier "segments". Tier 1 is a ~25 Hz preview (lands in
# seconds over 2 kbps, replay auto-plays), tier 2 is the full-rate record
# that sharpens it. Each segment: quantized int16 channels, delta-encoded,
# zlib-compressed, split into SBD-sized packets.

# Per-tier quantization: tier 1 (preview) is coarse so its deltas are tiny
# and the whole segment lands in a handful of SBD packets; tier 2 keeps
# full fidelity. Scales are part of the protocol (looked up by tier).
SCALES = {
    1: {"quat": 1.0 / 400.0, "gyro": 35.0 / 127.0, "acc": 1.0 / 64.0},
    2: {"quat": 1.0 / 16000.0, "gyro": 1.0 / 512.0, "acc": 1.0 / 1024.0},
    3: {"quat": 1.0 / 400.0, "gyro": 35.0 / 127.0, "acc": 1.0 / 64.0},  # gap backfill
}
Q_JOINT = math.pi * 2 / 32767.0

SEG_MAGIC = b"IBXS"


def _q(v, scale):
    return max(-32767, min(32767, int(round(v / scale))))


def _delta(ints: list[int]) -> list[int]:
    out, prev = [], 0
    for v in ints:
        d = v - prev
        # wrap into int16 range for struct packing; unwrap on decode
        out.append(((d + 32768) & 0xFFFF) - 32768)
        prev = v
    return out


def _undelta(ints: list[int]) -> list[int]:
    out, acc = [], 0
    for d in ints:
        acc = ((acc + d + 32768) & 0xFFFF) - 32768
        out.append(acc)
    return out


def encode_segment(samples: list[Sample], tier: int, report_id: int) -> bytes:
    n = len(samples)
    nj = len(samples[0].joints) if samples and samples[0].joints else 0
    t0 = samples[0].t if samples else 0.0
    rate = (n - 1) / (samples[-1].t - t0) if n > 1 else 0.0

    sc = SCALES[tier]
    chans: list[list[int]] = []
    for k in range(4):
        chans.append([_q(s.quat[k], sc["quat"]) for s in samples])
    for k in range(3):
        chans.append([_q(s.gyro[k], sc["gyro"]) for s in samples])
    for k in range(3):
        chans.append([_q(s.accel[k], sc["acc"]) for s in samples])
    for k in range(nj):
        chans.append([_q(s.joints[k], Q_JOINT) for s in samples])

    body = b"".join(struct.pack(f"<{n}h", *_delta(c)) for c in chans)
    comp = zlib.compress(body, 9)
    head = SEG_MAGIC + struct.pack("<HBdfHB", report_id, tier, t0, rate, n, nj)
    return head + comp


def decode_segment(blob: bytes) -> dict:
    assert blob[:4] == SEG_MAGIC, "bad segment magic"
    report_id, tier, t0, rate, n, nj = struct.unpack_from("<HBdfHB", blob, 4)
    body = zlib.decompress(blob[4 + struct.calcsize("<HBdfHB"):])
    nch = 10 + nj
    assert len(body) == nch * n * 2, "segment size mismatch"
    chans = []
    for c in range(nch):
        raw = struct.unpack_from(f"<{n}h", body, c * n * 2)
        chans.append(_undelta(list(raw)))
    sc = SCALES[tier]
    scale = [sc["quat"]] * 4 + [sc["gyro"]] * 3 + [sc["acc"]] * 3 + [Q_JOINT] * nj
    series = [[v * scale[c] for v in chans[c]] for c in range(nch)]
    return {"report_id": report_id, "tier": tier, "t0": t0, "rate": rate,
            "n": n, "n_joints": nj,
            "quat": series[0:4], "gyro": series[4:7], "accel": series[7:10],
            "joints": series[10:]}


def decimate(samples: list[Sample], target_hz: float) -> list[Sample]:
    """Bin to target rate, keeping each bin's peak-|accel| sample so the
    preview never hides the impact spike the full-rate record will show."""
    if not samples:
        return []
    out: list[Sample] = []
    bin_end = samples[0].t + 1.0 / target_hz
    best = samples[0]
    for s in samples:
        if s.t >= bin_end:
            out.append(best)
            best = s
            bin_end = s.t + 1.0 / target_hz
        elif _mag(s.accel) > _mag(best.accel):
            best = s
    out.append(best)
    return out


# ---------------------------------------------------------------- SBD framing
#
# Real Iridium SBD mobile-originated messages max out at 340 bytes. Every
# packet that crosses the link respects that: 12-byte header + <=328 payload.

SBD_MAX = 340
PKT_MAGIC = b"SB"
PKT_HEAD = struct.Struct("<2sHBHHBH")   # magic, report, tier, seq, total, kind, crc
PAYLOAD_MAX = SBD_MAX - PKT_HEAD.size
KIND_SEG = 1

HB_MAGIC = b"HB"
HB_STRUCT = struct.Struct("<2sHIBHBHbHBH")  # magic seq t state accel_mg tilt vbatt_mV temp buffered pct crc


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def packetize(segment: bytes, report_id: int, tier: int) -> list[bytes]:
    chunks = [segment[i:i + PAYLOAD_MAX] for i in range(0, len(segment), PAYLOAD_MAX)]
    total = len(chunks)
    pkts = []
    for seq, chunk in enumerate(chunks):
        head = PKT_HEAD.pack(PKT_MAGIC, report_id, tier, seq, total, KIND_SEG, crc16(chunk))
        pkts.append(head + chunk)
    assert all(len(p) <= SBD_MAX for p in pkts)
    return pkts


class Reassembler:
    """Ground side: collect SBD packets per (report, tier) -> decoded segment."""

    def __init__(self):
        self.parts: dict[tuple, dict] = {}

    def feed(self, pkt: bytes) -> dict | None:
        if len(pkt) < PKT_HEAD.size or pkt[:2] != PKT_MAGIC:
            return None
        magic, report, tier, seq, total, kind, crc = PKT_HEAD.unpack_from(pkt)
        payload = pkt[PKT_HEAD.size:]
        if crc16(payload) != crc:
            return {"type": "corrupt", "report": report, "tier": tier, "seq": seq}
        key = (report, tier)
        slot = self.parts.setdefault(key, {"total": total, "chunks": {}})
        slot["chunks"][seq] = payload
        got = len(slot["chunks"])
        info = {"type": "packet", "report": report, "tier": tier, "seq": seq,
                "total": total, "got": got, "bytes": len(pkt)}
        if got == total:
            blob = b"".join(slot["chunks"][i] for i in range(total))
            del self.parts[key]
            try:
                info["segment"] = decode_segment(blob)
                info["type"] = "segment"
            except Exception as e:      # corrupt but complete -> report it
                info["type"] = "corrupt"
                info["error"] = str(e)
        return info


def pack_heartbeat(seq: int, s: Sample, state: int, buffered: int, pct: int) -> bytes:
    body = HB_STRUCT.pack(
        HB_MAGIC, seq & 0xFFFF, int(s.t) & 0xFFFFFFFF, state,
        min(65535, int(_mag(s.accel) * 1000)), min(255, int(tilt_deg(s.quat))),
        int(s.vbatt * 1000), int(max(-128, min(127, s.temp))),
        min(65535, buffered), min(255, pct), 0)
    crc = crc16(body[:-2])
    return body[:-2] + struct.pack("<H", crc)


# ---------------------------------------------------------------- live stream
#
# NeverDrop state frames: the continuous telemetry channel. ~26 B for a
# humanoid pose (quat + |a| + tilt), +2 B per joint. At 8 Hz that is
# ~1.7 kbps of the 2 kbps link; heartbeats ride above it in priority.

ST_HEAD = struct.Struct("<2sHdhhhhHBB")  # magic seq t quat4 amag_mg tilt nj
ST_MAGIC = b"ST"


def pack_state(seq: int, s: Sample, amag_g: float | None = None) -> bytes:
    """amag_g: peak-hold |a| since the previous frame (an 8 Hz stream would
    otherwise sample right past a 40 ms impact spike)."""
    if amag_g is None:
        amag_g = _mag(s.accel)
    body = ST_HEAD.pack(
        ST_MAGIC, seq & 0xFFFF, s.t,
        _q(s.quat[0], SCALES[3]["quat"]), _q(s.quat[1], SCALES[3]["quat"]),
        _q(s.quat[2], SCALES[3]["quat"]), _q(s.quat[3], SCALES[3]["quat"]),
        min(65535, int(amag_g * 1000)), min(255, int(tilt_deg(s.quat))),
        len(s.joints))
    if s.joints:
        body += struct.pack(f"<{len(s.joints)}h", *(_q(j, Q_JOINT) for j in s.joints))
    return body + struct.pack("<H", crc16(body))


def unpack_state(pkt: bytes) -> dict | None:
    if len(pkt) < ST_HEAD.size + 2 or pkt[:2] != ST_MAGIC:
        return None
    if crc16(pkt[:-2]) != struct.unpack("<H", pkt[-2:])[0]:
        return None
    m, seq, t, q0, q1, q2, q3, amag, tilt, nj = ST_HEAD.unpack_from(pkt)
    joints = []
    if nj:
        if len(pkt) != ST_HEAD.size + nj * 2 + 2:
            return None
        joints = [v * Q_JOINT for v in
                  struct.unpack_from(f"<{nj}h", pkt, ST_HEAD.size)]
    sc = SCALES[3]["quat"]
    return {"type": "state", "seq": seq, "t": t,
            "quat": [q0 * sc, q1 * sc, q2 * sc, q3 * sc],
            "accel_g": amag / 1000.0, "tilt": tilt, "joints": joints,
            "bytes": len(pkt)}


def unpack_heartbeat(pkt: bytes) -> dict | None:
    if len(pkt) != HB_STRUCT.size or pkt[:2] != HB_MAGIC:
        return None
    m, seq, t, state, accel_mg, tilt, vb, temp, buffered, pct, crc = HB_STRUCT.unpack(pkt)
    if crc16(pkt[:-2]) != crc:
        return None
    return {"type": "hb", "seq": seq, "t": t, "state": state,
            "accel_g": accel_mg / 1000.0, "tilt": tilt, "vbatt": vb / 1000.0,
            "temp": temp, "buffered": buffered, "pct": pct,
            "bytes": HB_STRUCT.size}


# ---------------------------------------------------------------- analysis

def analyze(seg: dict) -> dict:
    """Root-cause heuristics from a decoded segment. Honest: every number
    below is computed from the transmitted data, nothing is scripted."""
    n, rate = seg["n"], max(seg["rate"], 1.0)
    ax, ay, az = seg["accel"]
    amag = [math.sqrt(ax[i] ** 2 + ay[i] ** 2 + az[i] ** 2) for i in range(n)]
    peak_i = max(range(n), key=lambda i: amag[i])
    peak_g = amag[peak_i]
    t_peak = peak_i / rate

    # free-fall: |a| < 0.35 g just before impact
    ff = 0
    i = peak_i - 1
    while i >= 0 and amag[i] < 0.35:
        ff += 1
        i -= 1
    ff_s = ff / rate
    drop_m = 0.5 * 9.81 * ff_s * ff_s if ff_s > 0.05 else 0.0

    # tilt onset: first sustained departure from baseline tilt
    tilts = [tilt_deg((seg["quat"][0][i], seg["quat"][1][i],
                       seg["quat"][2][i], seg["quat"][3][i])) for i in range(n)]
    base = sum(tilts[: max(1, n // 10)]) / max(1, n // 10)
    onset_i = next((i for i in range(n) if tilts[i] > base + 15), peak_i)
    warn_s = max(0.0, t_peak - onset_i / rate)

    # dominant rotation axis around onset->impact
    gx, gy, gz = seg["gyro"]
    span = range(onset_i, max(onset_i + 1, peak_i))
    ints = [sum(abs(g[i]) for i in span) for g in (gx, gy, gz)]
    axis = ["roll", "pitch", "yaw"][ints.index(max(ints))]
    sign = sum((gx, gy, gz)[ints.index(max(ints))][i] for i in span)
    direction = {"roll": ("left", "right"), "pitch": ("forward", "backward"),
                 "yaw": ("ccw", "cw")}[axis][0 if sign >= 0 else 1]

    final_tilt = sum(tilts[-max(1, n // 20):]) / max(1, n // 20)
    if final_tilt < 30:
        attitude = "upright"
    elif final_tilt >= 135:
        attitude = "inverted"
    elif axis == "pitch":
        attitude = "face down" if direction == "forward" else "on its back"
    else:
        attitude = "on side"

    joints = seg.get("joints") or []
    kind = "fall"
    if joints:
        jr = [max(j) - min(j) for j in joints]
        jw = jr.index(max(jr))

    if joints and peak_g < 2.5:
        # manipulator incident: no impact signature — lead with the joint story
        kind = "arm"
        med = sorted(joints[jw])[len(joints[jw]) // 2]
        t_j = max(range(n), key=lambda i: abs(joints[jw][i] - med)) / rate
        summary = (f"External force on manipulator: joint {jw} deflected "
                   f"{math.degrees(jr[jw]):.0f} deg at t+{t_j:.2f} s with no base "
                   f"impact signature (peak {peak_g:.1f} g) — consistent with a "
                   f"grab or collision. Full joint history recorded onboard.")
    else:
        joint_note = (f" Largest joint excursion: joint {jw} "
                      f"({math.degrees(jr[jw]):.0f} deg)." if joints else "")
        summary = (f"{direction.capitalize()} {axis} instability began {warn_s:.1f} s "
                   f"before impact"
                   + (f"; free-fall {ff_s:.2f} s (est. drop {drop_m:.1f} m)" if ff_s > 0.05 else "")
                   + f". Primary impact {peak_g:.1f} g at t+{t_peak:.2f} s. "
                   f"Robot came to rest {attitude} (tilt {final_tilt:.0f} deg)."
                   + joint_note)

    return {"peak_g": round(peak_g, 1), "t_peak": round(t_peak, 2),
            "freefall_s": round(ff_s, 2), "drop_m": round(drop_m, 1),
            "warning_s": round(warn_s, 1), "axis": axis, "direction": direction,
            "final_attitude": attitude, "kind": kind, "summary": summary}


# ---------------------------------------------------------------- report build

def build_report(ring: RingBuffer, t_incident: float, pre=10.0, post=2.0,
                 report_id: int = 1, preview_hz=20.0) -> dict:
    """Snapshot the ring around an incident -> {tier: packets, sizes}."""
    win = ring.window(t_incident - pre, t_incident + post)
    if len(win) < 10:
        return {"packets": [], "sizes": {}, "n": len(win)}
    seg2 = encode_segment(win, tier=2, report_id=report_id)
    seg1 = encode_segment(decimate(win, preview_hz), tier=1, report_id=report_id)
    p1 = packetize(seg1, report_id, 1)
    p2 = packetize(seg2, report_id, 2)
    return {"packets": p1 + p2,          # tier 1 first: preview lands first
            "sizes": {"tier1": len(seg1), "tier2": len(seg2),
                      "pkts1": len(p1), "pkts2": len(p2)},
            "n": len(win)}


STATE_NOMINAL, STATE_INCIDENT, STATE_TRANSMITTING, STATE_DELIVERED = 0, 1, 2, 3
STATE_NAMES = {0: "NOMINAL", 1: "INCIDENT", 2: "TRANSMITTING", 3: "DELIVERED"}


def now() -> float:
    return time.time()
