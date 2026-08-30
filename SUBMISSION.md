# Submission copy — paste-ready

**Project name:** NeverDrop

**One-liner:** Mission control for robots beyond the cloud — a live 3D
digital twin, loss-proof store-and-forward telemetry, and a flight-recorder
black box, all through a 2,000 bit/s satellite link.

**Repo:** https://github.com/vnmoorthy/neverdrop

**Description (short):**
Above 8,000 m the Everest robot's only connection is ~2 kbps of Iridium —
1,000× too slow for video. NeverDrop gives base camp eyes anyway: the
robot's pose streams as 26-byte frames into a live 3D twin; when the link
blacks out, the robot buffers onboard and backfills the gap on restore
(zero samples lost, demonstrated live); and when it crashes, an onboard
trigger bursts the last 10 seconds home as real 340-byte Iridium SBD
packets — a scrubbable 3D crash reconstruction with an automatically
computed root cause, ~7 s after impact. Every byte on the dashboard crossed
a genuinely rate-limited 2 kbps UDP link with CRC-16 framing; the honesty
ledger in the README states exactly what is simulated (the satellite is a
loopback socket; in phone/arm mode the motion is real). CI-tested: unit
suite + a full phone-mode end-to-end run pass on every push.

**Pillars addressed:** Satellite communication (core), hardware resilience
(black box + blackout survival), onboard autonomy (the robot decides what
its bandwidth is worth — all edge, no cloud).

**Why it matters beyond Everest:** video physically cannot supervise robot
fleets in mines, oceans, disaster zones, or orbit; state-streaming can.
NeverDrop is the observability layer of the physical world — per robot,
per month — with aviation's black-box mandate as the regulatory tailwind.

**Expedition deliverable:** dependency-light Python (aiohttp only); the
onboard half runs on the Jetson Thor by changing one loopback address. It
can ship with the team on Sept 5.

**Tech:** Python/asyncio, token-bucket UDP link sim at real Iridium budgets,
delta+zlib codec, 340 B SBD framing with CRC-16 and out-of-order
reassembly, Three.js mission control (vendored, offline-safe), Sensor
Logger phone IMU ingestion, Feetech serial adapter for real manipulators.
