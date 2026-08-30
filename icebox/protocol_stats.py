"""Canonical protocol measurements, derived from the actual structs.

    python -m icebox.protocol_stats

Documentation and tests must quote THESE numbers, never prose. Rates below
mirror the constants in server.py's stream scheduler.
"""
from __future__ import annotations

from . import blackbox as bb
from . import wire

# scheduler constants (single source, imported by server)
RATE_HUMANOID_HZ = 7.0
RATE_ARM_HZ = 5.0
RATE_THROTTLED_HZ = 2.0
HB_HZ = 1.0
LAB_BPS = 2000
SBD_SESSION_LATENCY_S = 8.0     # configurable model default, not a measurement


def state_frame_size(n_joints: int = 0) -> int:
    s = bb.Sample(t=0.0, joints=(0.0,) * n_joints, currents=(0.0,) * n_joints)
    return len(bb.pack_state(0, s))


def heartbeat_size() -> int:
    return len(bb.pack_heartbeat(0, bb.Sample(t=0.0), 0, 0, 100))


def stats() -> dict:
    st0, st6 = state_frame_size(0), state_frame_size(6)
    hb = heartbeat_size()
    live_bps_humanoid = st0 * 8 * RATE_HUMANOID_HZ
    live_bps_arm = st6 * 8 * RATE_ARM_HZ
    hb_bps = hb * 8 * HB_HZ
    # example report sizes from a deterministic sim run
    from .telemetry import SimSource
    import time
    src = SimSource()
    ring = bb.RingBuffer(seconds=60, nominal_hz=200)
    t0 = time.time()
    fall_t = None
    for i in range(int(30 * 200)):
        t = t0 + i / 200.0
        if i == int(20 * 200):
            fall_t = t
        s = (src._falling(t, t - fall_t) if fall_t and t >= fall_t
             else src._nominal(t))
        ring.append(s)
    rep = bb.build_report(ring, t0 + 21.2, report_id=1)
    tz = rep["sizes"]
    return {
        "heartbeat_B": hb,
        "state_frame_B_0_joints": st0,
        "state_frame_B_6_joints": st6,
        "incident_notice_B": len(wire.pack_incident_notice(1, 1, "impact", 0.0)),
        "manifest_B": len(wire.pack_manifest(1, 1, 1, 1, 4, 200, 20.0, 0, 12, b"x" * 100)),
        "ack_B_worst": len(wire.pack_ack(1, 1, 2, b"\0" * 16, 10,
                                         list(range(wire.ACK_MAX_MISSING)), False)),
        "sbd_chunk_header_B": bb.PKT_HEAD.size,
        "sbd_chunk_payload_max_B": bb.PAYLOAD_MAX,
        "sbd_mo_max_B": bb.SBD_MAX,
        "sbd_mt_max_B": wire.SBD_MT_MAX,
        "rate_humanoid_hz": RATE_HUMANOID_HZ,
        "rate_arm_hz": RATE_ARM_HZ,
        "rate_throttled_hz": RATE_THROTTLED_HZ,
        "live_payload_bps_humanoid": live_bps_humanoid,
        "live_payload_bps_arm": live_bps_arm,
        "heartbeat_payload_bps": hb_bps,
        "utilization_humanoid_pct": round(100 * (live_bps_humanoid + hb_bps) / LAB_BPS, 1),
        "utilization_arm_pct": round(100 * (live_bps_arm + hb_bps) / LAB_BPS, 1),
        "tier1_example_B": tz["tier1"], "tier1_example_chunks": tz["pkts1"],
        "tier2_example_B": tz["tier2"], "tier2_example_chunks": tz["pkts2"],
        "tier1_tx_s_lab_dedicated": round(tz["pkts1"] * bb.SBD_MAX * 8 / LAB_BPS, 1),
        "tier2_tx_s_lab_dedicated": round(tz["pkts2"] * bb.SBD_MAX * 8 / LAB_BPS, 1),
        "tier1_tx_s_sbd_model": round(tz["pkts1"] * SBD_SESSION_LATENCY_S, 1),
        "tier2_tx_s_sbd_model": round(tz["pkts2"] * SBD_SESSION_LATENCY_S, 1),
    }


def main():
    s = stats()
    print("NeverDrop protocol measurements (derived from structs, not prose)")
    print("-" * 64)
    for k, v in s.items():
        print(f"  {k:34s} {v}")
    print("-" * 64)
    print("  lab-2kbps: deterministic token-bucket payload model")
    print(f"  iridium-sbd: message sessions, default {SBD_SESSION_LATENCY_S}s "
          "latency (configurable model assumption, not a field measurement)")


if __name__ == "__main__":
    main()
