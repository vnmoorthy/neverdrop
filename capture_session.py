"""Capture a scripted live session's ws events -> docs/session.json.

Drives the REAL pipeline (fresh server, genuine 2 kbps link) through the
full demo arc and records every dashboard event with relative timestamps,
so GitHub Pages can replay an honest session through the real UI.
"""
import asyncio
import json
import pathlib
import time

import aiohttp

BASE = "http://localhost:8000"
OUT = pathlib.Path(__file__).parent / "docs" / "session.json"


async def main():
    events = []
    t0 = time.time()
    done = asyncio.Event()

    async with aiohttp.ClientSession() as s:

        async def op(cmd, **kw):
            await s.post(BASE + "/op", json={"cmd": cmd, **kw})

        async def recorder():
            async with s.ws_connect(BASE + "/ws") as ws:
                got_tier2 = False
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    ev = json.loads(msg.data)
                    events.append({"dt": round(time.time() - t0, 3), "ev": ev})
                    if ev["type"] == "segment" and ev.get("tier") == 2:
                        got_tier2 = True
                    if got_tier2 and ev["type"] == "hb" and ev["state"] == 0:
                        done.set()          # tier-2 landed, back to NOMINAL
                        return

        rec = asyncio.create_task(recorder())

        print("act 1: live twin (20 s)")
        await asyncio.sleep(20)
        print("act 2: blackout (10 s)")
        await op("link", up=False)
        await asyncio.sleep(10)
        print("act 3: restore + backfill (18 s)")
        await op("link", up=True)
        await asyncio.sleep(18)
        print("act 4: the crash")
        await op("fall")
        try:
            await asyncio.wait_for(done.wait(), timeout=200)
        except asyncio.TimeoutError:
            print("warning: tier-2 completion not observed within cap")
        rec.cancel()

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(events, separators=(",", ":")))
    kinds = {}
    for e in events:
        kinds[e["ev"]["type"]] = kinds.get(e["ev"]["type"], 0) + 1
    print(f"captured {len(events)} events over {events[-1]['dt']:.0f}s "
          f"-> {OUT} ({OUT.stat().st_size//1024} KB)")
    print("event mix:", dict(sorted(kinds.items())))


asyncio.run(main())
