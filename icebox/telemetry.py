"""Telemetry sources. Each source is an async generator of Samples.

  sim    — synthetic humanoid IMU with a scripted, physically plausible fall
           (rehearsal + the cannot-fail demo fallback)
  phone  — live IMU from the Sensor Logger app (HTTP push), the real shove
  simarm — synthetic 6-DOF arm doing a pick cycle; `disturb` injects a grab
  arm    — template adapter for the manipulator SDK at the venue
"""
from __future__ import annotations

import asyncio
import math
import random
import time

from .blackbox import Sample


def _quat_from_euler(roll, pitch, yaw):
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    return (cr * cp * cy + sr * sp * sy,
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy)


class SimSource:
    """Humanoid IMU at 200 Hz: gait bounce when nominal; `fall()` plays a
    ~2.6 s forward-pitch tumble — build-up, free-fall, impact, bounce, rest."""

    rate_hz = 200.0

    def __init__(self):
        self._fall_t0: float | None = None
        self._rng = random.Random(7)
        self.vbatt = 25.2

    def fall(self):
        if self._fall_t0 is None:
            self._fall_t0 = time.time() + 0.15

    def reset(self):
        self._fall_t0 = None

    def _nominal(self, t: float) -> Sample:
        g = self._rng.gauss
        bounce = 0.25 * math.sin(2 * math.pi * 1.9 * t)
        sway = math.radians(2.5) * math.sin(2 * math.pi * 0.9 * t)
        # visible life for the live twin: slow heading wander + gait pitch bob
        yaw = 0.9 * math.sin(t * 0.21) + 0.35 * math.sin(t * 0.047)
        bob = math.radians(3.0) * math.sin(2 * math.pi * 1.9 * t + 0.6)
        quat = _quat_from_euler(sway + g(0, 0.004), bob + g(0, 0.004), yaw)
        return Sample(
            t=t, quat=quat,
            gyro=(0.12 * math.cos(2 * math.pi * 0.9 * t) + g(0, 0.02),
                  g(0, 0.02), 0.19 * math.cos(t * 0.21) + g(0, 0.01)),
            accel=(g(0, 0.02), g(0, 0.02), 1.0 + bounce * 0.15 + g(0, 0.02)),
            vbatt=self.vbatt, temp=18.0 + g(0, 0.1))

    def _falling(self, t: float, ft: float) -> Sample:
        g = self._rng.gauss
        # phase timings (s): lean 0-0.9, freefall 0.9-1.35, impact 1.35-1.45,
        # bounce 1.45-1.9, rest >1.9 (face down, pitch 90 deg)
        if ft < 0.9:                               # accelerating forward lean
            k = (ft / 0.9) ** 2
            pitch = math.radians(70) * k
            wp = math.radians(150) * (ft / 0.9)
            acc = (math.sin(pitch) * 0.9, g(0, 0.03), math.cos(pitch) * 0.9)
        elif ft < 1.35:                            # free-fall
            pitch = math.radians(70 + (ft - 0.9) / 0.45 * 25)
            wp = math.radians(220)
            acc = (g(0, 0.05), g(0, 0.05), 0.12 + g(0, 0.04))
        elif ft < 1.45:                            # impact spike
            pitch = math.radians(96)
            wp = -math.radians(400) * (ft - 1.35) / 0.1
            spike = 10.5 * math.exp(-((ft - 1.38) / 0.018) ** 2)
            acc = (0.4 + spike * 0.9, g(0, 0.3) + spike * 0.25, 0.2 + spike * 0.35)
        elif ft < 1.9:                             # bounce & settle
            k = math.exp(-(ft - 1.45) * 6)
            pitch = math.radians(90 + 6 * math.sin((ft - 1.45) * 30) * k)
            wp = math.radians(80) * math.sin((ft - 1.45) * 30) * k
            acc = (0.95 + 1.5 * k * math.sin((ft - 1.45) * 35), g(0, 0.1), 0.15)
        else:                                      # at rest, face down
            pitch = math.radians(90.5)
            wp = g(0, 0.005)
            acc = (0.99 + g(0, 0.01), g(0, 0.01), 0.03 + g(0, 0.01))
        quat = _quat_from_euler(g(0, 0.01), pitch, 0.0)
        return Sample(t=t, quat=quat, gyro=(g(0, 0.02), wp, g(0, 0.02)),
                      accel=acc, vbatt=self.vbatt, temp=18.0)

    async def stream(self):
        dt = 1.0 / self.rate_hz
        next_t = time.time()
        while True:
            t = time.time()
            self.vbatt = max(21.0, self.vbatt - dt * 0.0004)
            if self._fall_t0 is not None and t >= self._fall_t0:
                yield self._falling(t, t - self._fall_t0)
            else:
                yield self._nominal(t)
            next_t += dt
            delay = next_t - time.time()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -1.0:          # fell badly behind; resync
                next_t = time.time()


class SimArmSource:
    """6-joint arm running a smooth pick cycle. `disturb()` = a judge grabs
    the arm: position error + current spike on joints 2/3 for ~1.2 s."""

    rate_hz = 100.0

    def __init__(self):
        self._disturb_t0: float | None = None
        self._rng = random.Random(11)

    # dashboard buttons reuse fall()/reset() names for a common op interface
    def fall(self, at_t: float | None = None):
        if self._disturb_t0 is None:
            self._disturb_t0 = (at_t if at_t is not None else time.time() + 0.1)

    def reset(self):
        self._disturb_t0 = None

    def sample_at(self, t: float) -> Sample:
        g = self._rng.gauss
        cyc = t * 0.7
        joints = [0.6 * math.sin(cyc + i * 0.9) + 0.2 * math.sin(2.1 * cyc + i)
                  for i in range(6)]
        currents = [0.6 + 0.4 * abs(math.sin(cyc + i)) + g(0, 0.03)
                    for i in range(6)]
        accel = (g(0, 0.01), g(0, 0.01), 1.0 + g(0, 0.01))
        if self._disturb_t0 is not None:
            ft = t - self._disturb_t0
            if 0 <= ft < 1.2:
                k = math.sin(math.pi * ft / 1.2)
                joints[2] += 0.5 * k
                joints[3] -= 0.35 * k
                currents[2] += 5.5 * k + g(0, 0.2)
                currents[3] += 3.8 * k + g(0, 0.2)
                accel = (g(0, 0.05) + 0.5 * k, g(0, 0.05), 1.0 + 0.3 * k)
            elif ft >= 1.2:
                self._disturb_t0 = None
        return Sample(t=t, quat=(1, 0, 0, 0), gyro=(0, 0, 0), accel=accel,
                      joints=tuple(joints), currents=tuple(currents),
                      vbatt=24.0, temp=26.0)

    async def stream(self):
        dt = 1.0 / self.rate_hz
        while True:
            yield self.sample_at(time.time())
            await asyncio.sleep(dt)


class PhoneSource:
    """Live IMU from the Sensor Logger app (free, iOS/Android).

    Phone setup (60 seconds):
      1. Install "Sensor Logger" (Kelvin Choi).
      2. Enable Accelerometer, Gyroscope, Gravity, Orientation (100 Hz) —
         gyroscope included, or the root-cause axis/direction is garbage.
      3. Settings -> Data Streaming -> HTTP Push:
             URL: http://<laptop-ip>:8000/phone
      4. Press record. Strap the phone in the boot. Shove the boot.

    The phone IS the robot: its samples enter the ONBOARD side directly;
    only heartbeats + crash bursts cross the simulated satellite link.
    """

    rate_hz = 100.0

    def __init__(self):
        self.queue: asyncio.Queue[Sample] = asyncio.Queue(maxsize=4096)
        self._latest: dict = {}
        self.last_push = 0.0

    def feed_http(self, body: dict) -> int:
        """Called by the web server with a Sensor Logger push payload."""
        fed = 0
        rows: dict[int, dict] = {}
        for entry in body.get("payload", []):
            name = entry.get("name", "")
            vals = entry.get("values", {})
            tns = entry.get("time", 0)
            rows.setdefault(tns, {})[name] = vals
        for tns in sorted(rows):
            r = rows[tns]
            self._latest.update(r)
            # emit ONLY on accelerometer rows: gravity/orientation rows just
            # update _latest (using gravity as accel would double-add gravity
            # and poison the jerk baseline -> the shove would never trigger)
            acc = r.get("accelerometer")
            if not acc:
                continue
            ori = self._latest.get("orientation", {})
            if {"qw", "qx", "qy", "qz"} <= ori.keys():
                quat = (ori["qw"], ori["qx"], ori["qy"], ori["qz"])
            else:
                quat = _quat_from_euler(ori.get("roll", 0.0),
                                        ori.get("pitch", 0.0),
                                        ori.get("yaw", 0.0))
            grav = self._latest.get("gravity", {})
            gx = acc.get("x", 0) + grav.get("x", 0)
            gy = acc.get("y", 0) + grav.get("y", 0)
            gz = acc.get("z", 0) + grav.get("z", 0)
            s = Sample(t=tns / 1e9, quat=quat,
                       gyro=(self._latest.get("gyroscope", {}).get("x", 0.0),
                             self._latest.get("gyroscope", {}).get("y", 0.0),
                             self._latest.get("gyroscope", {}).get("z", 0.0)),
                       accel=(gx / 9.81, gy / 9.81, gz / 9.81),
                       vbatt=24.0, temp=20.0)
            try:
                self.queue.put_nowait(s)
                fed += 1
            except asyncio.QueueFull:
                pass
        if fed:
            self.last_push = time.time()
        return fed

    def fall(self):     # physical source: nothing to script
        pass

    def reset(self):
        pass

    async def stream(self):
        while True:
            yield await self.queue.get()


class ArmAdapter:
    """Real manipulator over Feetech serial bus (SO-100/SO-101/LeRobot-class
    arms: STS3215 servos, IDs 1..6, 1 Mbaud). Auto-detects the USB serial
    port. Reads Present_Position + Present_Current per servo ~50 Hz.

    Env overrides:  ICEBOX_ARM_PORT=/dev/tty.usbmodemXXXX
                    ICEBOX_ARM_IDS=1,2,3,4,5,6
                    ICEBOX_ARM_BAUD=1000000
    """

    rate_hz = 50.0
    ADDR_PRESENT_POSITION = 56    # STS3215: 2 bytes
    ADDR_PRESENT_CURRENT = 69     # STS3215: 2 bytes, ~6.5 mA/LSB

    def __init__(self):
        import glob
        import os
        import scservo_sdk as scs
        port = os.environ.get("ICEBOX_ARM_PORT")
        if not port:
            cands = (glob.glob("/dev/tty.usbmodem*") + glob.glob("/dev/tty.usbserial*")
                     + glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
            if not cands:
                raise RuntimeError(
                    "No USB serial device found. Plug the follower arm into "
                    "the laptop, then re-run (or set ICEBOX_ARM_PORT).")
            port = cands[0]
        baud = int(os.environ.get("ICEBOX_ARM_BAUD", "1000000"))
        self.ids = [int(i) for i in
                    os.environ.get("ICEBOX_ARM_IDS", "1,2,3,4,5,6").split(",")]
        self.scs = scs
        self.port_h = scs.PortHandler(port)
        self.pkt = scs.PacketHandler(0)          # SCS/STS protocol
        if not self.port_h.openPort():
            raise RuntimeError(f"Could not open {port}")
        self.port_h.setBaudRate(baud)
        self._center = {}
        alive = []
        for sid in self.ids:
            pos, res, err = self.pkt.read2ByteTxRx(
                self.port_h, sid, self.ADDR_PRESENT_POSITION)
            if res == scs.COMM_SUCCESS:
                alive.append(sid)
                self._center[sid] = pos
        if not alive:
            raise RuntimeError(
                f"Port {port} opened but no servos answered on IDs "
                f"{self.ids}. Check baud (ICEBOX_ARM_BAUD) and cabling.")
        self.ids = alive
        print(f"ArmAdapter: {port} @ {baud} baud, servos {alive}")

    def read_state(self):
        joints, currents = [], []
        for sid in self.ids:
            pos, res, _ = self.pkt.read2ByteTxRx(
                self.port_h, sid, self.ADDR_PRESENT_POSITION)
            cur, res2, _ = self.pkt.read2ByteTxRx(
                self.port_h, sid, self.ADDR_PRESENT_CURRENT)
            if res != self.scs.COMM_SUCCESS:
                pos = self._center.get(sid, 2048)
            if res2 != self.scs.COMM_SUCCESS:
                cur = 0
            # STS3215: 0..4095 ticks over 360 deg; current is signed 15-bit
            joints.append((pos - self._center.get(sid, 2048)) *
                          (2 * math.pi / 4096.0))
            if cur > 32767:
                cur -= 65536
            currents.append(abs(cur) * 0.0065)
        return joints, currents

    def fall(self):     # physical source: the "incident" is a real grab
        pass

    def reset(self):
        pass

    async def stream(self):
        dt = 1.0 / self.rate_hz
        while True:
            joints, currents = self.read_state()
            yield Sample(t=time.time(), quat=(1, 0, 0, 0), gyro=(0, 0, 0),
                         accel=(0, 0, 1), joints=tuple(joints),
                         currents=tuple(currents), vbatt=24.0, temp=25.0)
            await asyncio.sleep(dt)


SOURCES = {"sim": SimSource, "simarm": SimArmSource, "phone": PhoneSource,
           "arm": ArmAdapter}
