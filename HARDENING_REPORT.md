# Hardening Report

Scope: turn NeverDrop from a polished constrained-link demonstration into a
truthfully labeled, failure-resilient, judge-verifiable prototype, per the
engineering directive. Baseline `main @ 947ed61`, clean tree.

## Baseline vs final test status

| Suite | Baseline | Final |
|---|---|---|
| `test_blackbox.py` (8 tests) | ALL PASS | ALL PASS |
| `test_phone_e2e.py` | ALL PASS (weak assertions) | ALL PASS (link-provenance, manifest coverage x/x, hash-verified backfill, blackout case, single-trace generation, non-empty heartbeat set) |
| `test_reliability.py` | did not exist | **RELIABILITY: ALL PASS (14/14)** |
| `test_split_roles.py` | did not exist | **SPLIT ROLES: ALL PASS** (two OS processes) |
| `python -m icebox.protocol_stats` | did not exist | canonical struct-derived numbers |

## Architecture: before → after

Before: one process; `Onboard` held a `Ground` reference and pushed
incident facts through an in-process `op_event` bypass; heartbeats and
state queued FIFO (blackouts accumulated stale status); crash bursts were
fire-and-forget (any lost packet = permanent silent hole); "0 LOST" was
displayed upon receiving one compressed segment; reports written as raw
concatenated packets; single hard-coded link model described as "real".

After: `Onboard` references only source/ring/outbox/link. New versioned
wire messages (INCIDENT_NOTICE, REPORT_MANIFEST, REPORT_CHUNK, ACK) carry
every judge-visible fact; ws events are provenance-tagged `via:
link|harness` and harness events are fact-free. Ephemeral traffic uses
latest-value slots (coalesced, never replayed stale); durable traffic
persists to SQLite WAL **before first transmission**, is manifest-declared,
ACK-verified over a ≤270 B reverse channel, selectively retransmitted, and
ends in DELIVERED or an explicit PARTIAL_FAILED. Both ends persist and
restart-resume; reports complete exactly once. Black boxes are versioned
`.ndz` archives (atomic write, per-record CRC32 + report SHA-256) with
`inspect_report` / `replay_report`. Link profiles: `lab-2kbps` (loss/
latency/dup/reorder/corrupt, seeded) and `iridium-sbd` (message sessions,
340/270 limits, configurable latency). Roles split: `--role
all|onboard|ground` with identical serialization; health endpoints.

## Files changed / added

- `icebox/wire.py` (new): versioned messages + validation limits
- `icebox/outbox.py` (new): Outbox/Inbox (SQLite WAL) + `.ndz` format
- `icebox/protocol_stats.py`, `icebox/inspect_report.py`,
  `icebox/replay_report.py` (new)
- `icebox/linksim.py`: profiles, slots, priority scheduler w/ starvation
  guard, reverse channel, peer learning
- `icebox/server.py`: role split, truth boundary, retry loop, gap-buf
  bounds, `.ndz` archiving, health
- `icebox/blackbox.py`: wire version on ST/HB, NOT-MEASURED sentinels,
  honest structured incident analysis, joint-velocity trigger, 0.35 A
  current floor
- `icebox/telemetry.py`: phone per-sensor alignment/staleness/validation/
  calibration/health; arm reads in a worker thread; no fabricated
  battery/temp
- `web/index.html`: truth strip, manifest-driven wall (dups/corrupt),
  VERIFIED·HASH OK states, INCIDENT ANALYSIS card w/ confidence +
  limitations, LOCAL TEST HARNESS labeling, Google Fonts removed
- `docs/index.html`: RECORDED END-TO-END SESSION labeling, pause/restart/
  chapters, fonts removed, analysis rail no longer hidden on small screens
- `assets/demo.svg`: zero-loss and "real link" language corrected
- tests: `test_reliability.py`, `test_split_roles.py` (new);
  `test_phone_e2e.py` strengthened
- docs: `PROTOCOL.md`, `FAILURE_MATRIX.md`, `DEPLOYMENT.md`,
  `CLAIMS_AND_EVIDENCE.md`, `JUDGE_HARDENING_PLAN.md`, this report;
  `README.md`/`SUBMISSION.md`/`DEMO_RUNBOOK.md` rewritten from measured
  values
- `.github/workflows/ci.yml`: all suites, py3.10+3.12, cleanup traps,
  reliability summary artifact

## Claims changed (representative)

| Was | Now |
|---|---|
| "26-byte pose frames, 8 Hz" | 31 B (43 B w/ joints), 7 Hz / 5 Hz / 2 Hz throttled — from `protocol_stats` |
| "live digital twin (full pose)" | "live base-state twin (attitude + x/y + joints)" |
| "BACKFILLED · GAP CLOSED · 0 LOST" | "RECEIVING x/y" → "BACKFILL VERIFIED · HASH OK" (manifest + SHA-256 only) |
| "ROOT CAUSE" | "INCIDENT ANALYSIS · GROUND-COMPUTED INFERENCE" + confidence, limitations, alternatives, PRELIMINARY/REFINED |
| "real rate-limited satellite-class link" | "LAB 2 KBPS MODEL" / "IRIDIUM SBD OPERATIONAL MODEL"; truth strip: SATELLITE HARDWARE: NONE |
| "WATCH THE LIVE DEMO" | "WATCH THE RECORDED END-TO-END DEMO" |
| "real hardware ready" | "physical Feetech arm streaming live joint state; incident capture pending physical test" |
| simulated battery/temp shown as telemetry | NOT MEASURED sentinels on phone/arm |

## Bugs found by the new tests (and fixed)

1. Pre-manifest chunk cache unbounded across hostile report ids (t07).
2. Restarted Ground lost its boot scope → resumed chunks orphaned (t10).
3. `bump_round` default-arg early binding neutralized retry-policy caps.
4. Real-arm false trigger: near-zero current noise floor made 1-LSB blips
   z-score as incidents (observed on the physical arm; 0.35 A floor).
5. `null.toFixed` crash on honest NOT-MEASURED battery in the dashboard.

## Verified reliability guarantees (commands & outcomes)

`python test_reliability.py` → `RELIABILITY: ALL PASS`, covering: blackout
(bounded memory, prompt status recovery, verified backfill at declared
12.5 Hz) · 10% loss (selective retry, one analysis per tier) · finite
retry → explicit PARTIAL_FAILED with no false completion either direction ·
reorder · duplication (counted, idempotent) · payload corruption
(CRC-caught, retransmitted, hash-verified) · hostile headers (rejected,
bounded) · missing final chunk (stays partial) · onboard restart
(outbox resume, original identity, no duplicate incident) · ground restart
(exactly-once completion) · status coalescing · truth path (facts only via
decoded link packets, notice first) · budget (measured 1,945 ≤ 2,000 bps;
sizes from structs) · phone time alignment. Plus
`test_split_roles.py` → `SPLIT ROLES: ALL PASS`.

## Remaining hardware / risks (stated plainly)

1. **No satellite modem** — `SatLink` is the integration boundary; SBD
   limits are modeled, session latency is an assumption (default 8 s,
   configurable).
2. **Arm incident flow not yet human-tested** — live joint streaming from
   the physical arm is verified; the yank-trigger threshold may need one
   tuning pass against real motion. Torque state of the arm unknown.
3. **No real phone trace in the repo** — pipeline proven on a labeled
   synthetic fixture only; Jetson deployment documented but unexecuted.
4. Timing-sensitive CI: the reliability suite uses real sleeps at
   accelerated link rates; slower runners could flake (seeds fixed;
   windows padded).
5. In `--role all`, one Python process hosts both ends; the boundary is
   enforced by object reachability and the truth-path test, not an OS
   boundary (use split roles for the strongest claim).

## Demo commands

Live (one command): `python -m icebox.server --role all --source arm`
Split: see DEPLOYMENT.md. Recorded: https://vnmoorthy.github.io/neverdrop/
Runbook: DEMO_RUNBOOK.md (two-minute story with truth labels first).
