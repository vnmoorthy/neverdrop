# Deployment

## Requirements

- Python ≥ 3.10 (developed/tested on 3.12, macOS; CI on Ubuntu 3.12).
- `pip install aiohttp` (the only runtime dependency).
- Optional for real arms: `pip install feetech-servo-sdk pyserial`.
- No cloud services. No CDN assets (fonts are system stacks; Three.js is
  vendored). The dashboard works offline once cloned.

## One-command demo (single host)

```bash
python -m icebox.server --role all --source sim          # dashboard :8000
python -m icebox.server --role all --source arm          # real Feetech arm
python -m icebox.server --role all --source phone        # Sensor Logger push
```

## Separate onboard / ground processes

```bash
# ground station (dashboard + UDP listener + ACK sender)
python -m icebox.server --role ground --port 8000 --listen-port 47700

# onboard (robot side; only UDP to the ground host leaves this process)
python -m icebox.server --role onboard --source sim \
    --ground-host 192.168.1.20 --ground-port 47700
```

Health: `GET /health` on either process reports source freshness, recorder
state, outbox pending/PARTIAL counts, link profile, and boot id. Proven by
`python test_split_roles.py` (two OS processes, same-host).

## Link profiles

```bash
--link-profile lab-2kbps  --bps 2000 --loss 0.1 --latency 0.35 --seed 7
--link-profile iridium-sbd --sbd-latency 12 --sbd-success 0.9 --seed 7
```

## Storage & restart

- `data/onboard_outbox.sqlite` (WAL): durable reports, persisted BEFORE
  first transmission; reloaded on restart with original identity.
- `data/ground_inbox.sqlite` (WAL): received manifests/chunks/verification;
  ground restarts are idempotent.
- `reports/report_NNNNN.ndz`: versioned black-box archives (atomic
  tmp+fsync+rename). Inspect/replay:
  `python -m icebox.inspect_report reports/report_00001.ndz`
  `python -m icebox.replay_report reports/report_00001.ndz`
- Retention: outbox keeps the newest 50 reports; blackout buffer bounded to
  9,000 samples; ring buffer 60 s.
- Shutdown: SIGTERM/SIGINT; WAL keeps both DBs consistent; unfinished
  reports resume on next start.

## Jetson (UNTESTED — documented, not claimed)

Nothing here has run on a Jetson. The onboard role is dependency-light
CPython, so the *expected* path is:

```bash
sudo apt install python3-pip
pip3 install aiohttp feetech-servo-sdk pyserial
python3 -m icebox.server --role onboard --source arm \
    --ground-host <basecamp-ip> --ground-port 47700 --data-dir /var/lib/neverdrop
```

systemd unit sketch:

```ini
[Unit]
Description=NeverDrop onboard recorder
After=network.target
[Service]
ExecStart=/usr/bin/python3 -m icebox.server --role onboard --source arm \
  --ground-host 10.0.0.2 --ground-port 47700 --data-dir /var/lib/neverdrop
Restart=always
[Install]
WantedBy=multi-user.target
```

Known-untested hardware dependencies: Jetson serial device naming
(`/dev/ttyACM*` vs `tty.usbmodem*` — the adapter globs both), Jetson clock
behavior across power loss (boot_id uses wall clock), storage endurance.

## Phone (Sensor Logger)

Enable Accelerometer + Gyroscope + Gravity + Orientation @ 100 Hz; HTTP
push to `http://<onboard-host>:<port>/phone`; use the phone's hotspot.
Turn ON "Standardise Units & Frames" if available — the pipeline warns via
`/health` source state when standardization is unknown. Battery/temp are
sent as NOT MEASURED.

## Real arm (Feetech SO-10x class)

Plug the follower's serial bus (auto-detected; override `ICEBOX_ARM_PORT`,
`ICEBOX_ARM_IDS`, `ICEBOX_ARM_BAUD`). Verified against physical hardware:
6 servos @ 1 Mbaud, 6.3 ms full read cycle, reads executed in a worker
thread so the event loop never blocks on the bus.

## Satellite modem integration boundary (NOT INTEGRATED)

`SatLink` is the boundary: replace its UDP send/receive with a modem
driver's MO/MT message calls. The SBD profile already enforces the 340 B /
270 B message limits and session-style pacing so the software above the
boundary is exercised against those constraints today.
