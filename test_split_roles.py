"""Separate-process end-to-end: onboard and ground as distinct OS processes
talking only through the constrained UDP link model.

    python test_split_roles.py   ->  SPLIT ROLES: ALL PASS
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

import aiohttp

PY = sys.executable
UDP = 47990
G_HTTP, O_HTTP = 8021, 8022


async def main():
    tmp = tempfile.mkdtemp(prefix="ndz_split_")
    env = dict(os.environ)
    ground = subprocess.Popen(
        [PY, "-m", "icebox.server", "--role", "ground", "--port", str(G_HTTP),
         "--listen-port", str(UDP), "--bps", "32000", "--latency", "0.05",
         "--data-dir", os.path.join(tmp, "g")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    onboard = subprocess.Popen(
        [PY, "-m", "icebox.server", "--role", "onboard", "--source", "sim",
         "--port", str(O_HTTP), "--ground-host", "127.0.0.1",
         "--ground-port", str(UDP), "--bps", "32000", "--latency", "0.05",
         "--data-dir", os.path.join(tmp, "o")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    try:
        await asyncio.sleep(3)
        assert ground.poll() is None, "ground process died"
        assert onboard.poll() is None, "onboard process died"
        got = {"states": 0, "incident_via": None, "verified": {}}
        async with aiohttp.ClientSession() as s:
            # health on both processes
            gh = await (await s.get(f"http://localhost:{G_HTTP}/health")).json()
            oh = await (await s.get(f"http://localhost:{O_HTTP}/health")).json()
            assert gh["role"] == "ground" and oh["role"] == "onboard"
            assert oh["onboard"]["recorder_active"]
            print(f"  onboard: {oh['onboard']['source']} · "
                  f"fresh {oh['onboard']['source_fresh_s']}s · "
                  f"profile {oh['link_profile']}")

            async def listen():
                async with s.ws_connect(f"http://localhost:{G_HTTP}/ws") as ws:
                    async for msg in ws:
                        ev = json.loads(msg.data)
                        if ev["type"] == "state":
                            got["states"] += 1
                        elif ev["type"] == "incident":
                            got["incident_via"] = ev.get("via")
                        elif ev["type"] == "segment" and ev.get("verified"):
                            got["verified"][ev["tier"]] = ev["coverage"]
                            if 1 in got["verified"] and 2 in got["verified"]:
                                return

            task = asyncio.create_task(listen())
            await asyncio.sleep(3)
            assert got["states"] > 5, "no live state crossed process boundary"
            # trigger the fall on the ONBOARD process's harness
            await s.post(f"http://localhost:{O_HTTP}/op", json={"cmd": "fall"})
            await asyncio.wait_for(task, timeout=90)
        assert got["incident_via"] == "link"
        c1, c2 = got["verified"][1], got["verified"][2]
        print(f"  cross-process: {got['states']}+ state frames, incident via "
              f"link, verified T1 {c1} / T2 {c2}")
        print("SPLIT ROLES: ALL PASS")
    finally:
        for p in (onboard, ground):
            p.send_signal(signal.SIGTERM)
        for p in (onboard, ground):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


asyncio.run(main())
