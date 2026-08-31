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
import os
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
        # traverse state: the robot has a job, not a pose
        self._px, self._py = 0.0, 0.0
        self._yaw = 0.0
        self._last_t: float | None = None
        self._stumble_at = 18.0            # first gust ~18 s in
        self._stumble_t0: float | None = None
        self._t_start: float | None = None

    def fall(self):
        if self._fall_t0 is None:
            self._fall_t0 = time.time() + 0.15

    def reset(self):
        self._fall_t0 = None

    def _nominal(self, t: float) -> Sample:
        g = self._rng.gauss
        if self._t_start is None:
            self._t_start = t
        dt = min(0.1, t - self._last_t) if self._last_t else 0.0
        self._last_t = t
        el = t - self._t_start

        # --- walk the traverse: wandering heading, steered back inside r=4 m
        wander = 0.28 * math.sin(el * 0.21) + 0.12 * math.sin(el * 0.045)
        r = math.hypot(self._px, self._py)
        steer = 0.0
        if r > 3.2:
            to_home = math.atan2(-self._py, -self._px)
            d = (to_home - self._yaw + math.pi) % (2 * math.pi) - math.pi
            steer = 1.4 * d
        self._yaw += (wander + steer) * dt
        speed = 0.55
        self._px += speed * math.cos(self._yaw) * dt
        self._py += speed * math.sin(self._yaw) * dt

        # --- gust stumble every ~25-45 s: stagger + g-spike, then recover
        # (2.2 g peak stays under the 3 g crash floor: drama, not an incident)
        stumble_roll = stumble_pitch = 0.0
        acc_spike = 0.0
        if self._stumble_t0 is None and el >= self._stumble_at:
            self._stumble_t0 = t
        if self._stumble_t0 is not None:
            ft = t - self._stumble_t0
            if ft < 1.3:
                k = math.sin(math.pi * ft / 1.3)
                stumble_pitch = math.radians(13) * k
                stumble_roll = math.radians(7) * math.sin(2 * math.pi * ft / 1.3)
                acc_spike = 1.2 * math.exp(-((ft - 0.35) / 0.06) ** 2)
            else:
                self._stumble_t0 = None
                self._stumble_at = el + self._rng.uniform(25, 45)

        bounce = 0.25 * math.sin(2 * math.pi * 1.9 * t)
        sway = math.radians(2.5) * math.sin(2 * math.pi * 0.9 * t)
        bob = math.radians(3.0) * math.sin(2 * math.pi * 1.9 * t + 0.6)
        quat = _quat_from_euler(sway + stumble_roll + g(0, 0.004),
                                bob + stumble_pitch + g(0, 0.004), self._yaw)
        return Sample(
            t=t, quat=quat,
            gyro=(0.12 * math.cos(2 * math.pi * 0.9 * t) + g(0, 0.02),
                  g(0, 0.02), wander + steer + g(0, 0.01)),
            accel=(acc_spike * 0.7 + g(0, 0.02), g(0, 0.02),
                   1.0 + bounce * 0.15 + acc_spike * 0.7 + g(0, 0.02)),
            vbatt=self.vbatt, temp=18.0 + g(0, 0.1),
            pos=(self._px, self._py))

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
        quat = _quat_from_euler(g(0, 0.01), pitch, self._yaw)
        return Sample(t=t, quat=quat, gyro=(g(0, 0.02), wp, g(0, 0.02)),
                      accel=acc, vbatt=self.vbatt, temp=18.0,
                      pos=(self._px, self._py))

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
    STALE_S = 0.5          # do not fuse sensor values older than this

    def __init__(self):
        self.queue: asyncio.Queue[Sample] = asyncio.Queue(maxsize=4096)
        # per-sensor timestamp buffers: name -> list[(t_s, values_dict)]
        self._buf: dict[str, list] = {}
        self.last_push = 0.0
        self._last_emit_t = 0.0
        self.health = {"platform": "unknown", "standardized": "unknown",
                       "aligned": 0, "stale_rejected": 0, "dup_rejected": 0,
                       "bad_quat": 0, "calibrated": False,
                       "gravity_mag": None, "noise_g": None}
        self._cal: list[float] = []

    def _nearest(self, name: str, t: float):
        """Nearest-in-time value within STALE_S, with linear interpolation
        between the two bracketing samples when available."""
        buf = self._buf.get(name)
        if not buf:
            return None
        lo, hi = None, None
        for bt, bv in reversed(buf):
            if bt <= t:
                lo = (bt, bv)
                break
        for bt, bv in buf:
            if bt >= t:
                hi = (bt, bv)
                break
        pick = None
        if lo and hi and hi[0] > lo[0]:
            f = (t - lo[0]) / (hi[0] - lo[0])
            pick = (t, {k: lo[1].get(k, 0) + f * (hi[1].get(k, 0) - lo[1].get(k, 0))
                        for k in set(lo[1]) | set(hi[1])})
        else:
            pick = min((x for x in (lo, hi) if x), key=lambda x: abs(x[0] - t),
                       default=None)
        if pick is None or abs(pick[0] - t) > self.STALE_S:
            self.health["stale_rejected"] += 1
            return None
        return pick[1]

    def feed_http(self, body: dict) -> int:
        """Sensor Logger push. Sensors arrive on independent timestamps and
        are aligned to the accelerometer clock; stale values are rejected,
        not silently fused."""
        if "deviceId" in body:
            self.health["platform"] = str(body.get("deviceId"))[:24]
        fed = 0
        for entry in body.get("payload", []):
            name, vals = entry.get("name", ""), entry.get("values", {})
            t = entry.get("time", 0) / 1e9
            buf = self._buf.setdefault(name, [])
            if buf and t <= buf[-1][0]:
                self.health["dup_rejected"] += 1     # duplicate/non-monotonic
                continue
            buf.append((t, vals))
            if len(buf) > 400:
                del buf[:200]
        for t, acc in list(self._buf.get("accelerometer", [])):
            if t <= self._last_emit_t:
                continue
            self._last_emit_t = t
            grav = self._nearest("gravity", t)
            gyro = self._nearest("gyroscope", t) or {}
            ori = self._nearest("orientation", t) or {}
            if grav is None:
                continue                              # cannot form total accel
            if {"qw", "qx", "qy", "qz"} <= ori.keys():
                q = (ori["qw"], ori["qx"], ori["qy"], ori["qz"])
                norm = math.sqrt(sum(v * v for v in q))
                if not (0.5 < norm < 2.0):
                    self.health["bad_quat"] += 1
                    q = (1.0, 0.0, 0.0, 0.0)
                else:
                    q = tuple(v / norm for v in q)
            else:
                q = _quat_from_euler(ori.get("roll", 0.0),
                                     ori.get("pitch", 0.0), ori.get("yaw", 0.0))
            ax = (acc.get("x", 0) + grav.get("x", 0)) / 9.81
            ay = (acc.get("y", 0) + grav.get("y", 0)) / 9.81
            az = (acc.get("z", 0) + grav.get("z", 0)) / 9.81
            # stationary calibration over the first ~3 s of samples
            if not self.health["calibrated"]:
                self._cal.append(math.sqrt(ax * ax + ay * ay + az * az))
                if len(self._cal) >= 200:
                    mean = sum(self._cal) / len(self._cal)
                    var = sum((v - mean) ** 2 for v in self._cal) / len(self._cal)
                    self.health.update(calibrated=True,
                                       gravity_mag=round(mean, 3),
                                       noise_g=round(math.sqrt(var), 4))
            # battery/temperature are NOT measured by this pipeline
            s = Sample(t=t, quat=q,
                       gyro=(gyro.get("x", 0.0), gyro.get("y", 0.0),
                             gyro.get("z", 0.0)),
                       accel=(ax, ay, az), vbatt=None, temp=None)
            try:
                self.queue.put_nowait(s)
                fed += 1
                self.health["aligned"] += 1
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
    ADDR_TORQUE_ENABLE = 40       # STS3215: 1 byte
    ADDR_GOAL_POSITION = 42       # 2 bytes
    ADDR_GOAL_SPEED = 46          # 2 bytes (steps/s; small = slow)
    ADDR_PRESENT_POSITION = 56    # 2 bytes
    ADDR_PRESENT_CURRENT = 69    # 2 bytes, ~6.5 mA/LSB

    # WORK LOOP SAFETY RAILS (the arm moves in the real world):
    WORK_AMP_TICKS = 70           # ~6 deg; env ICEBOX_ARM_AMP overrides, hard cap 150
    WORK_SPEED = 160              # slow goal speed (steps/s)
    WORK_JOINTS = (0, 3, 4)       # base + wrist pair only, phase-shifted

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
        self.working = False          # scan-routine flag (set via harness)
        self._work_setup = False
        self._work_t0 = 0.0
        self._cycle = 0
        self.amp = min(150, int(os.environ.get("ICEBOX_ARM_AMP",
                                               str(self.WORK_AMP_TICKS))))
        import atexit
        atexit.register(self._torque_off_all)
        print(f"ArmAdapter: {port} @ {baud} baud, servos {alive}")

    # ---------------------------------------------------------- work loop
    # The arm's job in this project: a slow scan routine that NeverDrop
    # supervises over the constrained link. All motion is clamped to
    # +-amp ticks (~6 deg) around the captured center at a slow speed
    # register, and torque drops on stop/reset/exception/exit.
    def work_start(self):
        self.working = True

    def work_stop(self):
        self.working = False
        self._work_setup = False
        self._torque_off_all()

    def _torque_off_all(self):
        try:
            for sid in self.ids:
                self.pkt.write1ByteTxRx(self.port_h, sid,
                                        self.ADDR_TORQUE_ENABLE, 0)
        except Exception:
            pass

    def _work_step(self):
        """Runs inside the serial worker thread (bus access serialized)."""
        try:
            if not self._work_setup:
                for j in self.WORK_JOINTS:
                    sid = self.ids[j]
                    self.pkt.write2ByteTxRx(self.port_h, sid,
                                            self.ADDR_GOAL_SPEED,
                                            self.WORK_SPEED)
                    self.pkt.write1ByteTxRx(self.port_h, sid,
                                            self.ADDR_TORQUE_ENABLE, 1)
                self._work_setup = True
                self._work_t0 = time.time()
            t = time.time() - self._work_t0
            for k, j in enumerate(self.WORK_JOINTS):
                sid = self.ids[j]
                target = self._center[sid] + int(
                    self.amp * math.sin(2 * math.pi * 0.12 * t + k * 2.1))
                target = max(self._center[sid] - self.amp,
                             min(self._center[sid] + self.amp, target))
                target = max(60, min(4035, target))
                self.pkt.write2ByteTxRx(self.port_h, sid,
                                        self.ADDR_GOAL_POSITION, target)
        except Exception:
            self.working = False
            self._torque_off_all()

    def _reconnect(self):
        """A grab can jostle the USB cable: reopen the port instead of dying."""
        import glob
        try:
            self.port_h.closePort()
        except Exception:
            pass
        cands = (glob.glob("/dev/tty.usbmodem*") + glob.glob("/dev/tty.usbserial*")
                 + glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
        if not cands:
            return False
        import scservo_sdk as scs
        self.port_h = scs.PortHandler(cands[0])
        if not self.port_h.openPort():
            return False
        self.port_h.setBaudRate(1000000)
        self._work_setup = False        # re-arm torque/speed on next cycle
        print(f"ArmAdapter: reconnected on {cands[0]}", flush=True)
        return True

    def read_state(self):
        try:
            return self._read_state_inner()
        except Exception as e:
            # serial hiccup (cable jostle): hold last-known state, reconnect
            time.sleep(0.5)
            self._reconnect()
            return self._last_jc if hasattr(self, "_last_jc") else                 ([0.0] * len(self.ids), [0.0] * len(self.ids))

    def _read_state_inner(self):
        self._cycle += 1
        if self.working and self._cycle % 5 == 0:      # goal writes at ~10 Hz
            self._work_step()
        elif not self.working and self._work_setup:
            self._work_setup = False
            self._torque_off_all()
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
        self._last_jc = (joints, currents)
        return joints, currents

    def fall(self):     # physical source: the "incident" is a real grab
        pass

    def reset(self):
        pass

    async def stream(self):
        """Serial reads are blocking (~6 ms/cycle measured): run them in a
        worker thread so the asyncio loop (link pacing, heartbeats, web)
        never stalls behind the bus."""
        loop = asyncio.get_running_loop()
        dt = 1.0 / self.rate_hz
        while True:
            joints, currents = await loop.run_in_executor(None, self.read_state)
            # battery/temperature are not measured on this bus
            yield Sample(t=time.time(), quat=(1, 0, 0, 0), gyro=(0, 0, 0),
                         accel=(0, 0, 1), joints=tuple(joints),
                         currents=tuple(currents), vbatt=None, temp=None)
            await asyncio.sleep(dt)


SOURCES = {"sim": SimSource, "simarm": SimArmSource, "phone": PhoneSource,
           "arm": ArmAdapter}
