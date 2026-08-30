"""ICEBOX — one-command demo server.

    python -m icebox.server --source sim            # rehearsal / fallback
    python -m icebox.server --source phone          # live shove (Sensor Logger)
    python -m icebox.server --source simarm         # arm-grab incident (sim)
    python -m icebox.server --source arm            # venue manipulator (wire SDK)

Onboard side (would run on the Jetson Thor): source -> ring buffer ->
crash trigger -> report builder -> SBD packets + 1 Hz heartbeats -> SatLink.
Ground side: SatLink rx -> reassembler -> analysis -> dashboard websocket.
Only SBD-framed datagrams at 2 kbps connect the two sides.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import time

from aiohttp import web, WSMsgType

from . import blackbox as bb
from .linksim import SatLink
from .telemetry import SOURCES

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
REPORTS = ROOT / "reports"


class Onboard:
    """Everything that would run on the robot."""

    def __init__(self, source, link: SatLink, ground):
        self.source = source
        self.link = link
        self.ground = ground        # debug channel only (op events, not telemetry)
        self.ring = bb.RingBuffer(seconds=60, nominal_hz=getattr(source, "rate_hz", 200))
        self.trigger = bb.CrashTrigger()
        self.state = bb.STATE_NOMINAL
        self.stream_seq = 0
        self.gap_buf: list = []            # samples held during link blackout
        self.gap_id = 1000                 # id space separate from crash reports
        # continue numbering across restarts so persisted .ibx files and a
        # still-open dashboard never collide with a stale report id
        try:
            ids = [int(p.stem.split("_")[1]) for p in REPORTS.glob("report_*.ibx")
                   if p.stem.split("_")[1].isdigit()]
            self.report_id = max(ids, default=0)
        except Exception:
            self.report_id = 0
        self.hb_seq = 0
        self.pending_packets: list[bytes] = []
        self.last_cause = ""

    async def run(self):
        asyncio.create_task(self._heartbeat_loop())
        asyncio.create_task(self._stream_loop())
        async for s in self.source.stream():
            self.ring.append(s)
            cause = self.trigger.check(s)
            if cause and self.state == bb.STATE_NOMINAL:
                asyncio.create_task(self._incident(s.t, cause))

    async def _incident(self, t: float, cause: str):
        self.state = bb.STATE_INCIDENT
        self.last_cause = cause
        self.report_id += 1
        rid = self.report_id
        self.ground.op_event({"type": "incident", "report": rid, "cause": cause,
                              "t": t})
        await asyncio.sleep(2.2)            # let the post-impact window fill
        rep = bb.build_report(self.ring, t, report_id=rid)
        self.state = bb.STATE_TRANSMITTING
        flushed = self.link.flush_lo()      # newer incident supersedes old burst
        if flushed:
            self.ground.op_event({"type": "burst_superseded", "report": rid,
                                  "flushed": flushed})
        packets = rep["packets"]
        if self.link.loss > 0:
            # no reverse channel for ARQ at 2 kbps: cheap 2x redundancy keeps
            # the preview alive under injected loss (reassembler dedups seqs)
            packets = packets + packets
        self.ground.op_event({"type": "burst_start", "report": rid,
                              "sizes": rep["sizes"], "packets": len(packets),
                              "cause": cause})
        for pkt in packets:
            self.link.send(pkt, priority=False)
        # persist raw report onboard (the "black box survives" artifact)
        REPORTS.mkdir(exist_ok=True)
        (REPORTS / f"report_{rid:03d}.ibx").write_bytes(b"".join(rep["packets"]))
        # watch for delivery
        asyncio.create_task(self._await_drain(rid))

    async def _await_drain(self, rid: int):
        while self.link.queued > 0:
            await asyncio.sleep(0.2)
        self.state = bb.STATE_DELIVERED
        await asyncio.sleep(3.0)
        if self.state == bb.STATE_DELIVERED:
            self.state = bb.STATE_NOMINAL

    async def _stream_loop(self):
        """NeverDrop live channel: state frames at 8 Hz (6 Hz with joints).
        During a blackout, frames buffer onboard; on restore the backlog is
        compressed into one tier-3 gap segment and backfilled behind the
        live stream, so mission control ends up with zero holes."""
        was_up = True
        while True:
            s = self.ring.latest
            if s is not None:
                if self.link.up:
                    if not was_up and len(self.gap_buf) >= 4:
                        self.gap_id += 1
                        seg = bb.encode_segment(bb.decimate(self.gap_buf, 12.5),
                                                tier=3, report_id=self.gap_id)
                        for p in bb.packetize(seg, self.gap_id, 3):
                            self.link.send(p, priority=False)
                        self.gap_buf = []
                    # live frames leapfrog any backlog (burst or backfill)
                    pri = self.link.queued > 5
                    self.link.send(bb.pack_state(self.stream_seq, s), priority=pri)
                    self.stream_seq += 1
                else:
                    self.gap_buf.append(s)
                was_up = self.link.up
            await asyncio.sleep(1.0 / (6.0 if (s and s.joints) else 8.0))

    async def _heartbeat_loop(self):
        while True:
            s = self.ring.latest
            if s is not None:
                hb = bb.pack_heartbeat(self.hb_seq, s, self.state,
                                       self.link.queued, pct=100)
                self.link.send(hb, priority=True)
                self.hb_seq += 1
            await asyncio.sleep(1.0)


class Ground:
    """Ground station: reassembles what crossed the link, serves dashboard."""

    def __init__(self):
        self.reasm = bb.Reassembler()
        # one outbound queue per client; a single writer task per socket
        # (concurrent ws.send_str from many tasks is not safe in aiohttp)
        self.clients: set[asyncio.Queue] = set()
        self.link: SatLink | None = None
        self.history: list[dict] = []       # replayable event log

    def on_pkt(self, data: bytes):
        st = bb.unpack_state(data)
        if st:
            st["rx_t"] = time.time()
            self.broadcast(st)
            return
        hb = bb.unpack_heartbeat(data)
        if hb:
            hb["rx_t"] = time.time()
            self.broadcast(hb)
            return
        info = self.reasm.feed(data)
        if not info:
            return
        info["rx_t"] = time.time()
        if info["type"] == "segment" and info["segment"]["tier"] == 3:
            seg = info.pop("segment")                     # blackout backfill
            self.broadcast({"type": "gap", "gap_id": seg["report_id"],
                            "t0": seg["t0"], "rate": seg["rate"], "n": seg["n"],
                            "quat": seg["quat"], "accel": seg["accel"],
                            "joints": seg["joints"], "rx_t": time.time()})
            return
        if info["type"] == "segment":
            seg = info.pop("segment")
            self.broadcast({**info, "type": "packet"})   # final packet tick
            payload = {"type": "segment", "report": seg["report_id"],
                       "tier": seg["tier"], "t0": seg["t0"], "rate": seg["rate"],
                       "n": seg["n"], "n_joints": seg["n_joints"],
                       "quat": seg["quat"], "gyro": seg["gyro"],
                       "accel": seg["accel"], "joints": seg["joints"],
                       "rx_t": info["rx_t"]}
            self.broadcast(payload)
            if seg["tier"] >= 1:
                analysis = bb.analyze(seg)
                self.broadcast({"type": "analysis", "report": seg["report_id"],
                                "tier": seg["tier"], **analysis,
                                "rx_t": time.time()})
                REPORTS.mkdir(exist_ok=True)
                (REPORTS / f"report_{seg['report_id']:03d}_analysis.json").write_text(
                    json.dumps(analysis, indent=2))
        else:
            self.broadcast(info)

    def op_event(self, ev: dict):
        ev["rx_t"] = time.time()
        self.broadcast(ev)

    def broadcast(self, obj: dict):
        self.history.append(obj)
        if len(self.history) > 20000:
            del self.history[:5000]
        msg = json.dumps(obj)
        for q in list(self.clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass                      # slow client: drop, don't block


async def main():
    ap = argparse.ArgumentParser(description="ICEBOX demo server")
    ap.add_argument("--source", default="sim", choices=list(SOURCES))
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--bps", type=int, default=2000)
    ap.add_argument("--loss", type=float, default=0.0)
    ap.add_argument("--latency", type=float, default=0.35)
    args = ap.parse_args()

    source = SOURCES[args.source]()
    ground = Ground()
    # UDP link port derived from the HTTP port so two instances can coexist
    link = SatLink(bps=args.bps, loss=args.loss, latency=args.latency,
                   port=args.port + 39700)
    await link.start(ground.on_pkt)
    ground.link = link
    onboard = Onboard(source, link, ground)
    if args.source == "phone":
        # a phone strapped vertically in a boot reports ~90 deg "tilt" at
        # rest — disable the absolute-orientation trigger; the impact
        # (jerk + g spike) trigger is the phone demo's real tripwire
        onboard.trigger.tilt_limit = 1e9

    app = web.Application()

    async def index(_req):
        return web.FileResponse(WEB / "index.html")

    def apply_cmd(cmd: dict):
        op = cmd.get("cmd")
        if op == "fall":
            source.reset()              # self-arming: works repeatedly
            source.fall()
        elif op == "reset":
            source.reset()
            onboard.state = bb.STATE_NOMINAL
            # fresh trigger so a reset never inherits stale
            # baselines/debounce from the previous incident
            tl = onboard.trigger.tilt_limit
            onboard.trigger = bb.CrashTrigger(tilt_limit=tl)
            # clear the recorder too: the next incident's pre-impact window
            # must not contain the pre-reset pose transient
            onboard.ring.buf.clear()
        elif op == "arm":
            onboard.trigger.arm(bool(cmd.get("on", True)))
        elif op == "link":
            if "bps" in cmd:
                link.bps = max(300, int(cmd["bps"]))
            if "loss" in cmd:
                link.loss = min(0.5, max(0.0, float(cmd["loss"])))
            if "up" in cmd:
                link.up = bool(cmd["up"])

    async def ws_handler(req):
        """Downstream-only: op commands arrive via POST /op (stateless,
        immune to any websocket upstream weirdness)."""
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(req)
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=5000)
        await ws.send_str(json.dumps({"type": "hello", "source": args.source,
                                      "bps": link.bps}))
        # replay incident history so a refreshed dashboard is never blank
        replay_types = {"incident", "burst_start", "packet", "segment", "analysis"}
        for ev in list(ground.history):
            if ev.get("type") in replay_types:
                await ws.send_str(json.dumps({**ev, "replayed": True}))
        ground.clients.add(q)

        async def writer():
            while True:
                await ws.send_str(await q.get())

        wtask = asyncio.create_task(writer())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:      # legacy path, still works
                    apply_cmd(json.loads(msg.data))
        finally:
            wtask.cancel()
            ground.clients.discard(q)
        return ws

    async def op_handler(req):
        apply_cmd(await req.json())
        return web.json_response({"ok": True})

    async def phone_handler(req):
        body = await req.json()
        fed = source.feed_http(body) if hasattr(source, "feed_http") else 0
        return web.json_response({"ok": True, "fed": fed})

    async def status_loop():
        while True:
            latest = onboard.ring.latest
            ground.broadcast({
                "type": "status", "rx_t": time.time(),
                "state": bb.STATE_NAMES[onboard.state],
                "kbps": round(link.kbps_now(), 2), "bps_cap": link.bps,
                "queued": link.queued, "sent_pkts": link.sent_pkts,
                "dropped": link.dropped_pkts, "loss": link.loss,
                "up": link.up,
                "rate_hz": round(onboard.ring.rate_hz(), 1),
                "buffered_s": round(len(onboard.ring.buf) /
                                    max(1.0, onboard.ring.rate_hz()), 1),
                "source": args.source,
                "have_data": latest is not None,
                "armed": onboard.trigger.st.armed})
            await asyncio.sleep(0.5)

    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/op", op_handler)
    app.router.add_post("/phone", phone_handler)
    app.router.add_static("/static", WEB)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", args.port)
    await site.start()
    print(f"ICEBOX up: http://localhost:{args.port}  source={args.source} "
          f"link={args.bps} bps  (phone push: http://<laptop-ip>:{args.port}/phone)")

    asyncio.create_task(status_loop())
    await onboard.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
