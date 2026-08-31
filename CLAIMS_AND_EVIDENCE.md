# Claims & Evidence Ledger

Every public claim, its status, how a judge can verify it, and the wording
we actually use. Statuses: **PROVEN** (machine-checked here), **REAL**
(physical hardware exercised), **MODELED** (deterministic lab model),
**SIMULATED**, **PENDING** (not yet demonstrated).

| Claim | Status | Evidence | Limitation | Judge-safe wording |
|---|---|---|---|---|
| Live base-state streaming at low bandwidth | PROVEN | `python -m icebox.protocol_stats` → 31 B frame (43 B w/ 6 joints), 7 Hz (5 Hz arm); `test_reliability.py` t13 measured 1,945 bps ≤ 2,000 | attitude + x/y position (+ joints), NOT full pose; position is dead-reckoned | "low-bandwidth base-state twin" |
| The link is constrained like Iridium | MODELED | `icebox/linksim.py` (token-bucket lab model; SBD session model); UI truth strip: SATELLITE HARDWARE: NONE | no modem; SBD latency default 8 s is a config assumption | "SBD-compatible framing over a constrained lab link model" |
| Incident facts cross the link | PROVEN | `test_reliability.py` t12 truth-path; ws events carry `via:"link"` | in `--role all`, harness panel shows fact-free local activity only | "every incident fact you see was decoded from link packets" |
| Blackout backfill, nothing silently lost | PROVEN | t01 + `test_phone_e2e.py`: manifest coverage + SHA-256; UI says VERIFIED x/x · HASH OK | declared 12.5 Hz backfill resolution, not full rate; ring bound 60 s / gap bound 9,000 samples | "backfill verified against a transmitted manifest and hash" |
| Survives packet loss with selective retry | PROVEN | t02 (10% loss), t06 (corruption); finite policy t03 ends in explicit PARTIAL_FAILED | reverse ACK channel is part of the model | "verified reliable delivery with selective retransmit" |
| Durable reports survive restart | PROVEN | t09 (onboard), t10 (ground): SQLite WAL outbox/inbox, persisted before first tx | — | "persisted before transmission; restart-resumable" |
| Incident analysis | PROVEN (as inference) | `python -m icebox.replay_report reports/…` — structured hypothesis + confidence + limitations | IMU-only: cannot see actuator faults, terrain, wind; tier-1 labeled PRELIMINARY | "ground-computed incident analysis (mechanism hypothesis), confidence-scored" |
| Real robot arm integration | REAL | Physical 6-servo Feetech bus (IDs 1-6 @ 1 Mbaud); **commanded scan routine verified end-to-end**: work-loop goals written to the servos, resulting joint motion (base −9°, wrists +2.4°/+6° over 6 s, within the ±6° safety clamp) observed in the TRANSMITTED state frames at ground | grab-incident flow still needs a human grab; battery/temp NOT MEASURED; motion amplitude deliberately small | "a real arm working a commanded routine, supervised over the constrained link" |
| Real phone IMU integration | MODELED + PENDING | `test_phone_e2e.py` uses a synthetic Sensor Logger trace (labeled synthetic); alignment logic proven in t14 | no real phone recording exists in the repo — we did not fabricate one | "phone pipeline proven against a synthetic fixture; live phone pending" |
| Battery / temperature telemetry | SIMULATED (sim) / ABSENT (phone, arm) | heartbeat sentinels; UI shows NOT MEASURED | — | "not measured on phone/arm; simulated values appear only with the sim source" |
| Jetson / expedition deployment | PENDING | none — Python runs on ARM but nothing was executed on a Jetson | untested hardware | "dependency-light Python; Jetson deployment documented but untested" |
| Everest terrain / GPS readout | REAL DATA, display context | `bake_terrain.py` (SRTM tiles, geo-check within one cell of the summit); GPS = base-camp anchor + transmitted x/y | not navigation; dead-reckoned position on a display anchor | "real SRTM terrain; GPS readout derives from an anchor constant plus transmitted position" |
| CI | PROVEN | `.github/workflows/ci.yml`: unit + reliability + phone E2E + split-process on every push | macOS-only serial testing not in CI | "all suites run from a fresh clone on every push" |
