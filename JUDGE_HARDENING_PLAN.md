# Judge Hardening Plan

Baseline: main @ 947ed61 · clean tree · `test_blackbox.py` ALL PASS (8) ·
`test_phone_e2e.py` ALL PASS.

## 1. Current architecture (as inspected)

```
Onboard (in-process)                    Ground (same process)
  source.stream() -> ring buffer          SatLink.on_pkt -> unpack ST/HB/SB
  CrashTrigger -> _incident()             Reassembler -> segment -> analyze
  build_report -> SBD packets             Ground.broadcast -> ws JSON -> UI
  _stream_loop -> ST frames               history replay on ws connect
  _heartbeat_loop -> HB (priority)
  SatLink (token bucket, UDP loopback, one direction, FIFO hi/lo queues)
  *** Onboard holds a Ground reference and calls ground.op_event() ***
```

## 2. Current factual claims vs evidence

| Claim (README/UI) | Actual | Verdict |
|---|---|---|
| "26-byte pose frames, 8 Hz" | 30 B (42 B w/ 6 joints), 7 Hz (5 Hz arm, 2 Hz throttled) | **wrong numbers** |
| "live digital twin / full pose" | base attitude + x/y position (+ joints in arm mode) | overclaim |
| "0 LOST", "GAP CLOSED" | inferred from receiving ONE compressed segment; no manifest, no hash | **unproven claim** |
| "root cause" | heuristic IMU inference, no confidence/limitations | overclaim |
| "real rate-limited satellite-class link" | deterministic token-bucket UDP loopback | must be "lab link model" |
| "every byte crossed the link" | incident banner ✓hb-driven, but incident id/cause/packet counts arrive via in-process `op_event` | **bypass exists** |
| "WATCH THE LIVE DEMO" | recorded session replay | relabel |
| "real hardware ready" | Feetech adapter written, never run on hardware | relabel |
| delivery reliability | no ACK, no retry, no persistence-before-send, loss ⇒ silent permanent hole | **name not earned** |
| battery/temp | simulated in sim & phone modes, labeled as telemetry | label as simulated |

## 3. Implementation plan (in the mandated priority order)

1. **Truth**: `icebox/protocol_stats.py` (sizes/rates derived from structs);
   correct README/SUBMISSION/RUNBOOK/UI wording; CLAIMS_AND_EVIDENCE.md.
2. **Manifest coverage**: new versioned wire messages (`icebox/wire.py`):
   INCIDENT_NOTICE, REPORT_MANIFEST, ACK; ground displays x/y chunks from the
   manifest and only shows VERIFIED on full coverage + SHA-256 match.
3. **Bypass removal**: Onboard loses its Ground reference; incident facts reach
   ground only as decoded link packets tagged `via:"link"`; local harness
   telemetry tagged `via:"harness"` and shown in a separate labeled panel.
4. **Selective retry**: reverse UDP channel (≤270 B ACKs, periodic); onboard
   resends missing chunks; finite retry policy ends in explicit PARTIAL/FAILED.
5. **Reliability tests**: `test_reliability.py` (blackout, loss 10%/20%,
   reorder, dup, payload+header corruption, missing-final-chunk, restarts,
   coalescing, truth path, budget, phone alignment).
6. **Durable outbox**: stdlib SQLite WAL (`icebox/outbox.py`), persist before
   first tx, resume after restart, no duplicate incident; ground inbox
   persisted for idempotent restart; versioned `.ndz` report format +
   `inspect_report` / `replay_report`.
7. **Role split**: `--role all|onboard|ground`; same serialization in all
   modes; separate-process e2e test; health endpoint; DEPLOYMENT.md.
8. **Link profiles**: `--link-profile lab-2kbps` (current, honest label,
   + loss/latency/dup/reorder/corruption knobs) and `iridium-sbd`
   (message sessions, 340/270 limits, seeded, configurable latency).
9. **Phone alignment**: per-sensor buffers, interpolation to accel timestamps,
   staleness threshold, quat normalization, duplicate rejection, health state.
10. **UI**: truth strip; RECEIVING/PARTIAL/VERIFIED·HASH OK backfill states;
    packet wall from manifest with dup/corrupt/retry counts; harness panel
    labeling; remove Google Fonts (system stacks); recorded demo relabel +
    pause/restart; keep visual identity.

## 4. Risks & mitigations

| Change | Risk | Mitigation / acceptance test |
|---|---|---|
| queue → slot coalescing | starves durable traffic or breaks pacing | budget + starvation tests; priority policy documented in PROTOCOL.md |
| bypass removal | banner/analysis stop appearing | truth-path test asserts facts arrive AND only via link |
| ACK/retry | livelock or chatter | seeded loss tests; retry cap ⇒ explicit PARTIAL |
| SQLite outbox | corrupt on kill | WAL + insert-before-send; restart-resume test |
| role split | demo breaks | `--role all` default unchanged; separate-process e2e in CI |
| recorded demo | event schema drift | docs/ page kept frozen & compatible; no regeneration from new schema |
| UI relabels | visual regression | screenshot check; identity preserved |

## 5. Assumptions (documented, conservative)

- SBD session latency is configurable, default 8 s, presented as a model.
- One 26 km SRTM patch and EBC anchor constants are display context, not
  navigation truth (labeled on-stage).
- "mission_id" fixed at 1 for this prototype; boot_id = onboard start time.
- The frozen recorded session (docs/) predates the manifest protocol and is
  labeled as a recorded session of the earlier build.
