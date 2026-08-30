# NeverDrop — 2-minute pitch runbook (hardened)

**Setup:** `--role all --source arm` (real arm) with `--source sim` in a
second terminal as fallback; dashboard on the projector; click the page
once to arm audio; run `caffeinate -dims`. If using the phone instead:
laptop on the phone's hotspot, verify `http://<laptop-ip>:8000` loads on
the phone first. After a RESET wait ~15 s before triggering (recorder
refills by design).

## The story

**1 · Problem (0:00).**
> "This robot's only connection is two kilobits per second — a thousandth
> of one video stream. Everything you're about to see fits through that."

**2 · Truth labels (0:10).** Point at the truth strip:
> "Full disclosure up front: source — a real robot arm, real measurements;
> the link is a lab model of that satellite budget — no satellite hardware;
> and everything on this screen marked 'link' arrived as decoded packets,
> not through a back door. The test-harness controls are labeled below."

**3 · Live state (0:25).** Move the arm by hand:
> "31-byte state frames, seven per second. That's the whole supervision
> feed — base state, not video."

**4 · Blackout (0:40).** A judge presses ✂ CUT THE LINK:
> "Status is latest-value — nothing stale queues. Durable data keeps
> recording to disk onboard."

**5 · Restore (0:55).** ▲ RESTORE:
> "Live comes back instantly. And now the robot declares what you missed —
> a manifest with a hash — and delivers it: RECEIVING… VERIFIED, HASH OK.
> We never say 'zero lost' without that proof."

**6 · Incident (1:15).** Yank the arm hard:
> "Onboard trigger. The report is persisted to a SQLite outbox *before*
> transmission, then chunked with a manifest. Watch the wall: unique,
> duplicate, corrupt counts — and selective retransmit on a 270-byte
> reverse channel."

**7 · Progressive delivery (1:35).** Tier-1 lands:
> "Preliminary reconstruction with a confidence score and its limitations
> stated. Tier-2 refines it. If chunks go missing past the retry policy it
> says PARTIAL — it never lies."

**8 · Proof (1:50).**
> "Fourteen deterministic failure scenarios in CI — loss, reorder,
> corruption, both-side restarts. Black-box files you can inspect offline.
> Clone it; every number is derived from the structs."

**9 · Close (2:00).**
> "The recorder, prioritization, compression and verified delivery all run
> at the edge today. The satellite modem is the next explicit integration —
> its message limits are already enforced in the SBD profile."

## Fallback ladder

1. Arm misbehaves → second terminal `--source sim`, say "synthetic source"
   (the truth strip will say it for you).
2. Server dies → restart; the outbox resumes any in-flight report — that
   IS a demo beat, not a failure.
3. Total loss → recorded end-to-end session at
   vnmoorthy.github.io/neverdrop (labeled as recorded) or
   `test_reliability.py` live in a terminal.

## Judge Q&A

- **"Is the link real?"** No — and the screen says so. It's a deterministic
  token-bucket model (read `linksim.py`), plus an SBD session model with
  the real 340/270-byte limits. The modem is an explicit integration
  boundary.
- **"How do you know nothing was lost?"** We don't claim it without the
  transmitted manifest and SHA-256 verification; the UI states coverage
  x/y and hash status.
- **"Root cause?"** We call it incident analysis: a confidence-scored
  hypothesis with stated limitations — IMU data can't see actuator faults.
- **"What's simulated?"** Sim source: motion + battery/temp. Arm: real
  joints/currents, battery/temp NOT MEASURED. Link: model. Terrain: real
  SRTM data as display context.
- **"Restart mid-transfer?"** Kill either process; both persist and resume;
  the report completes exactly once (tests 09/10).
