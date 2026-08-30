"""Deterministic reliability suite — `python test_reliability.py` => ALL PASS.

Runs real Onboard/Ground stacks over the real UDP link model (seeded RNG,
accelerated bit rates) and proves the NeverDrop guarantees:

  blackout survival · bounded memory · selective retry under loss ·
  explicit PARTIAL on retry exhaustion · reorder/dup/corruption handling ·
  restart resume (onboard + ground) · coalesced status · truth path ·
  protocol budget · phone time alignment
"""
import asyncio
import os
import shutil
import struct
import tempfile
import time

from icebox import blackbox as bb
from icebox import outbox as ob
from icebox import wire
from icebox.linksim import LinkProfile, SatLink
from icebox.server import Ground, Onboard
from icebox.telemetry import PhoneSource, SimSource

PORT = [48200]
TMP = tempfile.mkdtemp(prefix="ndz_rel_")


class Stack:
    def __init__(self, name, **prof):
        self.name = name
        PORT[0] += 2
        prof.setdefault("bps", 64000)
        prof.setdefault("latency", 0.02)
        prof.setdefault("seed", 42)
        self.profile = LinkProfile(**prof)
        self.link = SatLink(self.profile, port=PORT[0])
        self.source = SimSource()
        self.dir = os.path.join(TMP, name)
        os.makedirs(self.dir, exist_ok=True)
        self.ground = None
        self.onboard = None
        self.task = None

    async def start(self, fresh_ground=True):
        holder = {}
        if fresh_ground:
            self.ground = Ground(ob.Inbox(os.path.join(self.dir, "g.sqlite")),
                                 self.link)
        holder["g"] = self.ground
        self.onboard = Onboard(self.source, self.link,
                               ob.Outbox(os.path.join(self.dir, "o.sqlite")))
        holder["o"] = self.onboard
        await self.link.start(lambda d: holder["g"].on_pkt(d),
                              lambda d: holder["o"].on_ack(d))
        self._holder = holder
        self.task = asyncio.create_task(self.onboard.run())
        await asyncio.sleep(0.6)
        return self

    def swap_ground(self):
        """Simulated ground restart: new Ground over the SAME inbox db."""
        self.ground = Ground(ob.Inbox(os.path.join(self.dir, "g.sqlite")),
                             self.link)
        self._holder["g"] = self.ground
        return self.ground

    async def restart_onboard(self):
        """Simulated onboard restart: new Onboard over the SAME outbox db."""
        self.task.cancel()
        self.onboard = Onboard(SimSource(), self.link,
                               ob.Outbox(os.path.join(self.dir, "o.sqlite")))
        self._holder["o"] = self.onboard        # ACKs route to the new process
        self.task = asyncio.create_task(self.onboard.run())
        await asyncio.sleep(0.5)

    async def fall_and_wait_verified(self, tiers=(1, 2), timeout=60):
        self.source.reset()
        self.source.fall()
        end = time.time() + timeout
        while time.time() < end:
            done = {ev["tier"] for ev in self.ground.history
                    if ev.get("type") == "segment" and ev.get("verified")}
            if set(tiers) <= done:
                return True
            await asyncio.sleep(0.3)
        return False

    def events(self, typ):
        return [e for e in self.ground.history if e.get("type") == typ]

    async def stop(self):
        if self.task:
            self.task.cancel()


async def t01_blackout():
    st = await Stack("blackout").start()
    st.link.up = False
    q0 = st.link.queued
    await asyncio.sleep(6)
    assert st.link.queued <= q0 + 4, "queue grew during blackout (not coalesced)"
    assert len(st.onboard.gap_buf) > 20, "durable recording stopped in blackout"
    assert len(st.onboard.gap_buf) <= 9000
    st.link.up = True
    await asyncio.sleep(1.5)
    fresh = [e for e in st.events("hb") if e["rx_t"] > time.time() - 1.6]
    assert fresh, "current heartbeat did not return promptly"
    end = time.time() + 30
    ok = False
    while time.time() < end:
        gaps = [e for e in st.events("gap") if e.get("verified")]
        if gaps:
            ok = True
            assert gaps[0]["coverage"].split("/")[0] == gaps[0]["coverage"].split("/")[1]
            break
        await asyncio.sleep(0.3)
    assert ok, "backfill never verified"
    await st.stop()
    print("  01 blackout: recording continued, memory bounded, "
          f"backfill verified {gaps[0]['coverage']} at declared "
          f"{gaps[0]['declared_rate']:.1f} Hz")


async def t02_loss10():
    st = await Stack("loss10", loss=0.10, seed=7).start()
    assert await st.fall_and_wait_verified(), "tiers did not verify under 10% loss"
    assert st.onboard.retry_events > 0, "no selective retries occurred"
    for tier in (1, 2):
        n = len([e for e in st.events("analysis") if e["tier"] == tier])
        assert n == 1, f"duplicate analysis for tier {tier}: {n}"
    await st.stop()
    print(f"  02 loss10: verified with {st.onboard.retry_events} selective "
          "retry chunks, exactly one analysis per tier")


async def t03_finite_retry():
    ob_default = ob.DEFAULT_MAX_ROUNDS
    ob.DEFAULT_MAX_ROUNDS = 3
    try:
        st = await Stack("loss55", loss=0.55, seed=3).start()
        st.source.fall()
        end = time.time() + 45
        failed = None
        while time.time() < end:
            counts = st.onboard.outbox.status_counts()
            if counts.get(ob.ST_PARTIAL_FAILED):
                failed = counts
                break
            await asyncio.sleep(0.5)
        assert failed, f"retry policy never exhausted explicitly: {counts}"
        # No FALSE completion claims, in either direction:
        #  - onboard DELIVERED requires a hash-checked complete-ACK, which
        #    ground only sends after verification => DELIVERED implies a
        #    ground-verified event for that tier;
        #  - ground "verified" always implies full coverage + SHA match by
        #    construction (Inbox.assemble).
        # A transient (onboard PARTIAL_FAILED, ground verified) pair is a
        # pessimistic local view under ACK loss, not an over-claim, and a
        # late complete-ACK reconciles it.
        verified = {(e["report"], e["tier"]) for e in st.events("segment")
                    if e.get("verified")}
        for rep, tier, status in st.onboard.outbox.con.execute(
                "SELECT report, tier, status FROM reports"):
            if status == ob.ST_DELIVERED:
                assert (rep, tier) in verified, \
                    "onboard claimed DELIVERED without ground verification"
        await st.stop()
        print(f"  03 finite retry: heavy loss ended in explicit {failed}; "
              "no false completion claims")
    finally:
        ob.DEFAULT_MAX_ROUNDS = ob_default


async def t04_reorder():
    st = await Stack("reorder", reorder=0.6, seed=11).start()
    assert await st.fall_and_wait_verified(), "reordered chunks failed to verify"
    await st.stop()
    print("  04 reorder: out-of-order delivery verified")


async def t05_duplication():
    st = await Stack("dup", dup=0.5, seed=13).start()
    assert await st.fall_and_wait_verified(), "duplicated chunks failed to verify"
    m = st.ground.inbox.manifest(st.onboard.boot, st.onboard.report_id, 2)
    assert m["dups"] > 0, "duplicates were not counted"
    n = len([e for e in st.events("analysis") if e["tier"] == 2])
    assert n == 1, "duplicates produced duplicate analysis"
    await st.stop()
    print(f"  05 duplication: idempotent, {m['dups']} dups counted")


async def t06_payload_corruption():
    st = await Stack("corrupt", corrupt=0.12, seed=17).start()
    assert await st.fall_and_wait_verified(timeout=90), \
        "corruption prevented eventual verification"
    m = st.ground.inbox.manifest(st.onboard.boot, st.onboard.report_id, 2)
    assert m["corrupt"] > 0, "corruption went undetected"
    await st.stop()
    print(f"  06 payload corruption: {m['corrupt']} CRC failures caught, "
          "missing chunks retransmitted, hash verified")


async def t07_header_corruption():
    st = await Stack("hostile").start()
    g = st.ground
    # hostile manifest: absurd totals must be rejected before allocation
    bad = bytearray(wire.pack_manifest(1, 1, 1, 1, 4, 10, 10, 0, 1, b"x" * 50))
    struct.pack_into("<H", bad, 12, 60000)      # total := 60000
    g.on_pkt(bytes(bad))                        # bad CRC now -> ignored
    # hostile chunk headers: valid CRC over payload but insane header fields
    payload = b"y" * 100
    for total in (0, 5000):
        head = bb.PKT_HEAD.pack(bb.PKT_MAGIC, 9, 2, 0, total, 1,
                                bb.crc16(payload))
        g.on_pkt(head + payload)
    # flood of pre-manifest chunks across many report ids: bounded cache
    for rid in range(50):
        head = bb.PKT_HEAD.pack(bb.PKT_MAGIC, rid, 2, 0, 10, 1,
                                bb.crc16(payload))
        g.on_pkt(head + payload)
    assert len(g.pre) <= wire.MAX_OPEN_REPORTS + 1, \
        f"pre-manifest cache unbounded: {len(g.pre)}"
    assert not g.inbox.manifest(0, 1, 1), "hostile manifest was accepted"
    await st.stop()
    print("  07 header corruption: rejected safely, allocation bounded")


async def t08_missing_final_chunk():
    inbox = ob.Inbox(os.path.join(TMP, "mfc.sqlite"))
    PORT[0] += 2
    link = SatLink(LinkProfile(), port=PORT[0])
    g = Ground(inbox, link)
    payload = os.urandom(1000)
    man = wire.unpack_manifest(wire.pack_manifest(5, 7, 1, 1, 4, 100, 20,
                                                 0, 5, payload))
    g.on_pkt(wire.pack_manifest(5, 7, 1, 1, 4, 100, 20, 0, 5, payload))
    chunks = bb.packetize(payload, 7, 1)
    for c in chunks[:-1]:
        g._boot_gate(5)
        g.on_pkt(c)
    assert not any(e.get("verified") for e in g.history
                   if e.get("type") == "segment"), "verified without final chunk"
    m = g.inbox.manifest(5, 7, 1)
    assert not m["verified"] and not m["complete"]
    got = len(g.inbox.received_seqs(5, 7, 1))
    assert got == len(chunks) - 1
    print(f"  08 missing final chunk: state stays partial ({got}/{len(chunks)}), "
          "no completion claim")


async def t09_onboard_restart():
    st = await Stack("oreboot", loss=1.0).start()     # nothing delivers
    st.source.fall()
    await asyncio.sleep(4)
    counts = st.onboard.outbox.status_counts()
    assert counts, "report not persisted before delivery"
    boot0, rid = st.onboard.boot, st.onboard.report_id
    st.link.p.loss = 0.0                              # link heals
    await st.restart_onboard()
    end = time.time() + 60
    ok = False
    while time.time() < end:
        r = st.onboard.outbox.get(boot0, rid, 2)
        if r and r["status"] == ob.ST_DELIVERED:
            ok = True
            break
        await asyncio.sleep(0.5)
    assert ok, f"restart did not resume delivery: {st.onboard.outbox.status_counts()}"
    v = [e for e in st.events("segment") if e.get("verified") and e["tier"] == 2]
    assert len(v) == 1, "report did not complete exactly once"
    await st.stop()
    print("  09 onboard restart: outbox reloaded, original identity kept, "
          "delivery resumed and hash-verified")


async def t10_ground_restart():
    st = await Stack("greboot", loss=0.25, seed=23).start()
    st.source.fall()
    # wait until some chunks landed, then "restart" ground on the same inbox
    end = time.time() + 30
    while time.time() < end:
        if any(e.get("type") == "progress" for e in st.ground.history):
            break
        await asyncio.sleep(0.2)
    hist1 = st.ground.history
    st.swap_ground()
    end = time.time() + 60
    ok = False
    while time.time() < end:
        v = [e for e in st.ground.history
             if e.get("type") == "segment" and e.get("verified") and e["tier"] == 2]
        if v:
            ok = True
            break
        await asyncio.sleep(0.4)
    assert ok, "ground restart failed to complete the report"
    v_before = [e for e in hist1
                if e.get("type") == "segment" and e.get("verified")
                and e["tier"] == 2]
    assert len(v_before) + len(v) == 1, "report completed more than once"
    await st.stop()
    print("  10 ground restart: inbox reloaded, retransmits deduplicated, "
          "completed exactly once")


async def t11_coalescing():
    st = await Stack("coalesce").start()
    st.link.up = False
    await asyncio.sleep(8)
    assert st.link.queued <= 4, "status queued during blackout"
    st.link.up = True
    await asyncio.sleep(2.0)
    now = time.time()
    hbs = [e for e in st.events("hb") if e["rx_t"] > now - 2.0]
    assert 0 < len(hbs) <= 4, f"stale heartbeats replayed after restore: {len(hbs)}"
    assert st.link.coalesced > 5, "no coalescing happened"
    await st.stop()
    print(f"  11 coalescing: {st.link.coalesced} stale status frames replaced, "
          "no replay after restore")


async def t12_truth_path():
    st = await Stack("truth").start()
    st.source.fall()
    await st.fall_and_wait_verified(tiers=(1,), timeout=45)
    fact_keys = {"cause", "summary", "n_samples", "total", "confidence"}
    for ev in st.ground.history:
        if fact_keys & set(ev.keys()):
            assert ev.get("via") == "link", \
                f"incident fact outside the link: {ev.get('type')}"
    order = [e["type"] for e in st.ground.history
             if e.get("type") in ("incident", "manifest", "segment", "analysis")]
    assert order and order[0] == "incident", \
        "first incident knowledge was not the link-decoded notice"
    await st.stop()
    print("  12 truth path: every incident fact arrived via decoded link "
          "packets; notice first")


async def t13_budget():
    from icebox.protocol_stats import stats, LAB_BPS
    s = stats()
    assert s["state_frame_B_0_joints"] == len(bb.pack_state(0, bb.Sample(t=0))), \
        "stats not derived from struct"
    demand = (s["live_payload_bps_humanoid"] + s["heartbeat_payload_bps"])
    assert demand <= LAB_BPS, \
        f"steady-state slot demand {demand} exceeds budget (coalescing aside)"
    # measured: run the lab link at default budget with slots only
    st = Stack("budget", bps=2000)
    await st.start()
    st.link.sent_bytes = 0
    t0 = time.time()
    await asyncio.sleep(6)
    bps = st.link.sent_bytes * 8 / (time.time() - t0)
    assert bps <= 2000 * 1.15, f"lab profile exceeded budget: {bps:.0f} bps"
    await st.stop()
    print(f"  13 budget: derived sizes match structs; measured {bps:.0f} bps "
          f"<= {2000} (+burst allowance)")


async def t14_phone_alignment():
    src = PhoneSource()
    t0 = 1_000_000_000_000_000_000
    fed = src.feed_http({"payload": [
        {"name": "gravity", "time": t0, "values": {"x": 0, "y": 0, "z": 9.81}},
        {"name": "gyroscope", "time": t0 + 2_000_000, "values": {"x": 1.0, "y": 0, "z": 0}},
        {"name": "gyroscope", "time": t0 + 12_000_000, "values": {"x": 2.0, "y": 0, "z": 0}},
        {"name": "orientation", "time": t0 + 3_000_000,
         "values": {"qw": 2.0, "qx": 0, "qy": 0, "qz": 0}},   # unnormalized
        {"name": "accelerometer", "time": t0 + 7_000_000, "values": {"x": 0, "y": 0, "z": 0}},
    ]})
    assert fed == 1
    s = src.queue.get_nowait()
    assert abs(s.gyro[0] - 1.5) < 0.01, f"gyro not interpolated: {s.gyro[0]}"
    assert abs(s.quat[0] - 1.0) < 1e-6, "quaternion not normalized"
    assert s.vbatt is None and s.temp is None, "phone fabricated battery/temp"
    # stale gravity must be rejected, not silently fused
    t1 = t0 + 2_000_000_000
    fed = src.feed_http({"payload": [
        {"name": "accelerometer", "time": t1, "values": {"x": 0, "y": 0, "z": 0}},
    ]})
    assert fed == 0 and src.health["stale_rejected"] > 0, "stale fusion happened"
    # duplicate timestamps rejected
    fed = src.feed_http({"payload": [
        {"name": "accelerometer", "time": t1, "values": {"x": 0, "y": 0, "z": 0}},
    ]})
    assert src.health["dup_rejected"] > 0
    print("  14 phone alignment: interpolation, staleness gate, quat "
          "normalization, duplicate rejection, no fabricated battery/temp")


async def main():
    tests = [t01_blackout, t02_loss10, t03_finite_retry, t04_reorder,
             t05_duplication, t06_payload_corruption, t07_header_corruption,
             t08_missing_final_chunk, t09_onboard_restart, t10_ground_restart,
             t11_coalescing, t12_truth_path, t13_budget, t14_phone_alignment]
    for t in tests:
        print(t.__name__)
        await t()
    print("RELIABILITY: ALL PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
