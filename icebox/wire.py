"""Versioned wire messages for the constrained link.

Every judge-visible incident fact must cross the link as one of these
messages. Nothing in this module touches Ground or Onboard state; it only
converts between dataclass-ish dicts and bytes, with CRC-16 protection on
header+payload and hard validation limits so a corrupt or hostile header
cannot allocate unbounded state.

Message families
  STATE / HEARTBEAT      ephemeral, latest-value (defined in blackbox.py,
                         carry the same WIRE_VERSION byte)
  INCIDENT_NOTICE  'IN'  durable, tiny: the first thing ground may know
  REPORT_MANIFEST  'RM'  durable: authoritative chunk count + SHA-256
  REPORT_CHUNK     'SB'  durable payload chunks (blackbox.packetize)
  ACK              'AK'  reverse channel, fits the 270-byte SBD MT limit

Identifiers: (mission_id, boot_id, report_id, tier) scope every durable
object; a stale boot cannot contaminate a new session because ground drops
partial state when boot_id changes.
"""
from __future__ import annotations

import hashlib
import struct

from .blackbox import crc16

WIRE_VERSION = 1
MISSION_ID = 1

# hard validation limits (anti-allocation)
MAX_TOTAL_CHUNKS = 512
MAX_OPEN_REPORTS = 8
MAX_PREMANIFEST_CHUNKS = 64
SBD_MT_MAX = 270          # reverse-channel (mobile-terminated) limit
ACK_MAX_MISSING = 100     # keeps worst-case ACK <= 270 B

CAUSE_CODES = {0: "unknown trigger", 1: "impact", 2: "orientation loss",
               3: "joint overcurrent"}
KIND_CRASH, KIND_BACKFILL = 1, 2
KIND_NAMES = {KIND_CRASH: "crash", KIND_BACKFILL: "backfill"}

IN_STRUCT = struct.Struct("<2sBHIHBdH")            # magic ver mission boot report cause t crc
RM_STRUCT = struct.Struct("<2sBHIHBBHHfddI16sH")   # ..kind tier total nsamp rate t0 t1 plen sha16 crc
AK_HEAD = struct.Struct("<2sBHIHBB8sHBB")          # ..report tier flags digest8 highest complete nmiss


def cause_code(cause_text: str) -> int:
    t = (cause_text or "").lower()
    if "impact" in t:
        return 1
    if "orientation" in t or "tilt" in t:
        return 2
    if "joint" in t or "overcurrent" in t:
        return 3
    return 0


# ------------------------------------------------------------ incident notice

def pack_incident_notice(boot: int, report: int, cause: str, t: float) -> bytes:
    body = IN_STRUCT.pack(b"IN", WIRE_VERSION, MISSION_ID, boot & 0xFFFFFFFF,
                          report & 0xFFFF, cause_code(cause), t, 0)[:-2]
    return body + struct.pack("<H", crc16(body))


def unpack_incident_notice(pkt: bytes) -> dict | None:
    if len(pkt) != IN_STRUCT.size or pkt[:2] != b"IN":
        return None
    if crc16(pkt[:-2]) != struct.unpack("<H", pkt[-2:])[0]:
        return None
    m, ver, mission, boot, report, code, t, _ = IN_STRUCT.unpack(pkt)
    if ver != WIRE_VERSION:
        return None
    return {"type": "incident_notice", "boot": boot, "report": report,
            "cause_code": code, "cause": CAUSE_CODES.get(code, "unknown"),
            "t": t, "bytes": len(pkt)}


# ------------------------------------------------------------ report manifest

def report_sha(payload: bytes) -> bytes:
    return hashlib.sha256(payload).digest()


def pack_manifest(boot: int, report: int, kind: int, tier: int, total: int,
                  n_samples: int, rate: float, t0: float, t1: float,
                  payload: bytes) -> bytes:
    body = RM_STRUCT.pack(b"RM", WIRE_VERSION, MISSION_ID, boot & 0xFFFFFFFF,
                          report & 0xFFFF, kind, tier, total & 0xFFFF,
                          min(0xFFFF, n_samples), rate, t0, t1, len(payload),
                          report_sha(payload)[:16], 0)[:-2]
    return body + struct.pack("<H", crc16(body))


def unpack_manifest(pkt: bytes) -> dict | None:
    if len(pkt) != RM_STRUCT.size or pkt[:2] != b"RM":
        return None
    if crc16(pkt[:-2]) != struct.unpack("<H", pkt[-2:])[0]:
        return None
    (m, ver, mission, boot, report, kind, tier, total, nsamp, rate,
     t0, t1, plen, sha16, _) = RM_STRUCT.unpack(pkt)
    if ver != WIRE_VERSION:
        return None
    # header sanity: reject before any allocation happens
    if not (0 < total <= MAX_TOTAL_CHUNKS):
        return None
    if not (0 < plen <= MAX_TOTAL_CHUNKS * 328):
        return None
    if kind not in KIND_NAMES or tier not in (1, 2, 3):
        return None
    return {"type": "manifest", "boot": boot, "report": report, "kind": kind,
            "kind_name": KIND_NAMES[kind], "tier": tier, "total": total,
            "n_samples": nsamp, "rate": rate, "t0": t0, "t1": t1,
            "payload_len": plen, "sha16": sha16, "bytes": len(pkt)}


# ------------------------------------------------------------ reverse ACK

def pack_ack(boot: int, report: int, tier: int, sha16: bytes,
             highest_contig: int, missing: list[int], complete: bool) -> bytes:
    miss = sorted(set(missing))[:ACK_MAX_MISSING]
    body = AK_HEAD.pack(b"AK", WIRE_VERSION, MISSION_ID, boot & 0xFFFFFFFF,
                        report & 0xFFFF, tier, 0, sha16[:8],
                        highest_contig & 0xFFFF, 1 if complete else 0, len(miss))
    body += struct.pack(f"<{len(miss)}H", *miss)
    pkt = body + struct.pack("<H", crc16(body))
    assert len(pkt) <= SBD_MT_MAX, "ACK exceeds SBD MT limit"
    return pkt


def unpack_ack(pkt: bytes) -> dict | None:
    if len(pkt) < AK_HEAD.size + 2 or pkt[:2] != b"AK":
        return None
    if crc16(pkt[:-2]) != struct.unpack("<H", pkt[-2:])[0]:
        return None
    (m, ver, mission, boot, report, tier, _flags, digest8, highest,
     complete, nmiss) = AK_HEAD.unpack_from(pkt)
    if ver != WIRE_VERSION or nmiss > ACK_MAX_MISSING:
        return None
    if len(pkt) != AK_HEAD.size + nmiss * 2 + 2:
        return None
    missing = list(struct.unpack_from(f"<{nmiss}H", pkt, AK_HEAD.size))
    return {"type": "ack", "boot": boot, "report": report, "tier": tier,
            "digest8": digest8, "highest": highest,
            "complete": bool(complete), "missing": missing,
            "bytes": len(pkt)}


# ------------------------------------------------------------ chunk validation

def validate_chunk_header(report: int, tier: int, seq: int, total: int,
                          payload_len: int) -> bool:
    """Sanity gate applied by ground before any chunk is stored."""
    if not (0 < total <= MAX_TOTAL_CHUNKS):
        return False
    if not (0 <= seq < total):
        return False
    if not (0 < payload_len <= 328):
        return False
    if tier not in (1, 2, 3):
        return False
    return True
