# Failure Matrix

Every row is exercised by `python test_reliability.py` (deterministic,
seeded) unless noted. Last full run: RELIABILITY: ALL PASS (14/14).

| # | Failure | Expected behavior | Test | Result | Limitation |
|---|---|---|---|---|---|
| 1 | 30 s-class link blackout | live state stops; recording continues to bounded buffers; fresh status returns promptly on restore; backfill later VERIFIED against manifest+hash | t01 | PASS | backfill at declared 12.5 Hz, not full rate |
| 2 | 10% independent packet loss | both tiers verify via selective retransmit; exactly one analysis per tier | t02 | PASS | wall-clock grows with loss |
| 3 | Heavy loss + finite retry (3 rounds) | explicit PARTIAL_FAILED; no false completion in either direction | t03 | PASS | onboard may be pessimistic if the complete-ACK is lost; a late ACK reconciles |
| 4 | Reordering (60%) | out-of-order chunks assemble and verify | t04 | PASS | — |
| 5 | Duplication (50%) | idempotent; duplicates counted and displayed | t05 | PASS | — |
| 6 | Payload corruption (12%) | CRC catches; counted; chunk retransmitted; hash verifies | t06 | PASS | — |
| 7 | Hostile/corrupt headers | rejected pre-allocation; pre-manifest cache bounded (8×64) | t07 | PASS | — |
| 8 | Missing final chunk | stays PARTIAL x/y; never claims complete or zero-loss | t08 | PASS | — |
| 9 | Onboard restart mid-transfer | SQLite outbox reloads with original (boot, report) identity; resumes missing chunks; no duplicate incident; hash verifies | t09 | PASS | — |
| 10 | Ground restart mid-transfer | persisted inbox + boot scope reload; retransmits dedupe; completes exactly once | t10 | PASS | — |
| 11 | Long blackout status | heartbeat/state coalesce (latest-value); no stale status replayed as current | t11 | PASS | — |
| 12 | Truth-path | ground learns no incident fact except from decoded link packets; notice first | t12 | PASS | applies to `--role all` too (harness events are fact-free) |
| 13 | Budget | sizes derived from structs; measured lab throughput ≤ configured budget (+bucket burst) | t13 | PASS | 95.2% steady-state demand is intentional; slots absorb it |
| 14 | Phone sensor timing | cross-sensor interpolation; stale values rejected; duplicates rejected; quats normalized | t14 | PASS | synthetic fixture; no real trace in repo |
| 15 | Split processes | onboard and ground as separate OS processes over UDP; incident via link; both tiers verified | `test_split_roles.py` | PASS | same-host in CI; LAN untested here |
| 16 | Real-arm false trigger (observed live) | near-zero current noise floor must not z-score-trigger on 1 LSB | fixed: 0.35 A absolute floor; `test_blackbox.py` arm test still passes | PASS | velocity trigger (>2 rad/s) covers torque-off arms; physical yank not yet human-tested |
