<div align="center">

![NeverDrop](assets/banner.svg)

# 📡 NeverDrop

### Mission control for robots beyond the cloud

**A live base-state twin · manifest-verified store-and-forward telemetry ·
a persistent black-box recorder — over a constrained lab model of a
2,000 bit/s satellite link. No cloud, no CDN, no satellite hardware —
and it says so on screen.**

[![ci](https://github.com/vnmoorthy/neverdrop/actions/workflows/ci.yml/badge.svg)](https://github.com/vnmoorthy/neverdrop/actions/workflows/ci.yml)
[![reliability](https://img.shields.io/badge/reliability_suite-14_scenarios-blue)](test_reliability.py)
[![claims](https://img.shields.io/badge/claims-evidence_ledger-blue)](CLAIMS_AND_EVIDENCE.md)
[![link model](https://img.shields.io/badge/link-lab_2kbps_%2B_SBD_model-orange)](PROTOCOL.md)
[![license](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

### **[▶ &nbsp;WATCH THE RECORDED END-TO-END DEMO](https://vnmoorthy.github.io/neverdrop/)**
*a captured session (sim source, lab 2 kbps model) replayed through the
actual mission-control UI, with chapter navigation*

[protocol](PROTOCOL.md) · [failure matrix](FAILURE_MATRIX.md) ·
[claims & evidence](CLAIMS_AND_EVIDENCE.md) · [deployment](DEPLOYMENT.md) ·
[demo runbook](DEMO_RUNBOOK.md)

![The demo in 12 seconds](assets/demo.svg)

*Built at the Himalaya Robotics Hack 2026 · San Francisco — aimed at the
[Robot Everest](https://www.roboteverest.com/) expedition's constraint:
above 8,000 m the only uplink is ~2 kbps of Iridium.*

</div>

---

## What it does (and what that claim means)

1. **Live base-state twin.** The robot's attitude quaternion, peak |a|,
   tilt, x/y dead-reckoned position and optional joint angles stream as
   **31-byte frames at 7 Hz** (43 B / 5 Hz with 6 joints) — 1,904 bps
   including 21-byte heartbeats, inside a 2,000 bps budget. This is *base
   state*, not full pose, and the numbers come from
   `python -m icebox.protocol_stats`, not prose.
2. **Verified store-and-forward.** Cut the link: current status coalesces
   (latest value wins — no stale replay), durable data records to bounded
   disk-backed storage. On restore, the blackout is declared in a
   **transmitted manifest** (chunk count, sample count, declared 12.5 Hz
   resolution, SHA-256) and the UI shows `RECEIVING x/y` →
   `BACKFILL VERIFIED · HASH OK`. Zero-loss language appears **only** with
   that proof.
3. **Black box with delivery guarantees.** A crash triggers onboard; the
   last 10 s persist to a SQLite outbox **before first transmission**, then
   ship as 340-byte SBD-compatible chunks with manifest + ACK-driven
   selective retransmit on a ≤270-byte reverse channel. Tier-1 preview
   first (PRELIMINARY analysis), tier-2 refines it. Retry policy is finite
   and ends in an explicit `PARTIAL_FAILED` — never a silent drop. Both
   ends survive restarts and complete exactly once.
4. **Truth boundary.** The onboard process holds no reference to ground.
   Every incident fact on the dashboard — id, cause, chunk counts,
   analysis — arrives as decoded link packets tagged `via:"link"`; local
   demo controls live in a panel labeled `LOCAL TEST HARNESS · NEVER
   TRANSMITTED`. A dedicated test proves ground can't learn incident
   details any other way.

## Verify everything yourself

```bash
python3 -m venv .venv && .venv/bin/pip install aiohttp
.venv/bin/python test_blackbox.py        # codec/trigger/framing: ALL PASS
.venv/bin/python test_reliability.py     # 14 failure scenarios: ALL PASS
.venv/bin/python test_split_roles.py     # separate processes: ALL PASS
.venv/bin/python -m icebox.protocol_stats  # the real sizes and budgets
```

Phone-mode end-to-end (synthetic Sensor Logger fixture — clearly labeled;
we did not fabricate a "real" trace):

```bash
.venv/bin/python -m icebox.server --source phone --port 8010 &
.venv/bin/python test_phone_e2e.py       # PHONE E2E: ALL PASS
```

## Run it

```bash
python -m icebox.server --role all --source sim              # demo
python -m icebox.server --role all --source arm              # real Feetech arm
python -m icebox.server --role ground --port 8000 --listen-port 47700
python -m icebox.server --role onboard --source sim --ground-host <ip> --ground-port 47700
python -m icebox.server --role all --source sim --link-profile iridium-sbd --sbd-latency 12 --seed 7
```

Open `http://localhost:8000`. The truth strip at the top declares the
source (SYNTHETIC vs REAL MEASUREMENT), the link profile (LAB 2 KBPS MODEL
or IRIDIUM SBD OPERATIONAL MODEL), `SATELLITE HARDWARE: NONE`, and live
report coverage/integrity. Black-box archives are inspectable files:

```bash
python -m icebox.inspect_report reports/report_00001.ndz
python -m icebox.replay_report  reports/report_00001.ndz
```

## Hardware status (evidence-based, see CLAIMS_AND_EVIDENCE.md)

- **Arm — REAL, partially exercised**: a physical 6-servo Feetech bus is
  integrated (auto-detected, 1 Mbaud, 6.3 ms read cycle measured, reads in
  a worker thread); live joint state streams end-to-end. The grab-incident
  flow on hardware awaits a human yank.
- **Phone — pipeline proven on a synthetic fixture** (time-alignment,
  staleness gates, quaternion validation tested); a live phone run is
  pending.
- **Jetson / satellite modem — NOT integrated.** `SatLink` is the modem
  boundary; the SBD profile models message-session behavior with the real
  340/270-byte limits and a configurable (default 8 s, assumption)
  session latency.
- Battery/temperature are **NOT MEASURED** on phone/arm sources and the UI
  says so; simulated values appear only with the sim source.

## Incident analysis, honestly

The ground computes a **mechanism hypothesis** from received IMU/joint
data: event type, peak g, free-fall estimate, dominant axis, final
attitude — with a confidence score, coverage, an explicit limitations list
(no actuator state, no contact sensing, no terrain/wind data) and
alternative hypotheses. Tier-1 output is labeled PRELIMINARY; tier-2
REFINED. It is not called a root cause, because IMU telemetry alone cannot
establish one.

## The 3D scene

Terrain is real SRTM elevation of the Everest massif (26 km patch, relief
1:1, geo-verified to within one grid cell of the summit); the GPS readout
derives from a Base Camp anchor constant plus the transmitted position —
display context, not navigation. The robot render is stylized; joint
angles are the transmitted values.

Architecture, wire format, priority policy and integrity model:
[PROTOCOL.md](PROTOCOL.md). Every failure mode and its test:
[FAILURE_MATRIX.md](FAILURE_MATRIX.md).
