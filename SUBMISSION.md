# Submission copy — paste-ready (hardened wording)

**Project name:** NeverDrop

**One-liner:** Mission control for robots beyond the cloud — a live
base-state twin, manifest-verified store-and-forward telemetry, and a
persistent black-box recorder over a constrained lab model of a 2 kbps
satellite link.

**Repo:** https://github.com/vnmoorthy/neverdrop
**Recorded end-to-end demo:** https://vnmoorthy.github.io/neverdrop/

**Description:**
Above 8,000 m the Everest robot's only uplink is ~2 kbps of Iridium.
NeverDrop supervises through that budget: 31-byte state frames at 7 Hz
drive a live 3D twin (real Feetech arm integrated — actual joint telemetry
end-to-end); blackouts coalesce status and record durably onboard, then
backfill with a transmitted manifest and SHA-256 so the UI can honestly say
VERIFIED · HASH OK; crashes persist to a SQLite outbox before transmission
and deliver as 340-byte SBD-compatible chunks with ACK-driven selective
retransmit on a 270-byte reverse channel, finite retry ending in an
explicit PARTIAL state, and restart-resume on both ends — proven by a
14-scenario deterministic reliability suite plus a separate-process test,
all in CI. Every screen element is truth-labeled: source (REAL MEASUREMENT
vs SYNTHETIC), link (LAB 2 KBPS MODEL — no satellite hardware, stated),
and incident facts arrive only as decoded link packets. Incident analysis
is a confidence-scored hypothesis with stated limitations, not a claimed
root cause.

**Pillars:** satellite communication (core), hardware resilience
(black box, restarts, bounded storage), onboard autonomy (edge-side
triggering, prioritization, persistence — no cloud anywhere).

**Honest status:** physical arm streams live joint state; the modem itself
is the next explicit integration (its message limits are already enforced
by the SBD profile); Jetson deployment documented, untested. Full ledger:
CLAIMS_AND_EVIDENCE.md.

**Tech:** Python/asyncio + aiohttp only; SQLite WAL outbox/inbox; CRC-16 +
SHA-256 integrity; deterministic seeded link models; vendored Three.js UI
(offline-capable, no font CDN); real SRTM terrain (display context).
