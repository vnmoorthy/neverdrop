"""Durable outbox (onboard) and inbox (ground), stdlib SQLite in WAL mode.

Rules implemented here:
  - a durable report is persisted BEFORE its first transmission
  - it remains persisted until verified acknowledgment (hash-checked ACK)
  - after an onboard restart, pending reports reload with their original
    (boot_id, report_id) identity and resume from missing chunks
  - retry policy is finite and explicit: exhaustion => status PARTIAL_FAILED,
    never a silent drop
  - the ground inbox persists chunks + completion so a ground restart is
    idempotent (retransmits deduplicate; a report completes exactly once)

Storage bounds: at most MAX_REPORTS rows are retained; older delivered
reports are archived to .ndz files and pruned from the DB.
"""
from __future__ import annotations

import json
import os
import sqlite3
import struct
import time

from . import wire
from . import blackbox as bb

MAX_REPORTS = 50
DEFAULT_MAX_ROUNDS = 20

ST_PENDING, ST_SENDING, ST_DELIVERED, ST_PARTIAL_FAILED = (
    "PENDING", "SENDING", "DELIVERED", "PARTIAL_FAILED")


def _db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


class Outbox:
    def __init__(self, path: str):
        self.con = _db(path)
        self.con.execute("""CREATE TABLE IF NOT EXISTS reports(
            boot INTEGER, report INTEGER, tier INTEGER, kind INTEGER,
            total INTEGER, n_samples INTEGER, rate REAL, t0 REAL, t1 REAL,
            payload BLOB, sha BLOB, acked BLOB, status TEXT, rounds INTEGER,
            created REAL, PRIMARY KEY(boot, report, tier))""")
        self.con.commit()

    # ------------------------------------------------------------- write path
    def add(self, boot: int, report: int, tier: int, kind: int,
            payload: bytes, n_samples: int, rate: float,
            t0: float, t1: float) -> int:
        total = (len(payload) + bb.PAYLOAD_MAX - 1) // bb.PAYLOAD_MAX
        acked = bytes((total + 7) // 8)
        self.con.execute(
            "INSERT OR REPLACE INTO reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (boot, report, tier, kind, total, n_samples, rate, t0, t1,
             payload, wire.report_sha(payload), acked, ST_PENDING, 0, time.time()))
        self.con.commit()          # persisted BEFORE first transmission
        self._prune()
        return total

    def mark_sending(self, boot, report, tier):
        self.con.execute("UPDATE reports SET status=? WHERE boot=? AND report=? AND tier=?",
                         (ST_SENDING, boot, report, tier))
        self.con.commit()

    def apply_ack(self, ack: dict) -> str | None:
        """Returns new status if the ack changed anything."""
        row = self.con.execute(
            "SELECT total, sha, acked, status FROM reports WHERE boot=? AND report=? AND tier=?",
            (ack["boot"], ack["report"], ack["tier"])).fetchone()
        if not row:
            return None
        total, sha, acked, status = row
        if ack["digest8"] != sha[:8]:
            return None                       # conflicting manifest identity
        if ack["complete"]:
            self.con.execute(
                "UPDATE reports SET status=?, acked=? WHERE boot=? AND report=? AND tier=?",
                (ST_DELIVERED, bytes([0xFF]) * len(acked),
                 ack["boot"], ack["report"], ack["tier"]))
            self.con.commit()
            return ST_DELIVERED
        a = bytearray(acked)
        for seq in range(min(ack["highest"] + 1, total)):
            if seq not in ack["missing"]:
                a[seq // 8] |= 1 << (seq % 8)
        for seq in range(total):
            if seq in ack["missing"]:
                a[seq // 8] &= ~(1 << (seq % 8)) & 0xFF
        self.con.execute(
            "UPDATE reports SET acked=? WHERE boot=? AND report=? AND tier=?",
            (bytes(a), ack["boot"], ack["report"], ack["tier"]))
        self.con.commit()
        return None

    def missing_chunks(self, boot, report, tier) -> list[int]:
        row = self.con.execute(
            "SELECT total, acked FROM reports WHERE boot=? AND report=? AND tier=?",
            (boot, report, tier)).fetchone()
        if not row:
            return []
        total, acked = row
        return [s for s in range(total) if not (acked[s // 8] >> (s % 8)) & 1]

    def bump_round(self, boot, report, tier, max_rounds: int | None = None) -> str:
        """Count a retry round; exhaust => explicit PARTIAL_FAILED."""
        if max_rounds is None:
            max_rounds = DEFAULT_MAX_ROUNDS      # late-bound: tests may override
        row = self.con.execute(
            "SELECT rounds, status FROM reports WHERE boot=? AND report=? AND tier=?",
            (boot, report, tier)).fetchone()
        if not row:
            return ST_PARTIAL_FAILED
        rounds, status = row[0] + 1, row[1]
        new = ST_PARTIAL_FAILED if rounds >= max_rounds else status
        self.con.execute(
            "UPDATE reports SET rounds=?, status=? WHERE boot=? AND report=? AND tier=?",
            (rounds, new, boot, report, tier))
        self.con.commit()
        return new

    # ------------------------------------------------------------- read path
    def pending(self) -> list[dict]:
        rows = self.con.execute(
            "SELECT boot, report, tier, kind, total, n_samples, rate, t0, t1, "
            "payload, sha, status, rounds FROM reports "
            "WHERE status IN (?,?) ORDER BY report, tier",
            (ST_PENDING, ST_SENDING)).fetchall()
        return [dict(zip(("boot", "report", "tier", "kind", "total", "n_samples",
                          "rate", "t0", "t1", "payload", "sha", "status",
                          "rounds"), r)) for r in rows]

    def get(self, boot, report, tier) -> dict | None:
        r = self.con.execute(
            "SELECT boot, report, tier, kind, total, n_samples, rate, t0, t1, "
            "payload, sha, status, rounds FROM reports "
            "WHERE boot=? AND report=? AND tier=?", (boot, report, tier)).fetchone()
        if not r:
            return None
        return dict(zip(("boot", "report", "tier", "kind", "total", "n_samples",
                         "rate", "t0", "t1", "payload", "sha", "status",
                         "rounds"), r))

    def max_report_id(self) -> int:
        r = self.con.execute("SELECT MAX(report) FROM reports").fetchone()
        return r[0] or 0

    def status_counts(self) -> dict:
        out = {}
        for st, n in self.con.execute(
                "SELECT status, COUNT(*) FROM reports GROUP BY status"):
            out[st] = n
        return out

    def _prune(self):
        self.con.execute(
            "DELETE FROM reports WHERE rowid NOT IN "
            "(SELECT rowid FROM reports ORDER BY created DESC LIMIT ?)",
            (MAX_REPORTS,))
        self.con.commit()


class Inbox:
    """Ground persistence: received chunks + completion, restart-idempotent."""

    def __init__(self, path: str):
        self.con = _db(path)
        self.con.execute("""CREATE TABLE IF NOT EXISTS manifests(
            boot INTEGER, report INTEGER, tier INTEGER, kind INTEGER,
            total INTEGER, n_samples INTEGER, rate REAL, t0 REAL, t1 REAL,
            payload_len INTEGER, sha16 BLOB, complete INTEGER, verified INTEGER,
            dups INTEGER DEFAULT 0, corrupt INTEGER DEFAULT 0,
            PRIMARY KEY(boot, report, tier))""")
        self.con.execute("""CREATE TABLE IF NOT EXISTS chunks(
            boot INTEGER, report INTEGER, tier INTEGER, seq INTEGER, data BLOB,
            PRIMARY KEY(boot, report, tier, seq))""")
        self.con.commit()

    def put_manifest(self, m: dict) -> bool:
        """Rejects a conflicting manifest for the same identity."""
        row = self.con.execute(
            "SELECT total, sha16 FROM manifests WHERE boot=? AND report=? AND tier=?",
            (m["boot"], m["report"], m["tier"])).fetchone()
        if row and (row[0] != m["total"] or bytes(row[1]) != m["sha16"]):
            return False
        if not row:
            self.con.execute(
                "INSERT INTO manifests VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,0)",
                (m["boot"], m["report"], m["tier"], m["kind"], m["total"],
                 m["n_samples"], m["rate"], m["t0"], m["t1"],
                 m["payload_len"], m["sha16"]))
            self.con.commit()
        return True

    def manifest(self, boot, report, tier) -> dict | None:
        r = self.con.execute(
            "SELECT boot, report, tier, kind, total, n_samples, rate, t0, t1, "
            "payload_len, sha16, complete, verified, dups, corrupt "
            "FROM manifests WHERE boot=? AND report=? AND tier=?",
            (boot, report, tier)).fetchone()
        if not r:
            return None
        return dict(zip(("boot", "report", "tier", "kind", "total", "n_samples",
                         "rate", "t0", "t1", "payload_len", "sha16", "complete",
                         "verified", "dups", "corrupt"), r))

    def put_chunk(self, boot, report, tier, seq, data: bytes) -> str:
        """Returns 'new' | 'dup'."""
        cur = self.con.execute(
            "INSERT OR IGNORE INTO chunks VALUES (?,?,?,?,?)",
            (boot, report, tier, seq, data))
        self.con.commit()
        if cur.rowcount == 0:
            self.con.execute(
                "UPDATE manifests SET dups=dups+1 WHERE boot=? AND report=? AND tier=?",
                (boot, report, tier))
            self.con.commit()
            return "dup"
        return "new"

    def count_corrupt(self, boot, report, tier):
        self.con.execute(
            "UPDATE manifests SET corrupt=corrupt+1 WHERE boot=? AND report=? AND tier=?",
            (boot, report, tier))
        self.con.commit()

    def received_seqs(self, boot, report, tier) -> set[int]:
        return {r[0] for r in self.con.execute(
            "SELECT seq FROM chunks WHERE boot=? AND report=? AND tier=?",
            (boot, report, tier))}

    def assemble(self, boot, report, tier) -> bytes | None:
        """Full-coverage + hash-verified payload, else None."""
        m = self.manifest(boot, report, tier)
        if not m:
            return None
        seqs = self.received_seqs(boot, report, tier)
        if len(seqs) < m["total"]:
            return None
        rows = self.con.execute(
            "SELECT seq, data FROM chunks WHERE boot=? AND report=? AND tier=? "
            "ORDER BY seq", (boot, report, tier)).fetchall()
        payload = b"".join(r[1] for r in rows)
        if wire.report_sha(payload)[:16] != bytes(m["sha16"]):
            return None
        self.con.execute(
            "UPDATE manifests SET complete=1, verified=1 WHERE boot=? AND report=? AND tier=?",
            (boot, report, tier))
        self.con.commit()
        return payload

    def already_verified(self, boot, report, tier) -> bool:
        m = self.manifest(boot, report, tier)
        return bool(m and m["verified"])

    def drop_boot_except(self, boot: int):
        """A new boot invalidates partial state from older boots."""
        self.con.execute("DELETE FROM chunks WHERE boot != ? AND boot != 0", (boot,))
        self.con.execute(
            "DELETE FROM manifests WHERE boot != ? AND boot != 0 AND verified=0", (boot,))
        self.con.commit()


# ---------------------------------------------------------------- .ndz files
#
# Versioned, self-delimiting black-box archive:
#   magic 'NDBB' | ver u8 | mission u16 | boot u32 | report u16 | kind u8 |
#   status u8 | created f64 | n_records u16 |
#   n x [tier u8 | meta_len u32 | meta(json) | data_len u32 | data | crc32 u32]
#   | report_sha256 32B
NDZ_MAGIC = b"NDBB"
NDZ_VER = 1


def write_ndz(path: str, mission: int, boot: int, report: int, kind: int,
              status: str, records: list[tuple[int, dict, bytes]]):
    """records: [(tier, meta, payload)]. Atomic: tmp + fsync + rename."""
    import zlib
    body = struct.pack("<4sBHIHBB d H", NDZ_MAGIC, NDZ_VER, mission, boot,
                       report, kind, 1 if status == ST_DELIVERED else 0,
                       time.time(), len(records))
    hasher_input = b""
    for tier, meta, data in records:
        mj = json.dumps(meta, separators=(",", ":")).encode()
        body += struct.pack("<BI", tier, len(mj)) + mj
        body += struct.pack("<I", len(data)) + data
        body += struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF)
        hasher_input += data
    body += wire.report_sha(hasher_input)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_ndz(path: str) -> dict:
    import zlib
    raw = open(path, "rb").read()
    head = struct.Struct("<4sBHIHBB d H")
    magic, ver, mission, boot, report, kind, delivered, created, n = \
        head.unpack_from(raw)
    assert magic == NDZ_MAGIC, "not an NDBB file"
    assert ver == NDZ_VER, f"unsupported version {ver}"
    off = head.size
    records, hin = [], b""
    for _ in range(n):
        tier, mlen = struct.unpack_from("<BI", raw, off); off += 5
        meta = json.loads(raw[off:off+mlen]); off += mlen
        (dlen,) = struct.unpack_from("<I", raw, off); off += 4
        data = raw[off:off+dlen]; off += dlen
        (crc,) = struct.unpack_from("<I", raw, off); off += 4
        records.append({"tier": tier, "meta": meta, "data": data,
                        "crc_ok": zlib.crc32(data) & 0xFFFFFFFF == crc})
        hin += data
    sha_ok = raw[off:off+32] == wire.report_sha(hin)
    return {"mission": mission, "boot": boot, "report": report, "kind": kind,
            "delivered": bool(delivered), "created": created,
            "records": records, "sha_ok": sha_ok}
