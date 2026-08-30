"""Constrained-link models. THIS IS NOT SATELLITE HARDWARE.

Two deterministic lab models over real UDP sockets:

  lab-2kbps    token-bucket payload pacing at a configured bit rate, with
               configurable loss, latency, duplication, reordering and
               payload corruption. Seeded RNG for reproducible tests.
               UI label: LAB 2 KBPS MODEL.

  iridium-sbd  message-level sessions (no byte pipe): each message is one
               session with configurable latency and success probability,
               340 B mobile-originated / 270 B mobile-terminated limits.
               UI label: IRIDIUM SBD OPERATIONAL MODEL. This models the
               shape of SBD behavior; it is not a modem and its default
               latency is a documented assumption, not a measurement.

Traffic classes (documented priority policy, high to low):
  1. durable control (incident notice, manifests)      FIFO queue
  2. current heartbeat                                  latest-value slot
  3. current live state                                 latest-value slot
  4. durable chunks (tier-1 < backfill < tier-2)        priority queue

Slots coalesce: a newer heartbeat/state REPLACES the unsent one, so a
blackout never queues stale status, and stale status is never replayed as
current. Durable traffic is never dropped by the scheduler; under load the
scheduler guarantees at least one durable send per two slot sends.

A reverse channel (ground -> onboard) carries ACKs only, budgeted to the
SBD MT limit and paced ~1 message per interval.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import random
import time


class _Rx(asyncio.DatagramProtocol):
    def __init__(self, on_pkt):
        self.on_pkt = on_pkt

    def datagram_received(self, data, addr):
        self.on_pkt(data, addr)


class LinkProfile:
    def __init__(self, name="lab-2kbps", bps=2000, loss=0.0, latency=0.35,
                 dup=0.0, reorder=0.0, corrupt=0.0,
                 sbd_latency=8.0, sbd_success=0.95, seed=None):
        self.name = name
        self.bps, self.loss, self.latency = bps, loss, latency
        self.dup, self.reorder, self.corrupt = dup, reorder, corrupt
        self.sbd_latency, self.sbd_success = sbd_latency, sbd_success
        self.rng = random.Random(seed)

    @property
    def label(self):
        return ("IRIDIUM SBD OPERATIONAL MODEL" if self.name == "iridium-sbd"
                else f"LAB {self.bps/1000:g} KBPS MODEL")


class SatLink:
    """Forward channel onboard -> ground plus a reverse ACK channel."""

    PRI_CONTROL, PRI_TIER1, PRI_BACKFILL, PRI_TIER2 = 0, 1, 2, 3

    def __init__(self, profile: LinkProfile | None = None, port: int = 47700,
                 dest_host: str = "127.0.0.1", listen: bool = True):
        self.p = profile or LinkProfile()
        self.port = port
        self.dest_host = dest_host
        self.listen = listen
        self.up = True
        self._durable: list = []          # heap: (pri, n, pkt)
        self._count = itertools.count()
        self._slots: dict[str, bytes | None] = {"hb": None, "state": None}
        self._slot_order = ["hb", "state"]
        self._slot_streak = 0
        self.sent_bytes = 0
        self.sent_pkts = 0
        self.dropped_pkts = 0
        self.coalesced = 0                # slot values replaced before send
        self._window: list = []
        self._rev_q: list = []            # reverse-channel outbox (ground side)
        self._rev_last = 0.0

    # ------------------------------------------------------------ lifecycle
    async def start(self, on_pkt, on_rev=None):
        """on_pkt(data): receiver for forward packets (ground side).
        on_rev(data): receiver for reverse ACK packets (onboard side).
        The reverse destination is learned from the forward packets' source
        address, so split-host mode needs no extra configuration."""
        loop = asyncio.get_running_loop()
        self._rev_dest = (self.dest_host, self.port + 1)
        if self.listen and on_pkt is not None:
            def fwd(data, addr):
                self._rev_dest = (addr[0], self.port + 1)   # learn peer
                on_pkt(data)
            self._fwd_rx, _ = await loop.create_datagram_endpoint(
                lambda: _Rx(fwd), local_addr=("0.0.0.0", self.port))
        self._fwd_tx, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=(self.dest_host, self.port))
        if on_rev is not None:
            self._rev_rx, _ = await loop.create_datagram_endpoint(
                lambda: _Rx(lambda d, a: on_rev(d)),
                local_addr=("0.0.0.0", self.port + 1))
        self._rev_tx, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0))
        self._task = asyncio.create_task(self._pump())
        self._rev_task = asyncio.create_task(self._rev_pump())

    # ------------------------------------------------------------ tx api
    def set_slot(self, name: str, pkt: bytes):
        """Latest-value channel: replaces any unsent packet of this class."""
        if self._slots.get(name) is not None:
            self.coalesced += 1
        self._slots[name] = pkt

    def send_durable(self, pkt: bytes, priority: int):
        heapq.heappush(self._durable, (priority, next(self._count), pkt))

    def send_reverse(self, pkt: bytes):
        """Ground -> onboard ACK. Latest-per-report semantics kept simple:
        bounded queue, oldest dropped."""
        self._rev_q.append(pkt)
        if len(self._rev_q) > 8:
            self._rev_q.pop(0)

    @property
    def queued(self) -> int:
        return len(self._durable) + sum(1 for v in self._slots.values() if v)

    @property
    def durable_pending(self) -> int:
        return len(self._durable)

    def kbps_now(self) -> float:
        cut = time.time() - 2.0
        self._window = [w for w in self._window if w[0] > cut]
        return sum(b for _, b in self._window) * 8 / 2000.0

    # ------------------------------------------------------------ scheduler
    def _next_pkt(self):
        """Documented policy: control first; then slots, but at most two
        slot sends per durable send when durable work is pending."""
        if self._durable and self._durable[0][0] == self.PRI_CONTROL:
            return heapq.heappop(self._durable)[2], "durable"
        want_durable = self._durable and self._slot_streak >= 2
        if not want_durable:
            for name in self._slot_order:
                if self._slots.get(name):
                    pkt, self._slots[name] = self._slots[name], None
                    self._slot_streak += 1
                    return pkt, "slot"
        if self._durable:
            self._slot_streak = 0
            return heapq.heappop(self._durable)[2], "durable"
        return None, None

    async def _deliver(self, tx, pkt: bytes):
        p = self.p
        if p.rng.random() < p.loss:
            self.dropped_pkts += 1
            return
        if p.corrupt and p.rng.random() < p.corrupt:
            b = bytearray(pkt)
            b[p.rng.randrange(len(b))] ^= 0xFF
            pkt = bytes(b)
        delay = p.latency + (p.reorder and p.rng.random() < p.reorder) * p.rng.uniform(.2, .8)
        asyncio.get_running_loop().call_later(delay, tx.sendto, pkt)
        if p.dup and p.rng.random() < p.dup:
            asyncio.get_running_loop().call_later(delay + .05, tx.sendto, pkt)
        self.sent_bytes += len(pkt)
        self.sent_pkts += 1
        self._window.append((time.time(), len(pkt)))

    async def _pump(self):
        p = self.p
        if p.name == "iridium-sbd":
            while True:
                if not self.up:
                    await asyncio.sleep(0.05)
                    continue
                pkt, _ = self._next_pkt()
                if pkt is None:
                    await asyncio.sleep(0.05)
                    continue
                await asyncio.sleep(p.sbd_latency)      # one session per message
                if p.rng.random() < p.sbd_success:
                    self._fwd_tx.sendto(pkt)
                    self.sent_bytes += len(pkt)
                    self.sent_pkts += 1
                    self._window.append((time.time(), len(pkt)))
                else:
                    self.dropped_pkts += 1
            return
        bucket, last = 0.0, time.time()

        def cap():
            return max(p.bps * 1.0, 340 * 8 * 1.5)

        while True:
            now = time.time()
            bucket = min(cap(), bucket + (now - last) * p.bps)
            last = now
            if not self.up:
                await asyncio.sleep(0.05)
                continue
            pkt, _ = self._next_pkt()
            if pkt is None:
                await asyncio.sleep(0.02)
                continue
            cost = len(pkt) * 8
            while bucket < cost:
                await asyncio.sleep(max(0.01, (cost - bucket) / p.bps))
                now = time.time()
                bucket = min(cap(), bucket + (now - last) * p.bps)
                last = now
            bucket -= cost
            await self._deliver(self._fwd_tx, pkt)

    async def _rev_pump(self):
        """Reverse channel: <=1 message per interval, SBD MT sized."""
        p = self.p
        interval = p.sbd_latency if p.name == "iridium-sbd" else 1.5
        while True:
            if self.up and self._rev_q:
                pkt = self._rev_q.pop(0)
                dest = self._rev_dest
                if p.name == "iridium-sbd":
                    await asyncio.sleep(p.sbd_latency)
                    if p.rng.random() < p.sbd_success:
                        self._rev_tx.sendto(pkt, dest)
                else:
                    # reverse traffic: same loss/latency model, but not
                    # counted against the forward payload budget
                    if p.rng.random() >= p.loss:
                        asyncio.get_running_loop().call_later(
                            p.latency, self._rev_tx.sendto, pkt, dest)
                await asyncio.sleep(interval)
            else:
                await asyncio.sleep(0.1)
