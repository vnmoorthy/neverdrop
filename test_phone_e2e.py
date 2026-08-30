"""Phone-mode end-to-end proof, no phone required.

Streams a synthetic Sensor Logger HTTP push (batches every 200 ms, per-sensor
rows with distinct ns timestamps, exactly like the real app): 8 s of quiet
handling noise, then a shove — free-fall, 7 g impact, rest on side.

PASS = the impact trigger fires from the streamed data, the burst crosses
the 2 kbps link, and mission control decodes a tier-1 segment + analysis.

    (start first:  .venv/bin/python -m icebox.server --source phone --port 8010)
    .venv/bin/python test_phone_e2e.py
"""
import asyncio
import json
import math
import random

import aiohttp

BASE = "http://localhost:8010"
RATE = 100
rng = random.Random(3)


def batch(t0_ns, samples):
    """samples: list of (dt_s, accel_xyz m/s2, gyro_xyz, quat wxyz)."""
    payload = []
    for dt, acc, gyro, quat in samples:
        tns = t0_ns + int(dt * 1e9)
        payload.append({"name": "gravity", "time": tns,
                        "values": {"x": 0.0, "y": 0.0, "z": 9.81}})
        payload.append({"name": "gyroscope", "time": tns + 1_000_000,
                        "values": {"x": gyro[0], "y": gyro[1], "z": gyro[2]}})
        payload.append({"name": "orientation", "time": tns + 2_000_000,
                        "values": {"qw": quat[0], "qx": quat[1],
                                   "qy": quat[2], "qz": quat[3]}})
        payload.append({"name": "accelerometer", "time": tns + 3_000_000,
                        "values": {"x": acc[0], "y": acc[1], "z": acc[2]}})
    return {"messageId": 1, "sessionId": "e2e", "deviceId": "sim-phone",
            "payload": payload}


def quiet_sample():
    g = rng.gauss
    return ((g(0, .15), g(0, .15), g(0, .15)),
            (g(0, .02), g(0, .02), g(0, .02)), (1, 0, 0, 0))


def shove_samples():
    """1.2 s: 0.35 s free-fall, 50 ms 7 g impact, rest tilted."""
    out = []
    for i in range(int(1.2 * RATE)):
        t = i / RATE
        g = rng.gauss
        if t < 0.35:                       # free-fall: user accel cancels gravity
            acc = (g(0, .3), g(0, .3), -9.81 + g(0, .5))
            gyro = (g(0, .5), 3.5 + g(0, .5), g(0, .3))
            quat = (1, 0, 0, 0)
        elif t < 0.40:                     # impact spike ~7 g total
            k = math.exp(-((t - .375) / .012) ** 2)
            acc = (55 * k + g(0, 2), 20 * k, 25 * k)
            gyro = (g(0, 2), -8 * k, g(0, 1))
            quat = (.92, 0, .38, 0)
        else:                              # rest on side
            acc = (9.81 - 9.81, g(0, .1), g(0, .1))   # user accel ~0
            gyro = (g(0, .02), g(0, .02), g(0, .02))
            quat = (.707, 0, .707, 0)
        out.append((t, acc, gyro, quat))
    return out


async def main():
    got = {"incident": None, "incident_via": None, "segments": [],
           "analysis": None, "hb_states": set(), "states": 0,
           "coverage": {}, "gap_verified": False}

    async with aiohttp.ClientSession() as s:

        async def listen():
            async with s.ws_connect(BASE + "/ws") as ws:
                async for msg in ws:
                    ev = json.loads(msg.data)
                    if ev["type"] == "state":
                        got["states"] += 1        # the live-twin stream
                    elif ev["type"] == "hb":
                        got["hb_states"].add(ev["state"])
                    elif ev["type"] == "incident":
                        got["incident"] = ev["cause"]
                        got["incident_via"] = ev.get("via")
                        print(f"  INCIDENT via {ev.get('via')}: {ev['cause']}")
                    elif ev["type"] == "gap" and ev.get("verified"):
                        got["gap_verified"] = True
                        print(f"  blackout backfill VERIFIED: {ev.get('coverage')}"
                              f" chunks, {ev['n']} frames")
                    elif ev["type"] == "segment":
                        got["segments"].append((ev["tier"], ev["n"]))
                        if ev.get("verified"):
                            got["coverage"][ev["tier"]] = ev.get("coverage")
                        print(f"  segment tier {ev['tier']} VERIFIED "
                              f"{ev.get('coverage')}: {ev['n']} samples "
                              f"@ {ev['rate']:.0f} Hz")
                    elif ev["type"] == "analysis":
                        got["analysis"] = ev["summary"]
                        print(f"  analysis: \"{ev['summary']}\"")
                        if any(t == 1 for t, _ in got["segments"]):
                            return

        listener = asyncio.create_task(listen())

        t_ns = 1_700_000_000_000_000_000
        print("  streaming 8 s of quiet handling noise...")
        for _ in range(40):                       # 40 batches x 200 ms = 8 s
            samples = [(k / RATE, *quiet_sample()) for k in range(20)]
            await s.post(BASE + "/phone", json=batch(t_ns, samples))
            t_ns += int(0.2 * 1e9)
            await asyncio.sleep(0.2)
        print("  mid-quiet BLACKOUT (4 s) ...")
        await s.post(BASE + "/op", json={"cmd": "link", "up": False})
        for _ in range(20):                       # phone keeps pushing
            samples = [(k / RATE, *quiet_sample()) for k in range(20)]
            await s.post(BASE + "/phone", json=batch(t_ns, samples))
            t_ns += int(0.2 * 1e9)
            await asyncio.sleep(0.2)
        await s.post(BASE + "/op", json={"cmd": "link", "up": True})
        print("  SHOVE (free-fall -> 7 g impact)...")
        shove = shove_samples()                   # generated ONCE
        for i in range(0, len(shove), 20):
            chunk = shove[i:i + 20]
            chunk = [(dt - chunk[0][0], a, g, q) for dt, a, g, q in chunk]
            await s.post(BASE + "/phone", json=batch(t_ns, chunk))
            t_ns += int(0.2 * 1e9)
            await asyncio.sleep(0.2)
        # keep resting samples flowing so the post-impact window fills
        for _ in range(30):
            samples = [(k / RATE, (0, 0, 0), (0, 0, 0), (.707, 0, .707, 0))
                       for k in range(20)]
            await s.post(BASE + "/phone", json=batch(t_ns, samples))
            t_ns += int(0.2 * 1e9)
            await asyncio.sleep(0.2)
            if listener.done():
                break

        try:
            await asyncio.wait_for(listener, timeout=30)
        except asyncio.TimeoutError:
            listener.cancel()

    assert got["incident"], "trigger never fired from phone stream"
    assert got["incident_via"] == "link", \
        "incident knowledge did not come from a decoded link packet"
    assert "impact" in got["incident"], f"wrong cause: {got['incident']}"
    assert got["gap_verified"], "blackout backfill never verified"
    assert got["coverage"].get(1), "tier-1 never manifest-verified"
    c = got["coverage"][1].split("/")
    assert c[0] == c[1], f"tier-1 coverage incomplete: {got['coverage'][1]}"
    assert got["analysis"], "no root-cause analysis produced"
    assert got["hb_states"], "no heartbeats crossed the link at all"
    assert got["hb_states"] != {0}, "state never left NOMINAL"
    assert got["states"] > 20, f"live twin stream too thin: {got['states']} frames"
    print(f"  live twin: {got['states']} state frames crossed the link")
    print("PHONE E2E: ALL PASS")


asyncio.run(main())
