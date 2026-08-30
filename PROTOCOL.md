# NeverDrop Wire Protocol (v1)

All numbers below are derivable at any time with
`python -m icebox.protocol_stats` — that module reads the actual structs.

## Traffic classes

| Class | Messages | Delivery | Queueing |
|---|---|---|---|
| Ephemeral | STATE (`ST`), HEARTBEAT (`HB`) | best-effort, latest-value | slot: a newer frame REPLACES the unsent one (coalesced counter) |
| Durable control | INCIDENT_NOTICE (`IN`), REPORT_MANIFEST (`RM`) | queued FIFO, highest priority | notice sent 3× for loss tolerance |
| Durable bulk | REPORT_CHUNK (`SB`) | persisted first, ACK-verified, selectively retransmitted | priority: tier-1 < backfill < tier-2 |
| Reverse | ACK (`AK`) | ground→onboard, ≤270 B (SBD MT), ~1 per 1.5 s (lab) / per session (SBD) | bounded queue |
| Local only | LINK_TEST_CONTROL (HTTP `/op`) | never presented as telemetry; UI label LOCAL TEST HARNESS | — |

## Measured sizes (from structs, this build)

| Message | Bytes |
|---|---|
| HEARTBEAT | 21 |
| STATE, 0 joints | 31 |
| STATE, 6 joints | 43 |
| INCIDENT_NOTICE | 22 |
| REPORT_MANIFEST | 59 |
| ACK worst case (100 missing) | 227 (≤ 270 MT limit) |
| REPORT_CHUNK header / payload max / total | 12 / 328 / 340 (SBD MO limit) |

Rates: humanoid 7 Hz, arm 5 Hz, throttled 2 Hz while durable traffic
drains; heartbeat 1 Hz. Steady-state slot demand: 1,736 + 168 = 1,904 bps
of the 2,000 bps lab budget (95.2%); under contention slots coalesce, so
the pacer never exceeds budget (measured 1,945 bps, t13).

## Identifiers & versioning

Every message carries `WIRE_VERSION` (=1); unknown versions are dropped.
Durable objects are scoped by `(mission_id, boot_id, report_id, tier)` with
`report_kind` (crash | backfill). Manifests carry chunk `total`, sample
count, rate, first/last timestamps, payload length and the first 16 bytes
of the payload SHA-256. Ground rejects a conflicting manifest for an
existing identity and drops partial state from superseded boots.

## Header validation (before any allocation)

`total ∈ (0, 512]`, `seq < total`, `payload ∈ (0, 328]`, `tier ∈ {1,2,3}`,
CRC-16 over header+payload; pre-manifest chunk cache bounded to 8 reports ×
64 chunks.

## ACK semantics

ACK = (identity, sha-digest8, highest contiguous seq, explicit missing list
≤100, complete flag). Ground acks every 2 s per open report. Onboard
applies acks idempotently, resends only missing chunks (≤64 per round),
counts rounds, and after `DEFAULT_MAX_ROUNDS` (20) marks the report
**PARTIAL_FAILED** — visible, never silent. A late complete-ACK may still
upgrade it to DELIVERED. `DELIVERED` requires a digest-matched complete
ACK, which ground sends only after full coverage + SHA-256 verification.

## Priority policy (documented, scheduler-enforced)

1. durable control (notice, manifests)
2. current heartbeat (slot)
3. current live state (slot)
4. durable chunks by priority (tier-1, backfill, tier-2)

Starvation guard: at most two slot sends per durable send while durable
work is pending.

## Link profiles

| profile | model | knobs |
|---|---|---|
| `lab-2kbps` | deterministic token-bucket payload pacing over UDP loopback/LAN | `--bps --loss --latency` (+dup/reorder/corrupt in tests), `--seed` |
| `iridium-sbd` | one message per session; no byte pipe | `--sbd-latency` (default 8 s — a documented model assumption, not a measurement), `--sbd-success`, `--seed` |

Neither profile is satellite hardware. The UI truth strip states the active
profile and `SATELLITE HARDWARE: NONE`.

## Integrity model

CRC-16 per packet (header+payload) → SHA-256 (16-byte prefix) per report
payload in the manifest → full SHA-256 in the `.ndz` archive. Ground
displays VERIFIED only after coverage == total AND hash match. The UI never
uses zero-loss language without this proof.
