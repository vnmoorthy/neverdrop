# NeverDrop — 2-minute pitch runbook

**Setup (before judges arrive):**

- Dashboard on the projector, `--source phone` running, phone strapped in a
  boot on the table, `--source sim` in a second terminal ready as fallback.
- **Network:** laptop joins the PHONE's Personal Hotspot (venue Wi-Fi
  usually blocks phone→laptop). Verify: open `http://<laptop-ip>:8000` in
  the phone's browser — if it loads, Sensor Logger's push will land.
- Run `caffeinate -dims` in a spare terminal so the laptop never sleeps.
- **Click the dashboard page once** after loading it — browsers block audio
  until a user gesture; one click arms the klaxon.
- Export a clean backup incident (EXPORT button), keep the JSON on the
  desktop. Leave the loss slider at 0.
- If a fall follows a RESET, wait ~15 s before triggering so the recorder
  refills (RESET clears the ring buffer by design).

## The script

**[0:00] Hook — pick up the boot and tilt it:**
> "This robot is above 8,000 meters. The only thing connecting it to Earth
> is 2 kilobits per second of satellite — video needs a thousand times
> more. That 3D robot on the wall is a live digital twin of this boot,
> streaming through that soda straw. Watch." *(tilt the boot; the twin
> tilts.)*

**[0:25] The cut.** Hand a judge the mouse:
> "Storms kill satellite links for minutes at a time. Cut it."

They press **✂ CUT THE LINK**. Twin freezes, LINK BLACKOUT pulses, the
buffered-onboard counter climbs, the charts tear a hole.

> "Most telemetry systems are now losing data forever. NeverDrop's robot
> is buffering onboard and waiting."

**[0:45] The restore.** They press **▲ RESTORE LINK**:
> "Live comes back instantly — and watch the hole." *(the backfill lands;
> the chart gap closes; the green stamp appears.)* "Backfilled. Zero
> samples lost. The robot compressed its own blackout and sent it home
> behind the live stream."

**[1:10] The crash.** Shove the boot off the table. Klaxon, red INCIDENT:
> "And when something actually goes wrong, the built-in black box decides
> the last ten seconds mattered — bursts them home as real 340-byte
> Iridium packets — and there's the crash, reconstructed in 3D, with the
> root cause computed from the bytes that crossed the link. Not scripted."

**[1:40] The business:**
> "Every robot beyond the cloud — mines, oceans, mountains, orbit — needs
> exactly this lifeline, and video physically cannot provide it. This is
> the observability layer of the physical world, per robot, per month."

**[1:55] The close:**
> "The expedition leaves in six days. This is a Python process and an IMU.
> It goes up the mountain with them."

## Fallback ladder (decide in 5 seconds, never apologize)

1. **Phone won't stream** → second terminal: `--source sim`. The twin,
   cut/restore, and crash all work identically; say "recorded test
   article" and drive it with the buttons.
2. **Server dies** → restart is one command; while it boots, LOAD the
   backup JSON — the dashboard replays a real incident through the same
   UI. Say what it is.
3. **Projector trouble** → `test_blackbox.py` prints the whole pipeline
   proof, and `test_phone_e2e.py` proves the live path. ALL PASS as a
   finale is legitimate nerd-sniping.

## Judge Q&A ammo

- **"Is the link real?"** — ~100-line token bucket over UDP at 2,000 bps
  (`linksim.py`). Slider goes to 300 bps live. LINK DOWN genuinely holds
  packets onboard; nothing is faked on restore.
- **"What's simulated?"** — In phone mode: only space (loopback instead of
  a satellite). Motion, framing, rate limit, reassembly, twin, backfill,
  analysis: all live. Sim mode simulates the robot too — we say so.
- **"Bandwidth math?"** — Twin: 26 B × 8 Hz ≈ 1.7 kbps (0.08% of one
  video stream). Blackout backfill: ~12 s of history in 3 packets.
  Crash: preview in ~1.2 KB, full 200 Hz record in ~24 KB.
- **"Why not ARQ/retransmit?"** — SBD has no cheap reverse channel; we
  buffer-and-backfill on restore and add 2× redundancy under injected
  loss. Tiered so the important bytes land first.
- **"Jetson Thor?"** — Onboard half is aiohttp-only Python sharing nothing
  with ground but UDP datagrams; moving it to the Thor is a loopback
  address change. No cloud anywhere.
- **"False triggers?"** — jerk z-score + 3 g floor + debounce + upright
  re-arm: 0 false fires in 60 s of gait; shove fires in ~1.1 s.
