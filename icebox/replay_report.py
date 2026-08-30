"""Decode a black-box archive and print its incident analysis.

    python -m icebox.replay_report reports/report_00021.ndz
"""
import sys

from . import blackbox as bb
from .outbox import read_ndz


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    d = read_ndz(sys.argv[1])
    for r in d["records"]:
        try:
            seg = bb.decode_segment(r["data"])
        except Exception as e:
            print(f"tier {r['tier']}: decode failed: {e}")
            continue
        a = bb.analyze(seg)
        label = "REFINED" if r["tier"] == 2 else "PRELIMINARY"
        print(f"tier {r['tier']} ({label}): {seg['n']} samples @ {seg['rate']:.1f} Hz")
        print(f"  {a['summary']}")
        print(f"  confidence {a['confidence']} · limitations: {', '.join(a['limitations'])}")


if __name__ == "__main__":
    main()
