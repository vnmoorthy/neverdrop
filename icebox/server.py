"""NeverDrop — onboard recorder + ground station over a constrained lab link.

    python -m icebox.server --role all --source sim                # demo
    python -m icebox.server --role ground --port 8000 --listen-port 47700
    python -m icebox.server --role onboard --source sim \\
        --ground-host 192.168.1.20 --ground-port 47700

Truth boundary: Onboard holds NO reference to Ground. Every judge-visible
incident fact (incident id, cause, chunk counts, samples, analysis input)
crosses the link as versioned packets: INCIDENT_NOTICE, REPORT_MANIFEST,
REPORT_CHUNK, and is displayed only after decode. Local test-harness state
(simulator controls, link model knobs, onboard internals in --role all) is
broadcast with via="harness" and rendered in a separately labeled panel.

Delivery: durable reports persist to a SQLite outbox BEFORE first
transmission, ground ACKs coverage on a 270-byte reverse channel, onboard
selectively resends missing chunks, and a finite retry policy ends in an
explicit PARTIAL_FAILED status — never a silent drop. Ground persists its
inbox so a restart is idempotent and reports complete exactly once.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import struct
import time

from aiohttp import web, WSMsgType

from . import blackbox as bb
from . import wire
from . import outbox as ob
from .linksim import LinkProfile, SatLink
from .protocol_stats import (RATE_ARM_HZ, RATE_HUMANOID_HZ, RATE_THROTTLED_HZ)
from .telemetry import SOURCES

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
REPORTS = ROOT / "reports"
GAP_BUF_MAX = 9000        # bounded: ~20 min at stream rate


# ======================================================================
# ONBOARD — may only talk to: source, ring, outbox, link. No Ground.
# ======================================================================
class Onboard:
    def __init__(self, source, link: SatLink, outbox: ob.Outbox,
                 harness_cb=None):
        self.source = source
        self.link = link
        self.outbox = outbox
        self.harness = harness_cb or (lambda ev: None)   # local test panel only
        self.boot = int(time.time()) & 0xFFFFFFFF
        self.ring = bb.RingBuffer(seconds=60,
                                  nominal_hz=getattr(source, "rate_hz", 200))
        self.trigger = bb.CrashTrigger()
        self.state = bb.STATE_NOMINAL
        self.stream_seq = 0
        self._amax = 0.0
        self.hb_seq = 0
        self.gap_buf: list = []
        self.gap_drops = 0
        self.report_id = self.outbox.max_report_id()
        self.retry_events: int = 0

    # ------------------------------------------------------------ main loops
    async def run(self):
        asyncio.create_task(self._heartbeat_loop())
        asyncio.create_task(self._stream_loop())
        asyncio.create_task(self._retry_loop())
        self._resume_pending()
        async for s in self.source.stream():
            self.ring.append(s)
            a = (s.accel[0] ** 2 + s.accel[1] ** 2 + s.accel[2] ** 2) ** 0.5
            if a > self._amax:
                self._amax = a
            cause = self.trigger.check(s)
            if cause and self.state != bb.STATE_INCIDENT:
                asyncio.create_task(self._incident(s.t, cause))

    def _resume_pending(self):
        """After a restart: reload persisted reports, resume delivery with
        their ORIGINAL identity, and do not emit a duplicate incident."""
        for rep in self.outbox.pending():
            self._enqueue_report(rep, resume=True)
            self.harness({"type": "harness_activity", "what": "resume"})

    async def _incident(self, t: float, cause: str):
        self.state = bb.STATE_INCIDENT
        self.report_id += 1
        rid = self.report_id
        # incident notice: the FIRST thing ground may learn, over the link
        notice = wire.pack_incident_notice(self.boot, rid, cause, t)
        for _ in range(3):                       # small, repeated for loss
            self.link.send_durable(notice, SatLink.PRI_CONTROL)
        self.harness({"type": "harness_activity", "what": "trigger"})
        await asyncio.sleep(2.2)                 # post-impact window fills
        rep = bb.build_report(self.ring, t, report_id=rid)
        if not rep["sizes"]:
            self.state = bb.STATE_NOMINAL
            return
        self.state = bb.STATE_TRANSMITTING
        win = self.ring.window(t - 10.0, t + 2.0)
        t0, t1 = (win[0].t, win[-1].t) if win else (t, t)
        for tier, blob_key in ((1, "seg1"), (2, "seg2")):
            payload = rep[blob_key]
            n = rep["n1"] if tier == 1 else rep["n"]
            rate = (n - 1) / max(1e-6, (t1 - t0))
            self.outbox.add(self.boot, rid, tier, wire.KIND_CRASH, payload,
                            n, rate, t0, t1)      # persisted BEFORE tx
            self._archive_ndz(rid)
            self._enqueue_report(self.outbox.get(self.boot, rid, tier))
        asyncio.create_task(self._watch_delivery(rid))

    def _enqueue_report(self, rep: dict, resume: bool = False):
        pri = {1: SatLink.PRI_TIER1, 2: SatLink.PRI_TIER2,
               3: SatLink.PRI_BACKFILL}[rep["tier"]]
        manifest = wire.pack_manifest(rep["boot"], rep["report"], rep["kind"],
                                      rep["tier"], rep["total"],
                                      rep["n_samples"], rep["rate"],
                                      rep["t0"], rep["t1"], rep["payload"])
        self.link.send_durable(manifest, SatLink.PRI_CONTROL)
        seqs = (self.outbox.missing_chunks(rep["boot"], rep["report"], rep["tier"])
                if resume else range(rep["total"]))
        chunks = bb.packetize(rep["payload"], rep["report"], rep["tier"])
        for seq in seqs:
            self.link.send_durable(chunks[seq], pri)
        self.outbox.mark_sending(rep["boot"], rep["report"], rep["tier"])

    def on_ack(self, data: bytes):
        ack = wire.unpack_ack(data)
        if not ack or ack["boot"] != self.boot:
            # ACK for an old boot: ignore; that report will resume by policy
            if ack:
                self.outbox.apply_ack(ack)       # old-boot reports may finish
            return
        new = self.outbox.apply_ack(ack)
        if new == ob.ST_DELIVERED:
            self._archive_ndz(ack["report"])
            self.harness({"type": "harness_activity", "what": "delivered"})

    async def _retry_loop(self):
        """Selective resend of missing chunks, finite policy, explicit end."""
        while True:
            await asyncio.sleep(4.0)
            if not self.link.up or self.link.durable_pending > 0:
                continue                          # let the first pass finish
            for rep in self.outbox.pending():
                missing = self.outbox.missing_chunks(
                    rep["boot"], rep["report"], rep["tier"])
                if not missing:
                    continue
                status = self.outbox.bump_round(
                    rep["boot"], rep["report"], rep["tier"])
                if status == ob.ST_PARTIAL_FAILED:
                    self.harness({"type": "harness_activity",
                                  "what": "partial_failed"})
                    continue
                pri = {1: SatLink.PRI_TIER1, 2: SatLink.PRI_TIER2,
                       3: SatLink.PRI_BACKFILL}[rep["tier"]]
                chunks = bb.packetize(rep["payload"], rep["report"], rep["tier"])
                for seq in missing[:64]:
                    self.link.send_durable(chunks[seq], pri)
                self.retry_events += len(missing[:64])

    async def _watch_delivery(self, rid: int):
        while True:
            await asyncio.sleep(1.0)
            t1 = self.outbox.get(self.boot, rid, 1)
            t2 = self.outbox.get(self.boot, rid, 2)
            sts = {r["status"] for r in (t1, t2) if r}
            if sts <= {ob.ST_DELIVERED}:
                self.state = bb.STATE_DELIVERED
                await asyncio.sleep(3.0)
                if self.state == bb.STATE_DELIVERED:
                    self.state = bb.STATE_NOMINAL
                return
            if ob.ST_PARTIAL_FAILED in sts:
                self.state = bb.STATE_NOMINAL
                return

    def _archive_ndz(self, rid: int):
        REPORTS.mkdir(exist_ok=True)
        recs = []
        status = ob.ST_DELIVERED
        kind = wire.KIND_CRASH
        for tier in (1, 2, 3):
            r = self.outbox.get(self.boot, rid, tier)
            if not r:
                continue
            kind = r["kind"]
            if r["status"] != ob.ST_DELIVERED:
                status = r["status"]
            recs.append((tier, {"n_samples": r["n_samples"], "rate": r["rate"],
                                "t0": r["t0"], "t1": r["t1"],
                                "total": r["total"], "status": r["status"]},
                         r["payload"]))
        if recs:
            ob.write_ndz(str(REPORTS / f"report_{rid:05d}.ndz"),
                         wire.MISSION_ID, self.boot, rid, kind, status, recs)

    # ------------------------------------------------------------ streaming
    async def _stream_loop(self):
        was_up = True
        while True:
            s = self.ring.latest
            if s is not None:
                if self.link.up:
                    if not was_up:
                        self._flush_gap()
                    amax, self._amax = self._amax, 0.0
                    self.link.set_slot("state",
                                       bb.pack_state(self.stream_seq, s,
                                                     amag_g=amax or None))
                    self.stream_seq += 1
                else:
                    if len(self.gap_buf) >= GAP_BUF_MAX:
                        self.gap_buf.pop(0)
                        self.gap_drops += 1
                    self.gap_buf.append(s)
                was_up = self.link.up
            if self.link.durable_pending > 0:
                rate = RATE_THROTTLED_HZ
            else:
                rate = RATE_ARM_HZ if (s and s.joints) else RATE_HUMANOID_HZ
            await asyncio.sleep(1.0 / rate)

    def _flush_gap(self):
        if len(self.gap_buf) < 4:
            self.gap_buf = []
            return
        samples = bb.decimate(self.gap_buf, 12.5)
        payload = bb.encode_segment(samples, tier=3, report_id=0)
        self.report_id += 1
        rid = self.report_id
        payload = bb.encode_segment(samples, tier=3, report_id=rid)
        self.outbox.add(self.boot, rid, 3, wire.KIND_BACKFILL, payload,
                        len(samples),
                        (len(samples) - 1) / max(1e-6,
                                                 samples[-1].t - samples[0].t),
                        samples[0].t, samples[-1].t)
        self._enqueue_report(self.outbox.get(self.boot, rid, 3))
        self.gap_buf = []

    async def _heartbeat_loop(self):
        while True:
            s = self.ring.latest
            if s is not None:
                self.link.set_slot("hb", bb.pack_heartbeat(
                    self.hb_seq, s, self.state, self.link.durable_pending, 100))
                self.hb_seq += 1
            await asyncio.sleep(1.0)

    # ------------------------------------------------------------ harness API
    def health(self) -> dict:
        s = self.ring.latest
        return {"source": type(self.source).__name__,
                "source_fresh_s": round(time.time() - s.t, 2) if s else None,
                "recorder_active": s is not None,
                "outbox": self.outbox.status_counts(),
                "link_profile": self.link.p.label,
                "link_up": self.link.up,
                "durable_pending_pkts": self.link.durable_pending,
                "boot": self.boot, "retry_chunks_sent": self.retry_events,
                "gap_buffered": len(self.gap_buf),
                "gap_dropped": self.gap_drops}


# ======================================================================
# GROUND — knows only what arrived on the link.
# ======================================================================
class Ground:
    def __init__(self, inbox: ob.Inbox, link: SatLink):
        self.inbox = inbox
        self.link = link
        self.clients: set[asyncio.Queue] = set()
        self.history: list[dict] = []
        # restart-idempotent: restore the boot scope from persisted manifests
        # so resumed chunk delivery matches its original identity
        row = self.inbox.con.execute("SELECT MAX(boot) FROM manifests").fetchone()
        self.boot: int | None = row[0] if row and row[0] else None
        self.pre: dict = {}          # bounded pre-manifest chunk cache
        self.rx_bytes = 0
        asyncio.create_task(self._ack_loop())

    # ------------------------------------------------------------ rx path
    def on_pkt(self, data: bytes):
        self.rx_bytes += len(data)
        st = bb.unpack_state(data)
        if st:
            self._ws({**st, "via": "link", "rx_t": time.time()})
            return
        hb = bb.unpack_heartbeat(data)
        if hb:
            self._ws({**hb, "via": "link", "rx_t": time.time()})
            return
        notice = wire.unpack_incident_notice(data)
        if notice:
            self._boot_gate(notice["boot"])
            self._ws({"type": "incident", "report": notice["report"],
                      "cause": notice["cause"], "boot": notice["boot"],
                      "via": "link", "rx_t": time.time()})
            return
        man = wire.unpack_manifest(data)
        if man:
            self._boot_gate(man["boot"])
            if not self.inbox.put_manifest(man):
                self._ws({"type": "manifest_conflict", "report": man["report"],
                          "tier": man["tier"], "via": "link"})
                return
            self._ws({"type": "manifest", "via": "link", "rx_t": time.time(),
                      **{k: man[k] for k in ("boot", "report", "tier", "kind_name",
                                             "total", "n_samples", "rate")}})
            self._adopt_premanifest(man)
            self._try_complete(man["boot"], man["report"], man["tier"])
            return
        if data[:2] == bb.PKT_MAGIC:
            self._on_chunk(data)

    def _boot_gate(self, boot: int):
        if self.boot is None or boot > self.boot:
            self.boot = boot
            self.pre.clear()
            self.inbox.drop_boot_except(boot)

    def _on_chunk(self, data: bytes):
        if len(data) < bb.PKT_HEAD.size:
            return
        magic, report, tier, seq, total, kind, crc = bb.PKT_HEAD.unpack_from(data)
        payload = data[bb.PKT_HEAD.size:]
        boot = self.boot or 0
        if not wire.validate_chunk_header(report, tier, seq, total, len(payload)):
            return                                    # hostile/corrupt header
        if bb.crc16(payload) != crc:
            self.inbox.count_corrupt(boot, report, tier)
            self._ws({"type": "progress", "report": report, "tier": tier,
                      "corrupt": True, "via": "link"})
            return
        m = self.inbox.manifest(boot, report, tier)
        if not m:
            key = (report, tier)
            if key not in self.pre and len(self.pre) >= wire.MAX_OPEN_REPORTS:
                return                       # bound BEFORE allocating the key
            cache = self.pre.setdefault(key, {})
            if len(cache) < wire.MAX_PREMANIFEST_CHUNKS:
                cache[seq] = payload
            return
        if m["total"] != total:
            return                                    # conflicts with manifest
        res = self.inbox.put_chunk(boot, report, tier, seq, payload)
        got = len(self.inbox.received_seqs(boot, report, tier))
        m2 = self.inbox.manifest(boot, report, tier)
        self._ws({"type": "progress", "report": report, "tier": tier,
                  "seq": seq, "got": got, "total": m["total"],
                  "dups": m2["dups"], "corrupt": m2["corrupt"],
                  "dup": res == "dup", "via": "link", "rx_t": time.time()})
        self._try_complete(boot, report, tier)

    def _adopt_premanifest(self, man: dict):
        cache = self.pre.pop((man["report"], man["tier"]), {})
        for seq, payload in cache.items():
            if seq < man["total"]:
                self.inbox.put_chunk(man["boot"], man["report"], man["tier"],
                                     seq, payload)

    def _try_complete(self, boot, report, tier):
        if self.inbox.already_verified(boot, report, tier):
            return
        payload = self.inbox.assemble(boot, report, tier)
        if payload is None:
            return
        m = self.inbox.manifest(boot, report, tier)
        self._send_ack(boot, report, tier, complete=True)
        try:
            seg = bb.decode_segment(payload)
        except Exception:
            return
        base = {"report": report, "tier": tier, "via": "link",
                "rx_t": time.time(), "verified": True,
                "coverage": f"{m['total']}/{m['total']}",
                "dups": m["dups"], "corrupt": m["corrupt"]}
        if tier == 3:
            self._ws({**base, "type": "gap", "gap_id": report,
                      "t0": seg["t0"], "rate": seg["rate"], "n": seg["n"],
                      "quat": seg["quat"], "accel": seg["accel"],
                      "joints": seg["joints"], "declared_rate": m["rate"]})
            return
        self._ws({**base, "type": "segment", "t0": seg["t0"],
                  "rate": seg["rate"], "n": seg["n"],
                  "n_joints": seg["n_joints"], "quat": seg["quat"],
                  "gyro": seg["gyro"], "accel": seg["accel"],
                  "joints": seg["joints"]})
        analysis = bb.analyze(seg)
        label = "REFINED" if tier == 2 else "PRELIMINARY"
        conf = analysis["confidence"] - (0.1 if tier == 1 else 0.0)
        self._ws({**base, "type": "analysis", "label": label,
                  **{**analysis, "confidence": round(max(0.1, conf), 2)}})

    # ------------------------------------------------------------ ACK path
    def _send_ack(self, boot, report, tier, complete=False):
        m = self.inbox.manifest(boot, report, tier)
        if not m:
            return
        seqs = self.inbox.received_seqs(boot, report, tier)
        highest = -1
        for s in range(m["total"]):
            if s in seqs:
                highest = s
            else:
                break
        missing = [s for s in range(m["total"]) if s not in seqs]
        self.link.send_reverse(wire.pack_ack(
            boot, report, tier, bytes(m["sha16"]), max(0, highest),
            missing, complete or not missing))

    async def _ack_loop(self):
        while True:
            await asyncio.sleep(2.0)
            if self.boot is None:
                continue
            rows = self.inbox.con.execute(
                "SELECT report, tier FROM manifests WHERE verified=0 AND boot=?",
                (self.boot,)).fetchall()
            for report, tier in rows[:4]:
                self._send_ack(self.boot, report, tier)

    # ------------------------------------------------------------ ws fanout
    def _ws(self, obj: dict):
        self.history.append(obj)
        if len(self.history) > 20000:
            del self.history[:5000]
        msg = json.dumps(obj)
        for q in list(self.clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass


# ======================================================================
# HARNESS / APP WIRING
# ======================================================================
def build_app(args, onboard: Onboard | None, ground: Ground | None,
              link: SatLink, source):
    app = web.Application()

    async def index(_):
        return web.FileResponse(WEB / "index.html")

    async def health(_):
        h = {"role": args.role, "link_profile": link.p.label,
             "link_up": link.up}
        if onboard:
            h["onboard"] = onboard.health()
        if ground:
            h["ground"] = {"boot": ground.boot,
                           "open_reports": len(ground.pre),
                           "rx_bytes": ground.rx_bytes}
        return web.json_response(h)

    def apply_cmd(cmd: dict):
        op = cmd.get("cmd")
        if source is None:
            return
        if op == "fall":
            source.reset()
            source.fall()
        elif op == "reset":
            source.reset()
            if onboard:
                onboard.state = bb.STATE_NOMINAL
                tl = onboard.trigger.tilt_limit
                onboard.trigger = bb.CrashTrigger(tilt_limit=tl)
                onboard.ring.buf.clear()
        elif op == "arm" and onboard:
            onboard.trigger.arm(bool(cmd.get("on", True)))
        elif op == "link":
            if "bps" in cmd:
                link.p.bps = max(300, int(cmd["bps"]))
            if "loss" in cmd:
                link.p.loss = min(0.5, max(0.0, float(cmd["loss"])))
            if "up" in cmd:
                link.up = bool(cmd["up"])

    async def op_handler(req):
        apply_cmd(await req.json())
        return web.json_response({"ok": True, "harness": True})

    async def phone_handler(req):
        body = await req.json()
        fed = source.feed_http(body) if hasattr(source, "feed_http") else 0
        return web.json_response({"ok": True, "fed": fed})

    async def ws_handler(req):
        if ground is None:
            raise web.HTTPNotFound()
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(req)
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)
        await ws.send_str(json.dumps({
            "type": "hello", "source": args.source, "role": args.role,
            "link_profile": link.p.label, "satellite_hardware": "NONE",
            "source_kind": ("SYNTHETIC" if args.source in ("sim", "simarm")
                            else "REAL MEASUREMENT"),
            "via": "harness"}))
        replay = {"incident", "manifest", "progress", "segment", "analysis",
                  "gap"}
        for ev in list(ground.history):
            if ev.get("type") in replay:
                await ws.send_str(json.dumps({**ev, "replayed": True}))
        ground.clients.add(q)

        async def writer():
            while True:
                await ws.send_str(await q.get())

        wtask = asyncio.create_task(writer())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    apply_cmd(json.loads(msg.data))
        finally:
            wtask.cancel()
            ground.clients.discard(q)
        return ws

    async def status_loop():
        """LOCAL TEST HARNESS feed: never crossed the link, labeled as such."""
        while True:
            if ground:
                ev = {"type": "status", "via": "harness", "rx_t": time.time(),
                      "kbps": round(link.kbps_now(), 2), "bps_cap": link.p.bps,
                      "queued": link.queued,
                      "durable_pending": link.durable_pending,
                      "coalesced": link.coalesced,
                      "sent_pkts": link.sent_pkts, "dropped": link.dropped_pkts,
                      "loss": link.p.loss, "up": link.up,
                      "link_profile": link.p.label,
                      "source": args.source,
                      "source_kind": ("SYNTHETIC" if args.source in
                                      ("sim", "simarm") else "REAL MEASUREMENT")}
                if onboard:
                    latest = onboard.ring.latest
                    ev.update({
                        "state": bb.STATE_NAMES[onboard.state],
                        "rate_hz": round(onboard.ring.rate_hz(), 1),
                        "buffered_s": round(len(onboard.ring.buf) /
                                            max(1.0, onboard.ring.rate_hz()), 1),
                        "gap_held": len(onboard.gap_buf),
                        "outbox": onboard.outbox.status_counts(),
                        "retry_chunks": onboard.retry_events,
                        "have_data": latest is not None,
                        "armed": onboard.trigger.st.armed})
                ground._ws(ev)
            await asyncio.sleep(0.5)

    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/op", op_handler)
    app.router.add_post("/phone", phone_handler)
    app.router.add_static("/static", WEB)
    app["status_loop"] = status_loop
    return app


async def main(argv=None):
    ap = argparse.ArgumentParser(description="NeverDrop server")
    ap.add_argument("--role", default="all", choices=["all", "onboard", "ground"])
    ap.add_argument("--source", default="sim", choices=list(SOURCES))
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--listen-port", type=int, default=None,
                    help="UDP link port (default: HTTP port + 39700)")
    ap.add_argument("--ground-host", default="127.0.0.1")
    ap.add_argument("--ground-port", type=int, default=None)
    ap.add_argument("--link-profile", default="lab-2kbps",
                    choices=["lab-2kbps", "iridium-sbd"])
    ap.add_argument("--bps", type=int, default=2000)
    ap.add_argument("--loss", type=float, default=0.0)
    ap.add_argument("--latency", type=float, default=0.35)
    ap.add_argument("--sbd-latency", type=float, default=8.0)
    ap.add_argument("--sbd-success", type=float, default=0.95)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    args = ap.parse_args(argv)

    udp_port = (args.listen_port or args.ground_port or args.port + 39700)
    profile = LinkProfile(args.link_profile, bps=args.bps, loss=args.loss,
                          latency=args.latency, sbd_latency=args.sbd_latency,
                          sbd_success=args.sbd_success, seed=args.seed)
    data = pathlib.Path(args.data_dir)
    data.mkdir(exist_ok=True)

    source = SOURCES[args.source]() if args.role in ("all", "onboard") else None
    onboard = ground = None

    link = SatLink(profile, port=udp_port,
                   dest_host=(args.ground_host if args.role == "onboard"
                              else "127.0.0.1"),
                   listen=args.role in ("all", "ground"))

    if args.role in ("all", "ground"):
        ground = Ground(ob.Inbox(str(data / "ground_inbox.sqlite")), link)
    if args.role in ("all", "onboard"):
        harness_cb = ((lambda ev: ground._ws({**ev, "via": "harness"}))
                      if ground else None)
        onboard = Onboard(source, link,
                          ob.Outbox(str(data / "onboard_outbox.sqlite")),
                          harness_cb=harness_cb)
        if args.source == "phone":
            onboard.trigger.tilt_limit = 1e9

    await link.start(ground.on_pkt if ground else None,
                     onboard.on_ack if onboard else None)

    app = build_app(args, onboard, ground, link, source)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", args.port).start()
    print(f"NeverDrop up: role={args.role} http=:{args.port} udp=:{udp_port} "
          f"profile={profile.label} source={args.source}", flush=True)

    if ground:
        asyncio.create_task(app["status_loop"]())
    if onboard:
        await onboard.run()
    else:
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
